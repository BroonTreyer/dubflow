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
from pathlib import Path
from typing import Any

import anthropic

from app.config import settings
from app.pipeline import subtitles

log = logging.getLogger(__name__)

ASSETS = Path(__file__).parent / "assets"

# Detectores de rosto, carregados uma vez. YuNet (DNN) e melhor — pega rosto de
# lado e em angulo; o Haar frontal fica de reserva. Ambos tropecam em caminho com
# acento no Windows, entao sao carregados de forma que contorna isso (ver abaixo).
_CASCADE: Any = None
_CASCADE_LOADED = False
_YUNET: Any = None
_YUNET_LOADED = False

SELECTION_PROMPT = """\
Voce e editor de conteudo social. Recebe a transcricao de um episodio com \
timestamps e escolhe os trechos que funcionam como video vertical independente \
(Reels, TikTok, Shorts).

O QUE FAZ UM CORTE FUNCIONAR

- Ele se sustenta sozinho. Quem nunca viu o episodio entende sem contexto externo.
- Os primeiros 3 segundos ja entregam tensao, contradicao, numero surpreendente ou \
uma afirmacao forte. Um trecho que comeca com "entao, como eu estava dizendo" nao serve.
- Tem inicio e fim proprios: uma ideia completa, nao um pedaco de raciocinio cortado.
- Diz algo que a pessoa teria vontade de mandar para alguem.

O QUE NAO SERVE

- Apresentacoes, agradecimentos, "se inscreva no canal", leitura de patrocinio.
- Trechos que dependem de imagem que voce nao viu (referencia a grafico na tela).
- Conversa de transicao entre assuntos.

REGRAS

- Escolha exatamente {count} trechos, ou menos se o episodio nao tiver material bom. \
Nao complete a cota com trecho fraco.
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
- `score`: 0 a 10, seu grau de confianca de que o trecho performa.

Responda apenas com o JSON do schema.
"""

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
                    "score": {"type": "number"},
                },
                "required": ["start", "end", "title", "hook", "caption",
                             "yt_title", "yt_description", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def select_clips(
    segments: list[dict[str, Any]],
    meta: dict[str, Any],
    count: int | None = None,
) -> list[dict[str, Any]]:
    """Pede a Claude os melhores trechos do episodio."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY nao configurada.")

    count = count or settings.clips_per_episode
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    transcript = [
        {"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s.get("text") or ""}
        for s in segments
        if (s.get("text") or "").strip()
    ]

    system = SELECTION_PROMPT.format(
        count=count, min_s=settings.clip_min_seconds, max_s=settings.clip_max_seconds
    )
    user = (
        f"Episodio: {meta.get('title')}\nCanal: {meta.get('channel')}\n\n"
        "Transcricao com timestamps (segundos):\n"
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
        log.warning("selecao de cortes recusada pelos classificadores")
        return []

    text = next((b.text for b in response.content if b.type == "text"), "")
    clips = json.loads(text).get("clips", [])
    return _sanitize(clips, segments)


def _sanitize(clips: list[dict[str, Any]], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Encaixa cada corte nas fronteiras reais de fala e remove sobreposicao."""
    if not segments:
        return []
    starts = sorted({float(s["start"]) for s in segments})
    ends = sorted({float(s["end"]) for s in segments})
    limit = max(ends)

    def nearest(values: list[float], target: float) -> float:
        return min(values, key=lambda v: abs(v - target))

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
                "score": float(clip.get("score") or 0),
            }
        )

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


def _face_centers(img: Any) -> list[tuple[float, float]]:
    """Devolve (centro_x_px, area) de cada rosto no frame — YuNet, senao Haar."""
    import cv2

    h, w = img.shape[:2]
    yunet = _load_yunet()
    if yunet is not None:
        yunet.setInputSize((w, h))
        _, faces = yunet.detect(img)
        if faces is None:
            return []
        return [(float(f[0] + f[2] / 2), float(f[2] * f[3])) for f in faces]

    cascade = _load_cascade()
    if cascade is None:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5,
        minSize=(int(h * 0.08), int(h * 0.08)),
    )
    return [(float(x + fw / 2), float(fw * fh)) for (x, _y, fw, fh) in faces]


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


def _detect_focus(video_path: Path, start: float, duration: float) -> float | None:
    """Amostra frames do trecho e devolve onde a janela 9:16 deve focar (0..1).

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

    n = max(4, min(12, int(duration / 3)))
    rate = n / max(duration, 1.0)
    with tempfile.TemporaryDirectory(prefix="dubflow_focus_") as td:
        pattern = str(Path(td) / "f_%03d.jpg")
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

        soma_cx = 0.0
        peso = 0.0
        frame_w = frame_h = 0
        for frame in sorted(Path(td).glob("f_*.jpg")):
            img = cv2.imread(str(frame))
            if img is None:
                continue
            frame_h, frame_w = img.shape[:2]
            for cx, area in _face_centers(img):
                # Pondera pela area: o rosto maior manda no enquadramento, o que
                # mantem o corte estavel quando ha um rosto de fundo pequeno.
                soma_cx += cx * area
                peso += area

    if peso <= 0 or frame_w == 0:
        return None
    return _focus_from_center((soma_cx / peso) / frame_w, frame_w, frame_h)


def _reframe_filter(mode: str, focus: float, ass_path: Path) -> str:
    """Monta o filtro do ffmpeg que leva o video a 9:16 e queima a legenda."""
    sub = f"subtitles='{subtitles._escape_for_filter(ass_path)}'"
    if mode == "pad":
        # Legado: o video inteiro encolhido no meio de um fundo borrado. Nao foca
        # na cena — a faixa util fica pequena entre duas barras desfocadas.
        return (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=22:2[bg];"
            "[0:v]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[framed];"
            f"[framed]{sub}[v]"
        )
    # face/center: recorta uma janela 9:16 que preenche a tela. `focus` desliza a
    # janela na horizontal (0=esquerda, 0.5=centro, 1=direita); (in_w-out_w) e a
    # folga real, entao o corte nunca sai do quadro.
    return (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920:x='(in_w-out_w)*{focus:.4f}':y='(in_h-out_h)/2'[framed];"
        f"[framed]{sub}[v]"
    )


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

    mode = settings.clip_reframe
    focus = 0.5
    if mode == "face":
        detected = _detect_focus(video_path, start, duration)
        # Sem rosto (ou sem detector), cai para o centro: preenche a tela do mesmo
        # jeito, so nao segue o rosto.
        mode = "center" if detected is None else "face"
        focus = 0.5 if detected is None else detected

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


def make_thumbnail(video_path: Path, clip: dict[str, Any], output_path: Path) -> Path | None:
    """Extrai um frame do meio do trecho como thumbnail 1280x720 (16:9).

    Nunca derruba o pipeline: e um extra, entao qualquer falha vira None e o corte
    segue sem thumbnail.
    """
    start, end = float(clip["start"]), float(clip["end"])
    meio = start + (end - start) / 2
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{meio:.2f}", "-i", str(video_path), "-frames:v", "1",
        "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
        "-q:v", "3", str(output_path),
    ]
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
