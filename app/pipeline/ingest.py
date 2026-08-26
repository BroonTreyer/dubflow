"""Etapa 1: baixa o video de origem e extrai metadados + audio para ASR."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.config import settings

# Invocar pelo interpretador em execucao, e nao pelo .exe do venv, mantem o
# ingest funcionando em qualquer instalacao (venv em outro caminho, instalacao
# global, execucao empacotada).
YTDLP = [sys.executable, "-m", "yt_dlp"]


def probe(url: str) -> dict[str, Any]:
    """Le os metadados sem baixar o video."""
    out = subprocess.run(
        [*YTDLP, "--dump-single-json", "--no-playlist", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        raise RuntimeError(f"yt-dlp falhou ao ler {url}: {out.stderr.strip()[:500]}")
    info = json.loads(out.stdout)
    return {
        "video_id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or url,
        "description": (info.get("description") or "")[:4000],
        "upload_date": info.get("upload_date"),
        "language": info.get("language"),
        # Atribuicao da fonte. Creditar canal e video original e o que separa
        # "corte com credito" de "reupload" aos olhos de quem denuncia — e do
        # sistema de conteudo reaproveitado da plataforma. No YouTube atual o
        # uploader_id ja vem como @handle.
        "uploader_id": info.get("uploader_id"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
    }


def download(url: str, dest_dir: Path) -> Path:
    """Baixa o melhor MP4 ate MAX_HEIGHT. Devolve o caminho do arquivo."""
    target = dest_dir / "source.%(ext)s"
    fmt = (
        f"bestvideo[height<={settings.max_height}][ext=mp4]+bestaudio[ext=m4a]"
        f"/best[height<={settings.max_height}][ext=mp4]/best"
    )
    cmd = [
        *YTDLP,
        "--no-playlist",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", str(target),
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"download falhou: {result.stderr.strip()[-800:]}")

    for candidate in sorted(dest_dir.glob("source.*")):
        if candidate.suffix.lower() in {".mp4", ".mkv", ".webm"}:
            return candidate
    raise RuntimeError("download concluiu mas nenhum arquivo de video foi encontrado")


def extract_audio(video_path: Path) -> Path:
    """Extrai WAV 16kHz mono — formato que o Whisper consome sem reamostrar."""
    audio_path = video_path.with_name("audio.wav")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"extracao de audio falhou: {result.stderr.strip()[-800:]}")
    return audio_path
