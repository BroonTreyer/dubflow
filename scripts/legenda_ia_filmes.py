"""Legenda do post (o texto embaixo do Reels) escrita pela IA, uma por video.

Entra o gancho e, se o video ja foi legendado, as falas traduzidas (cache
<video>.legenda.json). Nunca dispara transcricao: sem cache, a IA trabalha so
com o gancho. Usa o papel "scan" do app.pipeline.llm (modelo pequeno, com
failover entre provedores). Falhou = None, e quem chamou cai no modelo fixo.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("legenda_ia_filmes")

MAX_FALAS = 1800   # caracteres de dialogo mandados para a IA

SISTEMA = """Voce escreve legendas de Reels para um perfil brasileiro de cortes de filmes e series.

Regras:
- Portugues do Brasil, tom de conversa, curto: 1 a 3 frases, no maximo 220 caracteres.
- Desperte curiosidade para a pessoa assistir ATE O FIM e comentar. Nao conte o final da cena.
- Nao repita o gancho palavra por palavra (ele ja aparece escrito no video); complemente.
- No maximo 2 emojis. Sem aspas, sem hashtags no texto, sem "link na bio".
- NUNCA invente o nome do filme, de ator ou de personagem. So cite um nome se ele aparecer
  nas falas. Na duvida, nao cite.
- Termine com uma pergunta curta OU um convite para seguir o perfil.
- hashtags: 2 a 4, sem acento, minusculas, de genero ou tema da cena que as pessoas
  realmente buscam (ex.: #suspense #terror #comedia #drama #acao #plottwist).
  Nada inventado ou juntado (#cinematrencador), nada de parte (#parte1), nada
  generico (#video #reels #filme)."""

ESQUEMA = {
    "type": "object",
    "properties": {
        "legenda": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["legenda", "hashtags"],
    "additionalProperties": False,
}


def falas_de(video: Path) -> str:
    """Dialogo ja traduzido do cache de legenda; vazio se o video nunca foi legendado."""
    cache = video.with_suffix(".legenda.json")
    if not cache.exists():
        return ""
    try:
        segs = json.loads(cache.read_text(encoding="utf-8")).get("segments") or []
    except (json.JSONDecodeError, OSError, AttributeError):
        return ""
    texto = " ".join((s.get("text") or "").strip() for s in segs if isinstance(s, dict))
    return texto[:MAX_FALAS]


def _pedido(gancho: str, falas: str, parte: int, partes: int) -> str:
    linhas = [f"Gancho escrito no video: {gancho.strip()}"]
    if partes > 1:
        linhas.append(f"Este post e a parte {parte} de {partes} da mesma cena.")
    linhas.append(f"Falas da cena (traduzidas): {falas}" if falas else
                  "Nao ha falas disponiveis: escreva so a partir do gancho.")
    return "\n".join(linhas)


def _hashtag(h: str) -> str:
    h = re.sub(r"[^0-9a-z_]", "", h.lower().lstrip("#"))
    return f"#{h}" if h else ""


def compor(texto: str, tags_ia: list[str], fixas: str, parte: int, partes: int) -> str:
    """Texto da IA + "Parte k/n" + hashtags fixas do painel + as da IA, sem repetir."""
    vistas, tags = set(), []
    ia = [h for h in (_hashtag(t) for t in tags_ia)
          if h and not h.startswith("#parte") and h not in ("#filme", "#video", "#reels")]
    for h in fixas.split() + ia[:4]:
        if h and h.lower() not in vistas:
            vistas.add(h.lower())
            tags.append(h)
    blocos = [texto.strip()]
    if partes > 1:
        blocos.append(f"Parte {parte}/{partes}")
    if tags:
        blocos.append(" ".join(tags[:10]))
    return "\n\n".join(b for b in blocos if b)[:2200]


def gerar(pedidos: list[dict], hashtags_fixas: str) -> list[str | None]:
    """pedidos: [{"video": Path, "gancho", "parte", "partes"}] -> legenda ou None cada."""
    if not pedidos:
        return []
    try:
        from app.pipeline import llm  # carrega config/.env do dubflow so quando usado
    except Exception as exc:  # noqa: BLE001
        log.warning("IA indisponivel: %s", exc)
        return [None] * len(pedidos)
    tarefas = [{"system": SISTEMA, "schema": ESQUEMA, "max_tokens": 600,
                "user": _pedido(p["gancho"], falas_de(p["video"]), p.get("parte", 1),
                                p.get("partes", 1))} for p in pedidos]
    try:
        respostas = llm.map_json(llm.ROLE_SCAN, tarefas, cache_system=True)
    except Exception as exc:  # noqa: BLE001 — sem IA, o modelo fixo assume
        log.warning("legenda por IA falhou: %s", exc)
        return [None] * len(pedidos)
    saida: list[str | None] = []
    for p, r in zip(pedidos, respostas):
        try:
            dados = r.json() if r is not None and not r.refusal else None
            texto = (dados or {}).get("legenda", "").strip()
        except (ValueError, AttributeError):
            texto = ""
        saida.append(compor(texto, dados.get("hashtags") or [], hashtags_fixas,
                            p.get("parte", 1), p.get("partes", 1)) if texto else None)
    return saida
