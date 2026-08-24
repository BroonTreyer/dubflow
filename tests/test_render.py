"""Teste de render: valida o filtro 9:16 e a queima de legenda com ffmpeg real.

Usa um video sintetico (testsrc), entao nao depende de rede, GPU nem da API.
Cobre a parte mais fragil no Windows: o escape do caminho da legenda dentro do
filtro `subtitles` do ffmpeg.

    py -m tests.test_render
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.pipeline import clips, subtitles

TMP = Path(__file__).parent / "_tmp"


def make_source(path: Path, seconds: int = 12) -> Path:
    """Gera um MP4 sintetico 1280x720 com audio."""
    if path.exists():
        return path
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"nao consegui gerar o video de teste: {result.stderr[-500:]}")
    return path


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, errors="replace",
    )
    return json.loads(out.stdout)


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    source = make_source(TMP / "fonte.mp4")
    print(f"fonte: {source.name} ({source.stat().st_size // 1024} KB)")

    segments = [
        {"start": 0.5, "end": 3.0, "text": "Primeira fala do corte, com acentuacao: coracao e emocao."},
        {"start": 3.2, "end": 6.0, "text": "Segunda fala, mais longa, para forcar a quebra de linha na tela."},
        {"start": 6.2, "end": 9.5, "text": "Terceira e ultima fala do trecho selecionado."},
    ]
    clip = {"start": 0.0, "end": 10.0, "title": "teste", "hook": "", "caption": "", "score": 9}

    output = TMP / "corte_teste.mp4"
    if output.exists():
        output.unlink()

    print("renderizando corte vertical...")
    clips.render_clip(source, segments, clip, output, TMP)

    info = probe(output)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    duration = float(info["format"]["duration"])

    failures = []

    def check(label: str, condition: bool, detail: object = "") -> None:
        if condition:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label} -> {detail}")
            failures.append(label)

    check("resolucao 1080x1920", (video["width"], video["height"]) == (1080, 1920),
          f'{video["width"]}x{video["height"]}')
    check("codec h264", video["codec_name"] == "h264", video["codec_name"])
    check("pixel format yuv420p", video["pix_fmt"] == "yuv420p", video["pix_fmt"])
    check("audio preservado", len(audio) == 1, len(audio))
    check("duracao ~10s", 9.0 < duration < 11.0, duration)
    check("arquivo com conteudo", output.stat().st_size > 50_000, output.stat().st_size)

    # A legenda ASS do corte precisa ter sido escrita com os timestamps rebaseados.
    ass = TMP / f"clip_{output.stem}.ass"
    check("ass gerado", ass.exists())
    if ass.exists():
        content = ass.read_text(encoding="utf-8")
        check("3 dialogos", content.count("Dialogue:") == 3, content.count("Dialogue:"))
        check("acentuacao preservada", "coracao" in content)
        check("primeiro dialogo em 0:00:00.50", "0:00:00.50" in content, content[-400:])

    print()
    if failures:
        print(f"{len(failures)} falha(s): {', '.join(failures)}")
        return 1
    print(f"render OK -> {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
