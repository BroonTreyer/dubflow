"""Contrato comum dos publishers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class PublishResult:
    ok: bool
    remote_id: str | None = None
    permalink: str | None = None
    error: str | None = None


class Publisher(Protocol):
    name: str

    def configured(self) -> bool:
        """True quando ha credenciais suficientes para publicar."""

    def publish(self, video_path: Path, caption: str) -> PublishResult:
        """Publica o video e devolve o resultado."""
