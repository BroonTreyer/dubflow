"""Legenda do post (o texto embaixo do Reels) escrita pela IA, fiel ao filme.

Uma chamada por VIDEO (nao por parte): a IA ve quadros de cada parte e le as
falas com o tempo de cada uma, e devolve um texto proprio para cada parte, todos
contando a mesma historia sem se repetir. Quadros saem do video ORIGINAL (sem o
molde), inclusive com o que estiver escrito nele, que as vezes traz o nome do
filme. Falas vem so do cache de legenda (<video>.legenda.json): nunca dispara
transcricao.

Usa o papel "clip" do app.pipeline.llm (modelo forte, com visao e failover entre
provedores). Falhou = None, e quem chamou cai no modelo fixo.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("legenda_ia_filmes")

MAX_FALAS = 1500          # caracteres de dialogo por parte mandados para a IA
QUADROS_MAX = 12          # imagens por video (somando as partes)
LARGURA_QUADRO = 512

CTA_PADRAO = ("🍿 Quer assistir esse e muitos outros filmes e séries completos? "
              "Acesse o link da nossa bio! 🔗")

SISTEMA = """Voce escreve a legenda (o texto embaixo do video) de Reels de um perfil brasileiro
de cortes de filmes e series. Voce recebe quadros reais do video e as falas transcritas.

O que importa e ser FIEL a cena: quem assiste tem que reconhecer na legenda exatamente o
que esta vendo. Nada generico ("esse filme e incrivel", "cena tensa", "voce nao vai
acreditar") sem dizer o que acontece.

Para cada parte pedida, escreva um texto que:
- Conte o que acontece NAQUELA parte, com detalhes concretos que aparecem nos quadros ou
  nas falas: quem esta em cena (descreva: "o lutador mais velho", "a mae", "o policial"),
  onde, qual o conflito, o que esta em jogo. Pode citar uma fala marcante entre aspas.
- Desperte curiosidade para assistir ATE O FIM, sem contar como a cena termina.
- Parte 2 em diante continua a historia da anterior (sem repetir o que ja foi dito).
- Termine com uma pergunta curta que provoque comentario (ex.: "Voce teria aguentado
  calado?").
- Portugues do Brasil COM acentuacao correta (você, não, está, já), mesmo que estas
  instrucoes estejam sem acento.
- Tom de conversa, 2 a 4 frases, no maximo 320 caracteres,
  no maximo 2 emojis. Sem hashtags, sem "link na bio", sem "parte X" (isso e
  colocado depois automaticamente).

Nome do filme/serie:
- So preencha "filme" se tiver CERTEZA: titulo escrito na tela, nome dito nas falas, ou
  atores e cena que voce reconhece sem duvida. Escreva o titulo como e conhecido no
  Brasil, com o ano entre parenteses se souber. Na duvida, deixe "" e certeza "baixa".
  Errar o nome do filme e muito pior que nao citar.
- Nunca invente nome de personagem ou ator. So use nomes ditos nas falas ou escritos na
  tela.
- Ignore @ de outros perfis, marcas d'agua e textos de outras paginas que aparecam nos
  quadros (nunca os cite).

hashtags: 3 a 5, minusculas, sem acento, sem espaco, de genero/tema da cena que as pessoas
buscam de verdade (ex.: #drama #comedia #acao #suspense #terror #filmebrasileiro), mais a
do titulo do filme se tiver certeza (ex.: #cidadededeus). Nada de #parte1, #video, #reels."""

ESQUEMA = {
    "type": "object",
    "properties": {
        "filme": {"type": "string"},
        "certeza": {"type": "string", "enum": ["alta", "media", "baixa"]},
        "partes": {"type": "array", "items": {
            "type": "object",
            "properties": {"parte": {"type": "integer"}, "texto": {"type": "string"}},
            "required": ["parte", "texto"], "additionalProperties": False}},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["filme", "certeza", "partes", "hashtags"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------ entrada

def segmentos_de(video: Path) -> list[dict]:
    """Falas do cache de legenda (tempo relativo ao inicio do corte)."""
    cache = video.with_suffix(".legenda.json")
    if not cache.exists():
        return []
    try:
        segs = json.loads(cache.read_text(encoding="utf-8")).get("segments") or []
    except (json.JSONDecodeError, OSError, AttributeError):
        return []
    return [s for s in segs if isinstance(s, dict) and (s.get("text") or "").strip()]


def falas_de(video: Path, de: float = 0.0, ate: float = 0.0) -> str:
    """Falas entre `de` e `ate` segundos do corte (ate 0 = ate o fim); vazio se o
    video nunca foi legendado."""
    texto = " ".join(s["text"].strip() for s in segmentos_de(video)
                     if float(s.get("end", 0)) > de and (not ate or float(s.get("start", 0)) < ate))
    return texto[:MAX_FALAS]


def trechos(video: Path, partes: int) -> list[tuple[float, float]]:
    """(inicio, fim) de cada parte em segundos do video original: o corte dividido
    em partes iguais, como o render faz."""
    from scripts import molde_filmes as mf
    try:
        corte = mf.corte_de(video)
        dur = mf.duracao(video)
    except Exception:  # noqa: BLE001 — sem ffprobe/arquivo: sem tempo
        return [(0.0, 0.0)] * partes
    ini = corte["inicio"]
    fim = corte["fim"] if corte["fim"] > ini else dur
    fim = min(fim, dur) if dur else fim
    passo = max(fim - ini, 0) / max(partes, 1)
    return [(ini + k * passo, ini + (k + 1) * passo) for k in range(partes)]


def _quadro_jpeg(video: Path, t: float) -> str | None:
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
             "-vf", f"scale={LARGURA_QUADRO}:-2", "-q:v", "5", "-f", "image2pipe",
             "-vcodec", "mjpeg", "-"], capture_output=True, check=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    return base64.b64encode(r.stdout).decode() if r.stdout else None


def _mensagem(video: Path, gancho: str, partes: int, pedir: list[int]) -> list[dict]:
    """Conteudo do pedido: por parte, quadros + falas. Formato de blocos da Anthropic
    (o llm converte para a OpenAI)."""
    blocos: list[dict] = [{"type": "text", "text":
        f"Gancho escrito no video (todas as partes): {gancho.strip()}\n"
        f"O video foi dividido em {partes} parte(s). Escreva o texto das partes: "
        f"{', '.join(map(str, pedir))}."}]
    por_parte = max(1, min(3, QUADROS_MAX // max(partes, 1)))
    inicio_corte = trechos(video, partes)[0][0]
    for k, (a, b) in enumerate(trechos(video, partes), start=1):
        blocos.append({"type": "text", "text": f"--- PARTE {k} ({a:.0f}s a {b:.0f}s) ---"})
        for j in range(por_parte):
            t = a + (b - a) * (j + 0.5) / por_parte if b > a else a
            img = _quadro_jpeg(video, t)
            if img:
                blocos.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": img}})
        falas = falas_de(video, a - inicio_corte, b - inicio_corte if b > a else 0)
        blocos.append({"type": "text", "text": f"Falas da parte {k}: {falas}" if falas
                       else f"Parte {k}: sem falas transcritas (use os quadros)."})
    return blocos


# ------------------------------------------------------------------ saida

def _hashtag(h: str) -> str:
    h = re.sub(r"[^0-9a-z_]", "", h.lower().lstrip("#"))
    return f"#{h}" if h else ""


def compor(texto: str, tags_ia: list[str], fixas: str, parte: int, partes: int,
           filme: str = "", cta: str = "") -> str:
    """Texto da IA + filme + "Parte k/n" + chamada da bio + hashtags (fixas do
    painel + as da IA, sem repetir)."""
    vistas, tags = set(), []
    ia = [h for h in (_hashtag(t) for t in tags_ia)
          if h and not h.startswith("#parte") and h not in ("#filme", "#video", "#reels")]
    for h in fixas.split() + ia[:5]:
        if h and h.lower() not in vistas:
            vistas.add(h.lower())
            tags.append(h)
    blocos = [texto.strip()]
    if filme.strip():
        blocos.append(f"🎬 Filme: {filme.strip()}")
    if partes > 1:
        blocos.append(f"▶️ Parte {parte}/{partes}" + (
            f" — segue o perfil pra não perder a parte {parte + 1}!" if parte < partes
            else " (final)"))
    if cta.strip():
        blocos.append(cta.strip())
    if tags:
        blocos.append(" ".join(tags[:10]))
    return "\n\n".join(b for b in blocos if b)[:2200]


def gerar(pedidos: list[dict], cfg: dict) -> list[str | None]:
    """pedidos: [{"video": Path, "gancho", "parte", "partes"}] -> legenda ou None cada.

    `cfg`: config do Instagram (usa "hashtags" e "cta"). Partes do mesmo video
    vao juntas numa chamada so.
    """
    if not pedidos:
        return []
    try:
        from app.pipeline import llm  # carrega config/.env do dubflow so quando usado
    except Exception as exc:  # noqa: BLE001
        log.warning("IA indisponivel: %s", exc)
        return [None] * len(pedidos)
    grupos: dict[tuple[str, int], list[int]] = {}
    for i, p in enumerate(pedidos):
        grupos.setdefault((str(p["video"]), int(p.get("partes", 1))), []).append(i)
    chaves = list(grupos)
    tarefas = []
    for video, partes in chaves:
        idx = grupos[(video, partes)]
        pedir = sorted({int(pedidos[i].get("parte", 1)) for i in idx})
        tarefas.append({"system": SISTEMA, "schema": ESQUEMA, "max_tokens": 2500,
                        "user": _mensagem(Path(video), pedidos[idx[0]]["gancho"], partes, pedir)})
    try:
        respostas = llm.map_json(llm.ROLE_CLIP, tarefas, cache_system=True)
    except Exception as exc:  # noqa: BLE001 — sem IA, o modelo fixo assume
        log.warning("legenda por IA falhou: %s", exc)
        return [None] * len(pedidos)
    saida: list[str | None] = [None] * len(pedidos)
    for (video, partes), r in zip(chaves, respostas):
        try:
            dados = r.json() if r is not None and not r.refusal else None
        except (ValueError, AttributeError):
            dados = None
        if not dados:
            continue
        filme = dados.get("filme", "") if dados.get("certeza") == "alta" else ""
        textos = {int(x.get("parte", 0)): (x.get("texto") or "").strip()
                  for x in dados.get("partes") or [] if isinstance(x, dict)}
        for i in grupos[(video, partes)]:
            k = int(pedidos[i].get("parte", 1))
            if textos.get(k):
                saida[i] = compor(textos[k], dados.get("hashtags") or [],
                                  cfg.get("hashtags", ""), k, partes, filme,
                                  cfg.get("cta", ""))
    return saida
