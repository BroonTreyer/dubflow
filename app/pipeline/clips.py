"""Etapa 5: selecao e renderizacao dos cortes verticais 9:16.

A selecao e feita por Claude sobre a transcricao traduzida com timestamps. O
modelo devolve trechos que se sustentam sozinhos — que e o criterio real de um
corte que funciona no feed, nao "o trecho onde alguem falou alto".
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import anthropic

from app.config import settings
from app.pipeline import subtitles

log = logging.getLogger(__name__)

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
                    "score": {"type": "number"},
                },
                "required": ["start", "end", "title", "hook", "caption", "score"],
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
        out.append(
            {
                "start": max(0.0, float(seg["start"]) - start),
                "end": min(end - start, float(seg["end"]) - start),
                "text": text,
            }
        )
    return out


def render_clip(
    video_path: Path,
    segments: list[dict[str, Any]],
    clip: dict[str, Any],
    output_path: Path,
    work_dir: Path,
) -> Path:
    """Corta o trecho, converte para 9:16 e queima a legenda social.

    O reframe usa `crop` centralizado sobre um fundo desfocado do proprio video:
    o assunto continua legivel e o quadro nao fica com barras pretas.
    """
    start, end = float(clip["start"]), float(clip["end"])
    duration = end - start

    ass_path = work_dir / f"clip_{output_path.stem}.ass"
    subtitles.write_ass(
        _clip_segments(segments, start, end),
        ass_path,
        width=1080,
        height=1920,
        style=subtitles.STYLE_CLIP,
        max_chars=subtitles.CLIP_MAX_CHARS_PER_LINE,
        max_lines=subtitles.CLIP_MAX_LINES,
    )

    # Fundo: o video ampliado e desfocado ocupando 1080x1920.
    # Frente: o video inteiro encaixado na largura, centralizado na vertical.
    filter_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=22:2[bg];"
        "[0:v]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[framed];"
        f"[framed]subtitles='{subtitles._escape_for_filter(ass_path)}'[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", "-i", str(video_path),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-r", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"render do corte falhou: {result.stderr.strip()[-800:]}")
    return output_path
