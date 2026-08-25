"""Configuracao central do dubflow, carregada de .env."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
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


def _float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name, "").strip() or default))
    except ValueError:
        return default


@dataclass
class Settings:
    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR") or ROOT / "data"))
    db_path: Path = field(init=False)
    # Cookie de sessao exige HTTPS. Liga sozinho quando o painel nao e local (ao
    # expor por tunel para o Instagram, o tunel ja da HTTPS); COOKIE_SECURE forca.
    cookie_secure: bool = field(init=False)

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
    # Modelo do passe de reconhecimento (genero/publico do episodio). E uma
    # leitura rasa sobre uma amostra, entao roda no Haiku: barato e suficiente.
    clip_scan_model: str = os.getenv("CLIP_SCAN_MODEL", "claude-haiku-4-5-20251001")
    use_batch_api: bool = _bool("USE_BATCH_API", False)

    # --- cortes ---
    # A cota e proporcional a duracao: um episodio de 2h nao pode receber a mesma
    # cota de um de 15 min. CLIPS_PER_EPISODE virou o piso (video curto ainda
    # rende esse minimo) e CLIPS_MAX o teto de seguranca do render.
    clips_per_hour: int = _int("CLIPS_PER_HOUR", 20)
    clips_per_episode: int = _int("CLIPS_PER_EPISODE", 5)
    clips_max: int = _int("CLIPS_MAX", 80)
    # Tamanho da janela de analise: o modelo escolhe os cortes olhando um trecho
    # por vez, em vez de varrer 2h de uma so tacada (a atencao se dilui e a
    # selecao piora). Janelas rodam em paralelo.
    clip_window_minutes: int = _int("CLIP_WINDOW_MINUTES", 20)
    clip_min_seconds: int = _int("CLIP_MIN_SECONDS", 25)
    clip_max_seconds: int = _int("CLIP_MAX_SECONDS", 75)
    # Enquadramento 9:16: face (recorta focando no rosto) | center | pad (legado).
    clip_reframe: str = os.getenv("CLIP_REFRAME", "face")
    # Legenda karaoke (palavra destacada no tempo da fala) nos cortes verticais.
    clip_karaoke: bool = _bool("CLIP_KARAOKE", True)
    # Renderiza tambem a versao horizontal 16:9 do corte (para o YouTube comum).
    clip_render_wide: bool = _bool("CLIP_RENDER_WIDE", True)
    # Gera thumbnail 16:9 de cada corte.
    clip_thumbnail: bool = _bool("CLIP_THUMBNAIL", True)
    # Normaliza o volume (loudnorm -14 LUFS) no render dos cortes.
    audio_loudnorm: bool = _bool("AUDIO_LOUDNORM", True)

    # --- video ---
    burn_full_episode: bool = _bool("BURN_FULL_EPISODE", False)
    max_height: int = _int("MAX_HEIGHT", 1080)
    # O audio extraido sempre e apagado apos o arquivamento; o video de origem so
    # se voce pedir (mantê-lo permite re-render de cortes sem baixar de novo).
    delete_source_after_archive: bool = _bool("DELETE_SOURCE_AFTER_ARCHIVE", False)

    # --- distribuicao automatica (roteia cortes -> canal do segmento + gotejamento) ---
    # Liga o passo automatico no fim do pipeline. Sem canais configurados ele nao
    # faz nada, entao e seguro deixar ligado por padrao.
    auto_distribute: bool = _bool("AUTO_DISTRIBUTE", True)
    # Cortes por dia por canal no agendamento (padrao; cada canal pode sobrescrever).
    distribute_per_day: int = _int("DISTRIBUTE_PER_DAY", 3)
    # Janela de postagem do dia, em HORA UTC (BR = UTC-3): os slots do dia sao
    # distribuidos entre start e end.
    distribute_start_hour: int = _int("DISTRIBUTE_START_HOUR", 9)
    distribute_end_hour: int = _int("DISTRIBUTE_END_HOUR", 21)
    # Confianca minima da classificacao para rotear sozinho; abaixo disso, fica
    # nao-atribuido esperando decisao manual (nunca chuta o canal).
    distribute_min_confidence: float = _float("DISTRIBUTE_MIN_CONFIDENCE", 0.6)

    # --- publicacao ---
    ig_user_id: str = os.getenv("IG_USER_ID", "")
    ig_access_token: str = os.getenv("IG_ACCESS_TOKEN", "")
    tiktok_access_token: str = os.getenv("TIKTOK_ACCESS_TOKEN", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")  # url publica dos MP4 (Instagram exige)

    # --- youtube (OAuth2: refresh token de longa duracao, ver scripts/youtube_auth.py) ---
    youtube_client_id: str = os.getenv("YOUTUBE_CLIENT_ID", "")
    youtube_client_secret: str = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    youtube_refresh_token: str = os.getenv("YOUTUBE_REFRESH_TOKEN", "")
    # Padrao 'private': o Short sobe oculto ate voce liberar. Aceita private/unlisted/public.
    youtube_privacy: str = os.getenv("YOUTUBE_PRIVACY", "private")
    youtube_category_id: str = os.getenv("YOUTUBE_CATEGORY_ID", "22")  # 22 = People & Blogs

    # --- telegram ---
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_channel_id: str = os.getenv("TELEGRAM_CHANNEL_ID", "")

    # --- venda no telegram (pagamento manual: voce confirma o Pix no painel) ---
    pix_key: str = os.getenv("PIX_KEY", "")               # sua chave Pix, mostrada ao comprador
    pix_name: str = os.getenv("PIX_NAME", "")             # nome do recebedor (aparece pro comprador)
    price_episode: float = _float("PRICE_EPISODE", 9.90)  # valor por episodio avulso (BRL)
    price_subscription: float = _float("PRICE_SUBSCRIPTION", 29.90)  # valor da assinatura (BRL)
    subscription_days: int = _int("SUBSCRIPTION_DAYS", 30)  # duracao da assinatura

    # --- servidor ---
    port: int = _int("PORT", 8030)
    # Padrao local: o painel controla contas sociais e o acervo. Expor na rede
    # exige senha (ver app/security.py) e e decisao explicita.
    host: str = os.getenv("HOST", "127.0.0.1")

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.db_path = self.data_dir / "dubflow.db"
        self.cookie_secure = _bool("COOKIE_SECURE", self.host not in ("127.0.0.1", "localhost"))
        for sub in ("episodes", "archive", "tmp", "logs"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

    def episode_dir(self, episode_id: int) -> Path:
        path = self.data_dir / "episodes" / f"ep_{episode_id:05d}"
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()


def configure_logging(component: str, level: int = logging.INFO) -> None:
    """Loga no console e tambem em data/logs/<component>.log, com rotacao.

    O worker roda headless numa janela separada; sem arquivo, o historico de
    falhas some quando a janela fecha — e uma transcricao que trava de madrugada
    fica sem rastro. O arquivo preserva o diagnostico. Rotaciona em 5 MB (3 backups)
    para nao encher o disco.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_dir / f"{component}.log",
                maxBytes=5_000_000, backupCount=3, encoding="utf-8",
            )
        )
    except OSError:
        pass  # sem arquivo, ao menos o console continua

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,  # substitui qualquer basicConfig anterior
    )
