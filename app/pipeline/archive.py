"""Etapa 6: organiza o episodio finalizado no acervo.

O acervo e uma pasta local. Se voce apontar `ARCHIVE_DIR` para a pasta
sincronizada do Google Drive / OneDrive, o backup na nuvem sai de graca.

Cada episodio vira uma pasta autocontida com video, legenda, transcricao e um
`meta.json` — que e tambem o registro de origem e licenca consultado pelo
catalogo do Telegram.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from app.config import settings

ARCHIVE_DIR = Path(os.getenv("ARCHIVE_DIR") or settings.data_dir / "archive")


def slugify(text: str, max_len: int = 60) -> str:
    text = (text or "sem-titulo").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return (text[:max_len].rstrip("-")) or "sem-titulo"


def archive_episode(episode: dict[str, Any], files: dict[str, Path]) -> Path:
    """Copia os artefatos finais para o acervo e devolve a pasta criada."""
    channel = slugify(episode.get("channel") or "sem-canal", 40)
    name = f"{episode['id']:05d}-{slugify(episode.get('title') or '')}"
    dest = ARCHIVE_DIR / channel / name
    dest.mkdir(parents=True, exist_ok=True)

    copied: dict[str, str] = {}
    for label, src in files.items():
        if not src:
            continue
        src = Path(src)
        if not src.exists():
            continue
        target = dest / f"{label}{src.suffix}"
        shutil.copy2(src, target)
        copied[label] = str(target)

    meta = {
        "id": episode["id"],
        "titulo": episode.get("title"),
        "canal": episode.get("channel"),
        "duracao_segundos": episode.get("duration"),
        "idioma_origem": episode.get("lang_src"),
        "idioma_destino": episode.get("lang_dst"),
        "url_origem": episode.get("source_url"),
        # Consultado antes de qualquer distribuicao paga.
        "licenca": episode.get("license_status", "unknown"),
        "arquivos": copied,
        "criado_em": episode.get("created_at"),
    }
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dest


def list_archive() -> list[dict[str, Any]]:
    """Le o acervo direto do disco — a fonte da verdade para o catalogo."""
    items = []
    if not ARCHIVE_DIR.exists():
        return items
    for meta_path in sorted(ARCHIVE_DIR.glob("*/*/meta.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        data["_dir"] = str(meta_path.parent)
        items.append(data)
    return items
