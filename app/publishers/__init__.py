"""Registro de publishers disponiveis."""

from __future__ import annotations

from pathlib import Path

from app.publishers import instagram, telegram, tiktok
from app.publishers.base import PublishResult

REGISTRY = {
    "instagram": instagram,
    "tiktok": tiktok,
    "telegram": telegram,
}


def publish(platform: str, video_path: Path, caption: str) -> PublishResult:
    module = REGISTRY.get(platform)
    if module is None:
        return PublishResult(False, error=f"plataforma desconhecida: {platform}")
    return module.publish(Path(video_path), caption)


def status() -> dict[str, bool]:
    return {plat: mod.configured() for plat, mod in REGISTRY.items()}
