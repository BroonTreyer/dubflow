"""Etapa 5: selecao e renderizacao dos cortes verticais 9:16.

A selecao e feita por Claude sobre a transcricao traduzida com timestamps. O
modelo devolve trechos que se sustentam sozinhos — que e o criterio real de um
corte que funciona no feed, nao "o trecho onde alguem falou alto".
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import anthropic

from app.config import settings
from app.pipeline import subtitles

log = logging.getLogger(__name__)

ASSETS = Path(__file__).parent / "assets"

# Reframe 9:16 — parametros da "camera" que segue quem fala.
FOCUS_FPS = 2.0          # amostras por segundo (troca de falante dura ~1 s)
FOCUS_HYSTERESIS = 0.12  # so muda o enquadramento se o alvo andar mais que isso
MIN_SEGMENT = 1.2        # segundos minimos entre dois cortes de camera
MAX_SEGMENTS = 24        # teto de trocas por corte (a expressao do ffmpeg cresce)

# Detectores de rosto, carregados uma vez. YuNet (DNN) e melhor — pega rosto de
# lado e em angulo; o Haar frontal fica de reserva. Ambos tropecam em caminho com
# acento no Windows, entao sao carregados de forma que contorna isso (ver abaixo).
_CASCADE: Any = None
_CASCADE_LOADED = False
_YUNET: Any = None
_YUNET_LOADED = False

SELECTION_PROMPT = """\
Voce e editor de conteudo social e vive de fazer corte performar. Recebe um \
trecho de episodio com timestamps e escolhe os pedacos que funcionam como video \
vertical independente (Reels, TikTok, Shorts).

Seu unico criterio e retencao: a pessoa esta rolando o feed e precisa parar no \
seu corte e ficar ate o fim. Nao escolha o trecho "mais importante" do episodio \
— escolha o que prende.

{genre_block}
O QUE FAZ UM CORTE PERFORMAR

- **Os 3 primeiros segundos decidem tudo.** O corte tem que abrir no meio da \
tensao — uma acusacao, uma virada, uma frase que exige explicacao. Se abrir com \
rodeio, preparacao ou "entao, como eu estava dizendo", esta morto.
- **Uma emocao clara e forte**: raiva, vergonha alheia, revolta, desejo, choque, \
graca. Trecho morno nao performa, por mais bem escrito que seja.
- **Se sustenta sozinho.** Quem nunca viu o episodio entende sem contexto externo.
- **Tem virada.** O melhor corte muda de direcao no meio: a resposta atravessada, \
a revelacao, a frase que cala o outro.
- **Termina em pico, nao em descida.** Corte na melhor frase — nunca na conversa \
esfriando depois dela.
- **Gera comentario.** A pessoa quer opinar, discordar ou marcar alguem.

O QUE NAO SERVE

- Apresentacoes, agradecimentos, "se inscreva no canal", leitura de patrocinio.
- Trechos que dependem de imagem que voce nao viu (referencia a grafico na tela).
- Conversa de transicao, logistica, gente combinando o que vai fazer.
- Trecho tecnicamente correto mas sem carga emocional. Na duvida, deixe de fora.

REGRAS

- Escolha ate {count} trechos deste bloco, ou menos se o material nao render. \
Nao complete a cota com trecho fraco: e melhor devolver 4 fortes que 8 mornos.
- Cada trecho entre {min_s} e {max_s} segundos.
- Alinhe `start` e `end` ao inicio e ao fim de falas completas, usando os timestamps \
fornecidos. Nunca corte no meio de uma frase.
- Trechos nao podem se sobrepor.
- `hook`: a frase de abertura do trecho, copiada da transcricao — serve para conferir \
o alinhamento.
- `title`: titulo curto em pt-BR para uso interno.
- `caption`: legenda pronta para publicar em pt-BR — uma linha de gancho, quebra de \
linha, contexto em uma frase, quebra de linha, 3 a 5 hashtags relevantes. Sem emoji \
em excesso, no maximo dois.
- `yt_title`: titulo otimizado para o YouTube, em pt-BR. Ate 90 caracteres, com o \
gancho ou o dado mais forte logo no comeco (as primeiras palavras decidem o clique). \
Chamativo mas honesto — nada de clickbait que o trecho nao entrega. Sem hashtags.
- `yt_description`: descricao para o YouTube, em pt-BR. Duas ou tres frases: o que o \
trecho mostra e por que vale assistir, seguidas de 3 a 6 hashtags relevantes em uma \
linha. Escreva para busca — use os termos que o publico procuraria.
- `thumb_text`: o texto que vai ESTAMPADO na capa, em pt-BR. Regra dura: no maximo \
5 palavras, idealmente 3. Nao e o titulo resumido — e o gancho que faz parar o \
scroll, lido em meio segundo a 3 cm de altura. Use a tensao do trecho: \
"ELE NEGOU TUDO", "PERDEU R$ 2 MILHOES", "A PERGUNTA PROIBIDA". Sem ponto final, \
sem aspas, sem hashtag, sem emoji. Marque com asteriscos a palavra (ou duas) que \
deve sair colorida na capa: "ELE *MENTIU* NA CARA".
- `score`: 0 a 10, o quanto voce aposta que ESTE corte performa. Use a escala \
inteira e seja duro: 9-10 e o corte que voce publicaria hoje, 7-8 e bom, 5-6 e \
mediano, abaixo de 5 nao deveria ter sido escolhido. Varios cortes com nota \
parecida tornam o score inutil — ele e usado para ranquear os trechos do \
episodio inteiro e cortar os piores.

Responda apenas com o JSON do schema.
"""

GENRE_PROMPT = """\
Voce recebe amostras da transcricao de um video e responde o que ele e, para \
orientar um editor de cortes. Seja concreto e curto.

- `genre`: o formato em poucas palavras (ex.: "novela turca dublada", \
"podcast de entrevista", "aula de matematica", "gameplay comentado").
- `audience`: quem assiste isso e o que essa pessoa procura.
- `viral_criteria`: 3 a 5 bullets do que faz um corte DESTE formato performar \
em Reels/TikTok. Especifico do formato, nao conselho generico: numa novela sao \
brigas, declaracoes, humilhacoes e revelacoes; num podcast sao opinioes \
polemicas e historias pessoais; numa aula e o macete que resolve rapido.
- `avoid`: o que neste formato parece bom e nao e.

Responda apenas com o JSON do schema.
"""

GENRE_SCHEMA = {
    "type": "object",
    "properties": {
        "genre": {"type": "string"},
        "audience": {"type": "string"},
        "viral_criteria": {"type": "array", "items": {"type": "string"}},
        "avoid": {"type": "string"},
    },
    "required": ["genre", "audience", "viral_criteria", "avoid"],
    "additionalProperties": False,
}

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "caption": {"type": "string"},
                    "yt_title": {"type": "string"},
                    "yt_description": {"type": "string"},
                    "thumb_text": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["start", "end", "title", "hook", "caption",
                             "yt_title", "yt_description", "thumb_text", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def target_count(duration_s: float) -> int:
    """Quantos cortes um episodio desta duracao deve render."""
    alvo = round((duration_s / 3600.0) * settings.clips_per_hour)
    # O piso vale para video curto (10 min nao pode voltar com 3 cortes so porque
    # a conta deu 3), mas nunca pode passar do teto.
    alvo = max(alvo, min(settings.clips_per_episode, settings.clips_max))
    return int(min(alvo, settings.clips_max))


def _detect_genre(client: anthropic.Anthropic, segments: list[dict[str, Any]],
                  meta: dict[str, Any]) -> dict[str, Any] | None:
    """Le uma amostra do episodio e devolve o que faz um corte DELE performar.

    Sem isto o prompt de selecao fala de "numero surpreendente" e "leitura de
    patrocinio" para uma novela — criterios de podcast aplicados a ficcao.
    """
    textos = [(s.get("text") or "").strip() for s in segments if (s.get("text") or "").strip()]
    if not textos:
        return None

    # Amostra de tres pontos: abertura, meio e fim tem cara diferente no mesmo video.
    n = len(textos)
    amostra = textos[: min(60, n)] + textos[n // 2: n // 2 + 60] + textos[-60:]

    try:
        response = client.messages.create(
            model=settings.clip_scan_model,
            max_tokens=1500,
            system=[{"type": "text", "text": GENRE_PROMPT}],
            output_config={"format": {"type": "json_schema", "schema": GENRE_SCHEMA}},
            messages=[{
                "role": "user",
                "content": (f"Titulo: {meta.get('title')}\nCanal: {meta.get('channel')}\n\n"
                            "Amostras da transcricao:\n" + "\n".join(amostra)),
            }],
        )
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text)
    except Exception as exc:  # o reconhecimento e um extra: sem ele a selecao ainda roda
        log.warning("deteccao de genero falhou (%s) — seguindo com criterios genericos", exc)
        return None


def _genre_block(genre: dict[str, Any] | None) -> str:
    if not genre:
        return ""
    criterios = "\n".join(f"- {c}" for c in genre.get("viral_criteria") or [])
    return (
        "ESTE VIDEO ESPECIFICAMENTE\n\n"
        f"Formato: {genre.get('genre')}\n"
        f"Publico: {genre.get('audience')}\n\n"
        f"O que faz um corte deste formato performar:\n{criterios}\n\n"
        f"Evite neste formato: {genre.get('avoid')}\n\n"
    )


def _windows(segments: list[dict[str, Any]], count: int) -> list[tuple[list[dict[str, Any]], int]]:
    """Fatia o episodio em janelas de analise, com a cota de cada uma."""
    if not segments:
        return []
    duracao = max(float(s["end"]) for s in segments)
    janela_s = max(300, settings.clip_window_minutes * 60)
    n_janelas = max(1, round(duracao / janela_s))
    if n_janelas == 1:
        return [(segments, count)]

    passo = duracao / n_janelas
    out: list[tuple[list[dict[str, Any]], int]] = []
    for i in range(n_janelas):
        ini, fim = i * passo, (i + 1) * passo
        bloco = [s for s in segments if ini <= float(s["start"]) < fim]
        if not bloco:
            continue
        # Pede com folga por janela: parte vira sobreposicao ou cai no corte final
        # por score, e uma janela fraca nao deve arrastar a cota do episodio.
        cota = max(2, round(count / n_janelas) + 2)
        out.append((bloco, cota))
    return out


def _select_window(client: anthropic.Anthropic, bloco: list[dict[str, Any]], cota: int,
                   meta: dict[str, Any], genre_block: str) -> list[dict[str, Any]]:
    transcript = [
        {"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s.get("text") or ""}
        for s in bloco
        if (s.get("text") or "").strip()
    ]
    if not transcript:
        return []

    system = SELECTION_PROMPT.format(
        count=cota, genre_block=genre_block,
        min_s=settings.clip_min_seconds, max_s=settings.clip_max_seconds,
    )
    ini_min = int(transcript[0]["start"] // 60)
    fim_min = int(transcript[-1]["end"] // 60)
    user = (
        f"Episodio: {meta.get('title')}\nCanal: {meta.get('channel')}\n"
        f"Bloco analisado: minuto {ini_min} ao {fim_min} do episodio.\n\n"
        "Transcricao com timestamps (segundos, na escala do episodio inteiro):\n"
        "```json\n" + json.dumps(transcript, ensure_ascii=False) + "\n```"
    )

    response = client.messages.create(
        model=settings.clip_model,
        max_tokens=16000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": CLIP_SCHEMA},
        },
        messages=[{"role": "user", "content": user}],
    )

    if response.stop_reason == "refusal":
        log.warning("selecao de cortes recusada pelos classificadores (minuto %d-%d)",
                    ini_min, fim_min)
        return []

    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text).get("clips", [])


def select_clips(
    segments: list[dict[str, Any]],
    meta: dict[str, Any],
    count: int | None = None,
) -> list[dict[str, Any]]:
    """Pede a Claude os melhores trechos do episodio, janela por janela."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY nao configurada.")
    if not segments:
        return []

    duracao = max(float(s["end"]) for s in segments)
    count = count or target_count(duracao)
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    genre = _detect_genre(client, segments, meta)
    if genre:
        log.info("cortes: formato reconhecido como '%s'", genre.get("genre"))
    genre_block = _genre_block(genre)

    janelas = _windows(segments, count)
    log.info("cortes: alvo de %d em %.0f min, analisando em %d janela(s)",
             count, duracao / 60, len(janelas))

    bruto: list[dict[str, Any]] = []
    if len(janelas) == 1:
        bruto = _select_window(client, janelas[0][0], janelas[0][1], meta, genre_block)
    else:
        # Janelas sao independentes entre si, entao vao em paralelo — senao um
        # episodio de 2h faria 7 chamadas de alto effort em fila.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(_select_window, client, bloco, cota, meta, genre_block)
                for bloco, cota in janelas
            ]
            for fut in futures:
                try:
                    bruto.extend(fut.result())
                except Exception as exc:
                    # Uma janela que falha nao pode derrubar o episodio inteiro.
                    log.warning("uma janela de selecao falhou (%s) — seguindo com as demais", exc)

    return _sanitize(bruto, segments, count)


def _sanitize(clips: list[dict[str, Any]], segments: list[dict[str, Any]],
              limit_count: int | None = None) -> list[dict[str, Any]]:
    """Encaixa cada corte nas fronteiras reais de fala e remove sobreposicao.

    Os cortes chegam de varias janelas, entao a disputa por sobreposicao e
    resolvida por score: percorrer na ordem do episodio faria o corte pior
    ganhar do melhor so por comecar antes.
    """
    if not segments:
        return []
    starts = sorted({float(s["start"]) for s in segments})
    ends = sorted({float(s["end"]) for s in segments})
    limit = max(ends)

    def nearest(values: list[float], target: float) -> float:
        return min(values, key=lambda v: abs(v - target))

    clips = sorted(clips, key=lambda c: float(c.get("score") or 0), reverse=True)

    cleaned: list[dict[str, Any]] = []
    for clip in clips:
        try:
            start = nearest(starts, float(clip["start"]))
            end = nearest(ends, float(clip["end"]))
        except (KeyError, TypeError, ValueError):
            continue

        start = max(0.0, start - 0.25)  # respiro antes da primeira palavra
        end = min(limit, end + 0.4)
        duration = end - start
        if duration < settings.clip_min_seconds * 0.6 or duration > settings.clip_max_seconds * 1.5:
            continue
        if any(start < c["end"] and end > c["start"] for c in cleaned):
            continue

        cleaned.append(
            {
                "start": round(start, 2),
                "end": round(end, 2),
                "title": (clip.get("title") or "").strip()[:120],
                "hook": (clip.get("hook") or "").strip()[:300],
                "caption": (clip.get("caption") or "").strip()[:2000],
                "yt_title": (clip.get("yt_title") or "").strip()[:100],
                "yt_description": (clip.get("yt_description") or "").strip()[:4800],
                "thumb_text": (clip.get("thumb_text") or "").strip()[:80],
                "score": float(clip.get("score") or 0),
            }
        )

    if limit_count is not None:
        # cleaned ja esta em ordem de score, entao o corte do teto tira os piores.
        cleaned = cleaned[:limit_count]

    cleaned.sort(key=lambda c: c["start"])
    return cleaned


def _clip_segments(segments: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    """Recorta os segmentos do intervalo e rebaseia os timestamps para zero."""
    out = []
    for seg in segments:
        if float(seg["end"]) <= start or float(seg["start"]) >= end:
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        # Rebaseia tambem os timestamps por palavra (recortando a janela do corte),
        # para a legenda do corte poder aparar no tempo real da fala.
        words = []
        for w in (seg.get("words") or []):
            ws, we = w.get("start"), w.get("end")
            if ws is None or we is None or float(we) <= start or float(ws) >= end:
                continue
            words.append({
                "start": max(0.0, float(ws) - start),
                "end": min(end - start, float(we) - start),
                "word": w.get("word", ""),
            })
        entry = {
            "start": max(0.0, float(seg["start"]) - start),
            "end": min(end - start, float(seg["end"]) - start),
            "text": text,
        }
        if words:
            entry["words"] = words
        out.append(entry)
    return out


def _load_cascade() -> Any:
    """Carrega o detector Haar de rosto, uma vez, tolerando ausencia do opencv.

    O XML e lido em Python (que abre caminhos Unicode) e passado ao OpenCV pela
    memoria: no Windows o cv2 nao abre arquivos em caminhos com acento, e a pasta
    do projeto ("Area de Trabalho") tem um. Sem isso, o detector nem carregaria.
    """
    global _CASCADE, _CASCADE_LOADED
    if _CASCADE_LOADED:
        return _CASCADE
    _CASCADE_LOADED = True
    try:
        import cv2
    except ImportError:
        log.warning("opencv indisponivel; o reframe cai para o recorte central")
        return None
    try:
        data = (ASSETS / "haarcascade_frontalface_default.xml").read_text(encoding="utf-8")
        fs = cv2.FileStorage(data, cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
        cascade = cv2.CascadeClassifier()
        cascade.read(fs.getFirstTopLevelNode())
        _CASCADE = None if cascade.empty() else cascade
    except Exception as exc:  # noqa: BLE001 — deteccao e um luxo; nunca derruba o render
        log.warning("falha ao carregar detector de rosto (%s); reframe central", exc)
        _CASCADE = None
    return _CASCADE


def _load_yunet() -> Any:
    """Carrega o detector YuNet (DNN), uma vez. None se o modelo/opencv faltar.

    O cv2 abre o .onnx por caminho, e no Windows nao le caminho com acento; entao
    copiamos o modelo para uma pasta temporaria ASCII e carregamos de la.
    """
    global _YUNET, _YUNET_LOADED
    if _YUNET_LOADED:
        return _YUNET
    _YUNET_LOADED = True
    model = ASSETS / "face_detection_yunet_2023mar.onnx"
    if not model.exists():
        return None
    try:
        import cv2
        tmp = Path(tempfile.gettempdir()) / "dubflow_yunet_2023mar.onnx"
        if not tmp.exists() or tmp.stat().st_size != model.stat().st_size:
            shutil.copyfile(model, tmp)
        _YUNET = cv2.FaceDetectorYN_create(str(tmp), "", (320, 320), score_threshold=0.6)
    except Exception as exc:  # noqa: BLE001 — sem YuNet caimos no Haar
        log.warning("YuNet indisponivel (%s); usando Haar", exc)
        _YUNET = None
    return _YUNET


def _face_boxes(img: Any) -> list[tuple[float, float, float, float]]:
    """Devolve (x, y, w, h) em pixels de cada rosto no frame — YuNet, senao Haar.

    A caixa inteira importa, nao so o centro: sem a largura nao da para garantir
    que o rosto caiba dentro da janela 9:16 em vez de ser cortado ao meio.
    """
    import cv2

    h, w = img.shape[:2]
    yunet = _load_yunet()
    if yunet is not None:
        yunet.setInputSize((w, h))
        _, faces = yunet.detect(img)
        if faces is None:
            return []
        return [(float(f[0]), float(f[1]), float(f[2]), float(f[3])) for f in faces]

    cascade = _load_cascade()
    if cascade is None:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5,
        minSize=(int(h * 0.08), int(h * 0.08)),
    )
    return [(float(x), float(y), float(fw), float(fh)) for (x, y, fw, fh) in faces]


def _face_centers(img: Any) -> list[tuple[float, float]]:
    """(centro_x_px, area) de cada rosto — atalho sobre _face_boxes."""
    return [(x + w / 2, w * h) for (x, _y, w, h) in _face_boxes(img)]


def _window_ratio(frame_w: int, frame_h: int) -> float:
    """Fracao da largura do frame que a janela 9:16 cobre (1.0 = frame inteiro)."""
    if frame_w <= 0 or frame_h <= 0:
        return 1.0
    return min(1.0, (1080 / 1920) * (frame_h / frame_w))


def _focus_for_span(x0: float, x1: float, frame_w: int, frame_h: int,
                    margin: float = 0.02) -> float:
    """Posiciona a janela 9:16 de modo a conter o intervalo [x0, x1] inteiro.

    x0/x1 sao normalizados (0..1) e delimitam o que precisa aparecer — a caixa do
    rosto escolhido, ou a de um grupo de rostos. Quando o intervalo cabe na
    janela, o resultado e o enquadramento mais centrado que ainda nao corta
    ninguem; quando nao cabe, centraliza no meio do intervalo (o chamador ja
    deveria ter escolhido um subconjunto que coubesse).
    """
    r = _window_ratio(frame_w, frame_h)
    if r >= 1:  # fonte ja e 9:16 ou mais estreita: nao ha corte horizontal a fazer
        return 0.5

    centro = (x0 + x1) / 2
    ideal = (centro - r / 2) / (1 - r)

    # Faixa de posicoes que mantem [x0-margem, x1+margem] dentro da janela.
    lo = (x1 + margin - r) / (1 - r)
    hi = (x0 - margin) / (1 - r)
    if lo <= hi:
        ideal = max(lo, min(hi, ideal))
    return max(0.0, min(1.0, ideal))


def _pick_group(boxes: list[tuple[float, float, float, float]],
                pesos: list[float], frame_w: int, frame_h: int,
                margin: float = 0.02) -> tuple[float, float] | None:
    """Escolhe o que enquadrar: um rosto, ou os vizinhos que cabem junto com ele.

    Substitui a media ponderada de todos os rostos, que era o bug real — com duas
    pessoas afastadas a media caia no vazio entre elas e cortava as duas.
    """
    if not boxes:
        return None
    r = _window_ratio(frame_w, frame_h)
    util = max(0.0, r - 2 * margin)  # largura aproveitavel dentro da janela

    ordem = sorted(range(len(boxes)), key=lambda i: boxes[i][0])
    melhor: tuple[float, float, float] | None = None  # (peso, x0, x1)

    # Cada rosto e uma semente; o grupo cresce enquanto o conjunto couber na janela.
    for pos, i in enumerate(ordem):
        x0 = boxes[i][0] / frame_w
        x1 = (boxes[i][0] + boxes[i][2]) / frame_w
        peso = pesos[i]
        for j in ordem[pos + 1:]:
            nx0 = min(x0, boxes[j][0] / frame_w)
            nx1 = max(x1, (boxes[j][0] + boxes[j][2]) / frame_w)
            if (nx1 - nx0) > util:
                break  # o proximo rosto ja nao cabe junto: fecha o grupo aqui
            x0, x1, peso = nx0, nx1, peso + pesos[j]
        if melhor is None or peso > melhor[0]:
            melhor = (peso, x0, x1)

    return (melhor[1], melhor[2]) if melhor else None


def _focus_from_center(cx_norm: float, frame_w: int, frame_h: int) -> float:
    """Converte o centro horizontal do rosto (0..1) na posicao da janela 9:16.

    Devolve 0 (janela na esquerda), 0.5 (centro) ou 1 (direita). `r` e a fracao
    da largura — ja escalada para cobrir 1080x1920 — que a janela vertical ocupa;
    fora dessa faixa util o valor e travado para nao vazar do quadro.
    """
    if frame_w <= 0:
        return 0.5
    r = (1080 / 1920) * (frame_h / frame_w)
    if r >= 1:  # fonte ja e 9:16 ou mais estreita: nao ha corte horizontal a fazer
        return 0.5
    focus = (cx_norm - r / 2) / (1 - r)
    return max(0.0, min(1.0, focus))


def _detect_focus(video_path: Path, start: float,
                  duration: float) -> list[tuple[float, float]] | None:
    """Devolve a trilha [(segundo, foco 0..1)] que a janela 9:16 deve seguir.

    Uma so posicao para o corte inteiro nao da conta de cena com duas pessoas: a
    janela precisa acompanhar quem esta falando. A lista e sempre nao-vazia e
    comeca em t=0; um corte sem troca de enquadramento volta com um item so.

    Devolve None quando nao ha como decidir (sem opencv, sem ffmpeg, ou nenhum
    rosto encontrado) — o chamador entao usa o recorte central. Os frames saem
    para uma pasta temporaria ASCII porque o cv2.imread tambem tropeca em acento.
    """
    try:
        import cv2
    except ImportError:
        return None
    # Precisa de ao menos um detector (YuNet ou Haar); senao nao ha o que focar.
    if _load_yunet() is None and _load_cascade() is None:
        return None

    rate = FOCUS_FPS
    with tempfile.TemporaryDirectory(prefix="dubflow_focus_") as td:
        pattern = str(Path(td) / "f_%04d.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", "-i", str(video_path),
            # Reduz para 480 de largura: deteccao de rosto nao precisa de resolucao
            # cheia e assim roda rapido mesmo em CPU.
            "-vf", f"fps={rate:.4f},scale=480:-2", "-q:v", "4", pattern,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
        except OSError as exc:  # ffmpeg ausente do PATH, por exemplo
            log.warning("ffmpeg indisponivel para deteccao (%s); reframe central", exc)
            return None
        if res.returncode != 0:
            log.warning("extracao de frames para deteccao falhou; reframe central")
            return None

        amostras: list[tuple[float, float, float]] = []  # (t, x0, x1) do grupo escolhido
        frame_w = frame_h = 0
        anterior = None
        for i, frame in enumerate(sorted(Path(td).glob("f_*.jpg"))):
            img = cv2.imread(str(frame))
            if img is None:
                continue
            frame_h, frame_w = img.shape[:2]
            cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            boxes = _face_boxes(img)
            if boxes:
                # Peso = tamanho do rosto, multiplicado pela boca em movimento. Numa
                # conversa os dois rostos tem area parecida; quem fala e o criterio
                # que decide, e e o que o espectador espera ver enquadrado.
                fala = _mouth_activity(cinza, anterior, boxes)
                pesos = [(b[2] * b[3]) * (1.0 + 2.0 * a) for b, a in zip(boxes, fala)]
                grupo = _pick_group(boxes, pesos, frame_w, frame_h)
                if grupo is not None:
                    amostras.append((i / rate, grupo[0], grupo[1]))
            anterior = cinza

    if not amostras or frame_w == 0:
        return None
    return _build_track(amostras, frame_w, frame_h, duration)


def _mouth_activity(cinza: Any, anterior: Any,
                    boxes: list[tuple[float, float, float, float]]) -> list[float]:
    """Quanto a boca de cada rosto se mexeu desde o frame anterior (0..1).

    Aproximacao barata de "quem esta falando": compara a faixa da boca (terco
    inferior do rosto) com o mesmo recorte do frame anterior. E relativa ao frame,
    entao um corte de camera — que mexe tudo de uma vez — nao elege ninguem.
    """
    if anterior is None or anterior.shape != cinza.shape:
        return [0.0] * len(boxes)

    import numpy as np

    brutos: list[float] = []
    for (x, y, w, h) in boxes:
        bx0, bx1 = int(x + w * 0.2), int(x + w * 0.8)
        by0, by1 = int(y + h * 0.6), int(y + h * 1.0)
        bx0, by0 = max(0, bx0), max(0, by0)
        bx1 = min(cinza.shape[1], bx1)
        by1 = min(cinza.shape[0], by1)
        if bx1 - bx0 < 4 or by1 - by0 < 4:
            brutos.append(0.0)
            continue
        atual = cinza[by0:by1, bx0:bx1].astype("float32")
        antes = anterior[by0:by1, bx0:bx1].astype("float32")
        brutos.append(float(np.abs(atual - antes).mean()))

    teto = max(brutos) if brutos else 0.0
    if teto < 2.0:  # ninguem se mexeu de verdade: nao inventa um falante
        return [0.0] * len(boxes)
    return [b / teto for b in brutos]


def _build_track(amostras: list[tuple[float, float, float]], frame_w: int, frame_h: int,
                 duration: float) -> list[tuple[float, float]]:
    """Transforma as amostras por frame numa trilha estavel de (tempo, foco).

    Duas travas contra a "camera nervosa": a mediana movel absorve deteccao que
    pisca, e a histerese so publica uma mudanca quando ela e grande e se sustenta
    por MIN_SEGMENT segundos. O resultado e um corte de camera, nao um tremor.
    """
    focos = [
        (t, _focus_for_span(x0, x1, frame_w, frame_h))
        for (t, x0, x1) in amostras
    ]

    # Mediana movel de 5 amostras (~2,5 s): remove deteccao isolada fora do lugar.
    suave: list[tuple[float, float]] = []
    for i, (t, _f) in enumerate(focos):
        janela = [f for _t, f in focos[max(0, i - 2): i + 3]]
        suave.append((t, sorted(janela)[len(janela) // 2]))

    trilha: list[tuple[float, float]] = []
    atual = suave[0][1]
    trilha.append((0.0, atual))
    candidato: tuple[float, float] | None = None
    for t, f in suave:
        if abs(f - atual) < FOCUS_HYSTERESIS:
            candidato = None
            continue
        if candidato is None or abs(f - candidato[1]) >= FOCUS_HYSTERESIS:
            candidato = (t, f)
            continue
        # O novo enquadramento se manteve tempo suficiente: vira corte de camera.
        # A distancia minima vale entre os tempos PUBLICADOS (candidato[0], nao t):
        # comparar com t deixaria passar dois cortes colados no inicio do trecho.
        if t - candidato[0] >= MIN_SEGMENT and candidato[0] - trilha[-1][0] >= MIN_SEGMENT:
            atual = candidato[1]
            trilha.append((candidato[0], atual))
            candidato = None

    if len(trilha) > MAX_SEGMENTS:
        trilha = trilha[:MAX_SEGMENTS]
    return trilha


Track = float | list[tuple[float, float]]


def _focus_expr(track: Track) -> str:
    """Expressao de foco para o ffmpeg: constante, ou variavel no tempo.

    Com mais de um segmento vira um if aninhado sobre `t`, que o crop avalia a
    cada frame — e assim a janela corta de uma pessoa para a outra no meio do
    trecho, em vez de ficar parada no meio das duas.
    """
    if isinstance(track, (int, float)):
        return f"{float(track):.4f}"
    if len(track) == 1:
        return f"{track[0][1]:.4f}"

    # Do fim para o inicio: if(lt(t,T1),F0, if(lt(t,T2),F1, ... Fn))
    expr = f"{track[-1][1]:.4f}"
    for (t_corte, _f), (_t_ant, f_ant) in zip(track[:0:-1], track[-2::-1]):
        expr = f"if(lt(t\\,{t_corte:.2f})\\,{f_ant:.4f}\\,{expr})"
    return expr


def _focus_at(track: Track, t: float) -> float:
    """O foco vigente em um instante — usado pela capa vertical (frame unico)."""
    if isinstance(track, (int, float)):
        return float(track)
    atual = track[0][1]
    for t_corte, f in track:
        if t_corte <= t:
            atual = f
        else:
            break
    return atual


def _vertical_chain(mode: str, focus: Track) -> str:
    """Cadeia que leva o quadro a 9:16, terminando no rotulo [framed].

    Fica separada da queima de legenda porque a thumbnail vertical precisa do
    mesmo enquadramento do corte — e uma capa com a cabeca cortada nao serve.
    """
    if mode == "pad":
        # Legado: o video inteiro encolhido no meio de um fundo borrado. Nao foca
        # na cena — a faixa util fica pequena entre duas barras desfocadas.
        return (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=22:2[bg];"
            "[0:v]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[framed]"
        )
    # face/center: recorta uma janela 9:16 que preenche a tela. O foco desliza a
    # janela na horizontal (0=esquerda, 0.5=centro, 1=direita); (in_w-out_w) e a
    # folga real, entao o corte nunca sai do quadro.
    return (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920:x='(in_w-out_w)*({_focus_expr(focus)})':y='(in_h-out_h)/2'[framed]"
    )


def _reframe_filter(mode: str, focus: Track, ass_path: Path) -> str:
    """Monta o filtro do ffmpeg que leva o video a 9:16 e queima a legenda."""
    sub = f"subtitles='{subtitles._escape_for_filter(ass_path)}'"
    return f"{_vertical_chain(mode, focus)};[framed]{sub}[v]"


# O reframe de um mesmo trecho e reusado pela thumbnail vertical: detectar rosto
# custa extracao de frames, e rodar duas vezes so para a capa nao se paga.
_REFRAME_CACHE: dict[tuple[str, float, float], tuple[str, Track]] = {}


def _resolve_reframe(video_path: Path, start: float, duration: float) -> tuple[str, Track]:
    """Decide o modo de enquadramento e a trilha de foco da janela 9:16."""
    mode = settings.clip_reframe
    if mode != "face":
        return mode, 0.5

    key = (str(video_path), round(start, 2), round(duration, 2))
    if key not in _REFRAME_CACHE:
        detected = _detect_focus(video_path, start, duration)
        # Sem rosto (ou sem detector), cai para o centro: preenche a tela do mesmo
        # jeito, so nao segue o rosto.
        _REFRAME_CACHE[key] = ("center", 0.5) if detected is None else ("face", detected)
    return _REFRAME_CACHE[key]


def render_clip(
    video_path: Path,
    segments: list[dict[str, Any]],
    clip: dict[str, Any],
    output_path: Path,
    work_dir: Path,
) -> Path:
    """Corta o trecho, converte para 9:16 focando na cena e queima a legenda.

    O reframe (CLIP_REFRAME) recorta uma janela vertical que preenche a tela: em
    'face' a janela e posicionada sobre o rosto detectado; em 'center' fica no
    meio; 'pad' mantem o encaixe antigo com fundo borrado.
    """
    start, end = float(clip["start"]), float(clip["end"])
    duration = end - start

    karaoke = settings.clip_karaoke
    ass_path = work_dir / f"clip_{output_path.stem}.ass"
    subtitles.write_ass(
        _clip_segments(segments, start, end),
        ass_path,
        width=1080,
        height=1920,
        style=subtitles.STYLE_CLIP_KARAOKE if karaoke else subtitles.STYLE_CLIP,
        max_chars=subtitles.CLIP_MAX_CHARS_PER_LINE,
        max_lines=subtitles.CLIP_MAX_LINES,
        karaoke=karaoke,
    )

    mode, focus = _resolve_reframe(video_path, start, duration)
    filter_complex = _reframe_filter(mode, focus, ass_path)

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", "-i", str(video_path),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-r", "30", "-pix_fmt", "yuv420p",
        *_audio_args(),
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"render do corte falhou: {result.stderr.strip()[-800:]}")
    return output_path


def _audio_args() -> list[str]:
    """Args de audio comuns aos renders. Normaliza o volume quando ativado.

    loudnorm mira -14 LUFS (o alvo de YouTube/streaming): sem isso, cada corte sai
    com um volume, e um feed de cortes fica com gente subindo e descendo o som.
    """
    args = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"] if settings.audio_loudnorm else []
    return [*args, "-c:a", "aac", "-b:a", "128k", "-ar", "44100"]


def render_clip_wide(
    video_path: Path,
    segments: list[dict[str, Any]],
    clip: dict[str, Any],
    output_path: Path,
    work_dir: Path,
) -> Path:
    """Renderiza a versao horizontal 16:9 do mesmo trecho, para o YouTube comum.

    Mantem o quadro original encaixado em 1920x1080 (sem recorte) e queima a
    legenda no estilo do episodio. Serve para publicar o corte como video normal,
    nao como Short.
    """
    start, end = float(clip["start"]), float(clip["end"])
    duration = end - start

    ass_path = work_dir / f"clip_{output_path.stem}_wide.ass"
    subtitles.write_ass(
        _clip_segments(segments, start, end),
        ass_path,
        width=1920,
        height=1080,
        style=subtitles.STYLE_EPISODE,
    )

    # decrease + pad: o quadro inteiro cabe em 16:9; fonte de origem ja landscape
    # preenche exato, uma fonte mais estreita ganha faixas laterais em vez de corte.
    filter_complex = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
        f"subtitles='{subtitles._escape_for_filter(ass_path)}'[v]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", "-i", str(video_path),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-r", "30", "-pix_fmt", "yuv420p",
        *_audio_args(),
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"render do corte horizontal falhou: {result.stderr.strip()[-800:]}")
    return output_path


def make_thumbnail(video_path: Path, clip: dict[str, Any], output_path: Path,
                   vertical: bool = False) -> Path | None:
    """Extrai um frame do meio do trecho como thumbnail.

    Horizontal (padrao) sai 1280x720, para o YouTube. Vertical sai 1080x1920 com
    o MESMO enquadramento do corte — e a capa do Reels/TikTok/Short, entao usar o
    recorte 16:9 ali entregaria uma capa com a cabeca cortada.

    Nunca derruba o pipeline: e um extra, entao qualquer falha vira None e o corte
    segue sem thumbnail.
    """
    start, end = float(clip["start"]), float(clip["end"])
    duration = end - start
    meio = start + duration / 2

    cmd = ["ffmpeg", "-y", "-ss", f"{meio:.2f}", "-i", str(video_path), "-frames:v", "1"]
    if vertical:
        mode, track = _resolve_reframe(video_path, start, duration)
        # A capa e um frame so: usa o foco vigente naquele instante da trilha.
        focus = _focus_at(track, duration / 2)
        cmd += ["-filter_complex", _vertical_chain(mode, focus), "-map", "[framed]"]
    else:
        cmd += ["-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720"]
    cmd += ["-q:v", "3", str(output_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("ffmpeg indisponivel para thumbnail (%s)", exc)
        return None
    if result.returncode != 0 or not output_path.exists():
        log.warning("thumbnail do corte falhou: %s", result.stderr.strip()[-300:])
        return None
    return output_path
