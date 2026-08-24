"""Etapa 4: geracao de SRT/ASS e queima da legenda no video.

Duas saidas diferentes:

- **SRT** — legenda separada, para o arquivo e para subir como faixa no YouTube.
- **ASS** — usada na queima. Permite estilo (contorno, sombra, posicao), que e o
  que faz a legenda continuar legivel sobre qualquer imagem.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterable

MAX_CHARS_PER_LINE = 42
MAX_LINES = 2

# O corte vertical tem menos da metade da largura util do episodio e uma fonte
# muito maior, entao cabe bem menos texto por linha. Com o limite do episodio a
# legenda vazaria para fora do quadro. Em compensacao ha altura sobrando, por
# isso ele aceita tres linhas.
# 22 caracteres e o que cabe em 940px uteis (1080 menos as margens) com Arial
# Black 68. Linhas mais cheias significam menos linhas: a mesma frase que ocupava
# quatro linhas passa a ocupar tres, sobrando imagem visivel.
CLIP_MAX_CHARS_PER_LINE = 22
CLIP_MAX_LINES = 3

# Os tamanhos sao relativos ao PlayRes declarado no arquivo, nao a pixels da
# tela. Como o PlayResY do episodio e 1080 e o do corte e 1920, o mesmo numero
# significa tamanhos diferentes nos dois — e foi por isso que a primeira versao
# saiu ilegivel. A referencia usada aqui: a altura da fonte deve ficar por volta
# de 5% da altura do quadro (~54 em 1080, ~76 em 1920).
#
# Campos, em ordem: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,
# OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,
# Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
#
# Alignment usa a disposicao do teclado numerico: 2 = inferior centralizado,
# 5 = centro da tela. Precisa ser 2 nos dois estilos.

# Episodio completo (PlayRes 1920x1080): legivel sem tapar a imagem.
STYLE_EPISODE = (
    "Style: Default,Arial,52,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,"
    "-1,0,0,0,100,100,0,0,1,3.2,1.6,2,60,60,58,1"
)

# Cortes verticais (PlayRes 1080x1920): fonte grande, contorno forte e margem
# inferior alta o bastante para a legenda nao ficar atras da interface do
# TikTok/Reels, que ocupa a faixa de baixo do quadro.
STYLE_CLIP = (
    "Style: Default,Arial Black,68,&H00FFFFFF,&H000000FF,&H00000000,&HC0000000,"
    "-1,0,0,0,100,100,0,0,1,5.0,2.5,2,70,70,260,1"
)


def _fmt_srt(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_ass(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _greedy_wrap(words: list[str], width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def wrap_text(text: str, max_chars: int = MAX_CHARS_PER_LINE, max_lines: int = MAX_LINES) -> str:
    """Quebra em linhas equilibradas de ate `max_chars`, sem cortar palavra.

    `max_lines` e um alvo, nao um teto rigido: quando o texto nao cabe em
    `max_lines * max_chars`, e melhor abrir uma linha extra do que estourar a
    largura — o renderizador ASS nao re-quebra, entao uma linha larga demais sai
    cortada na tela.

    O reequilibrio evita o resultado feio do greedy puro (uma linha cheia e
    outra com duas palavras): procura a menor largura que ainda produz o mesmo
    numero de linhas.
    """
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text

    words = text.split(" ")
    lines = _greedy_wrap(words, max_chars)

    target = min(len(lines), max_lines) if len(lines) > max_lines else len(lines)
    # A largura util nunca fica abaixo da maior palavra isolada.
    floor = max(len(max(words, key=len)), (len(text) + target - 1) // target)
    for width in range(floor, max_chars + 1):
        candidate = _greedy_wrap(words, width)
        if len(candidate) <= target:
            lines = candidate
            break

    return "\n".join(lines)


def escape_ass(text: str) -> str:
    """Neutraliza a sintaxe do ASS dentro do texto da legenda.

    No ASS, `{...}` delimita override tags e `\\` inicia escapes (`\\N`, `\\h`).
    Uma traducao que mencione JSON, LaTeX ou um trecho de codigo sumiria da tela
    sem erro nenhum. Os substitutos sao visualmente equivalentes.
    """
    return (
        text.replace("\\", "⧵")  # barra invertida "de exibicao"
        .replace("{", "｛")       # chaves de largura total
        .replace("}", "｝")
    )


def _usable(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start, end = float(seg["start"]), float(seg["end"])
        if end <= start:
            end = start + 1.2
        out.append({"start": start, "end": end, "text": text})
    return out


def write_srt(segments: list[dict[str, Any]], path: Path) -> Path:
    lines = []
    for i, seg in enumerate(_usable(segments), start=1):
        lines.append(str(i))
        lines.append(f"{_fmt_srt(seg['start'])} --> {_fmt_srt(seg['end'])}")
        lines.append(wrap_text(seg["text"]))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_ass(
    segments: list[dict[str, Any]],
    path: Path,
    width: int = 1920,
    height: int = 1080,
    style: str = STYLE_EPISODE,
    max_chars: int = MAX_CHARS_PER_LINE,
    max_lines: int = MAX_LINES,
) -> Path:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
{style}

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    events = []
    for seg in _usable(segments):
        # Escapa antes de quebrar: o \N da quebra de linha e sintaxe nossa, nao do texto.
        text = wrap_text(escape_ass(seg["text"]), max_chars, max_lines).replace("\n", "\\N")
        events.append(
            f"Dialogue: 0,{_fmt_ass(seg['start'])},{_fmt_ass(seg['end'])},Default,,0,0,0,,{text}"
        )
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path


def _escape_for_filter(path: Path) -> str:
    """Escapa o caminho para o filtro `subtitles` do ffmpeg.

    No Windows o filtro precisa de barras normais e do `:` do drive escapado,
    senao o ffmpeg interpreta `C:` como separador de opcao do filtro. A aspa
    simples tambem escapa porque o valor inteiro vai entre aspas simples.
    """
    return (
        str(path.resolve())
        .replace("\\", "/")
        .replace(":", "\\:")
        .replace("'", r"\'")
    )


def burn(video_path: Path, ass_path: Path, output_path: Path, crf: int = 20) -> Path:
    """Queima a legenda ASS no video."""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vf", f"subtitles='{_escape_for_filter(ass_path)}'",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"queima de legenda falhou: {result.stderr.strip()[-800:]}")
    return output_path
