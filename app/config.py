"""Configuracao central do dubflow, carregada de .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "sim"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class Settings:
    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR") or ROOT / "data"))
    db_path: Path = field(init=False)

    # --- pipeline ---
    whisper_model: str = os.getenv("WHISPER_MODEL", "large-v3")
    whisper_device: str = os.getenv("WHISPER_DEVICE", "cuda")
    whisper_compute: str = os.getenv("WHISPER_COMPUTE", "float16")
    source_lang: str = os.getenv("SOURCE_LANG", "")  # "" = autodetect
    target_lang: str = os.getenv("TARGET_LANG", "pt-BR")

    # --- Claude ---
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    translate_model: str = os.getenv("TRANSLATE_MODEL", "claude-opus-5")
    translate_effort: str = os.getenv("TRANSLATE_EFFORT", "medium")
    clip_model: str = os.getenv("CLIP_MODEL", "claude-opus-5")
    use_batch_api: bool = _bool("USE_BATCH_API", False)

    # --- cortes ---
    clips_per_episode: int = _int("CLIPS_PER_EPISODE", 5)
    clip_min_seconds: int = _int("CLIP_MIN_SECONDS", 25)
    clip_max_seconds: int = _int("CLIP_MAX_SECONDS", 75)

    # --- video ---
    burn_full_episode: bool = _bool("BURN_FULL_EPISODE", False)
    max_height: int = _int("MAX_HEIGHT", 1080)
    # O audio extraido sempre e apagado apos o arquivamento; o video de origem so
    # se voce pedir (mantê-lo permite re-render de cortes sem baixar de novo).
    delete_source_after_archive: bool = _bool("DELETE_SOURCE_AFTER_ARCHIVE", False)

    # --- publicacao ---
    ig_user_id: str = os.getenv("IG_USER_ID", "")
    ig_access_token: str = os.getenv("IG_ACCESS_TOKEN", "")
    tiktok_access_token: str = os.getenv("TIKTOK_ACCESS_TOKEN", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")  # url publica dos MP4 (Instagram exige)

    # --- telegram ---
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_channel_id: str = os.getenv("TELEGRAM_CHANNEL_ID", "")

    # --- servidor ---
    port: int = _int("PORT", 8030)
    # Padrao local: o painel controla contas sociais e o acervo. Expor na rede
    # exige senha (ver app/security.py) e e decisao explicita.
    host: str = os.getenv("HOST", "127.0.0.1")

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.db_path = self.data_dir / "dubflow.db"
        for sub in ("episodes", "archive", "tmp", "logs"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

    def episode_dir(self, episode_id: int) -> Path:
        path = self.data_dir / "episodes" / f"ep_{episode_id:05d}"
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
