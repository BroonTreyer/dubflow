"""Registro de publishers disponiveis."""

from __future__ import annotations

from pathlib import Path

from app.publishers import instagram, telegram, tiktok, youtube
from app.publishers.base import PublishResult

REGISTRY = {
    "youtube": youtube,
    "instagram": instagram,
    "tiktok": tiktok,
    "telegram": telegram,
}


def publish(platform: str, video_path: Path, caption: str,
            title: str | None = None, thumb_path: Path | None = None) -> PublishResult:
    module = REGISTRY.get(platform)
    if module is None:
        return PublishResult(False, error=f"plataforma desconhecida: {platform}")
    return module.publish(Path(video_path), caption, title, thumb_path)


def status() -> dict[str, bool]:
    return {plat: mod.configured() for plat, mod in REGISTRY.items()}


def stats_for(platform: str, remote_id: str) -> dict[str, int | None] | None:
    """Metricas (views/likes/comments) de uma publicacao, se a plataforma suportar."""
    module = REGISTRY.get(platform)
    fetch = getattr(module, "stats", None) if module else None
    if fetch is None:
        return None
    return fetch(remote_id)
