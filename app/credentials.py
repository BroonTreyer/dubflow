"""Cofre local das credenciais das plataformas, editavel pela tela de Conexoes.

Guardado em `data/credentials.json` — que ja fica fora do git (a pasta `data/`
inteira e ignorada) e com permissao restrita, como o `.secret_key`.

Regra de prioridade: o valor salvo pelo painel vence o do `.env`; quando o cofre
nao tem a chave, cai no `.env`. Assim da para conectar contas pelo painel sem
reiniciar e sem editar arquivo — mas quem ja usa `.env` continua funcionando.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

# Chaves que a tela de Conexoes gerencia, agrupadas por plataforma.
MANAGED: dict[str, list[str]] = {
    "youtube": ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_PRIVACY"],
    "instagram": ["IG_USER_ID", "IG_ACCESS_TOKEN", "PUBLIC_BASE_URL"],
    "tiktok": ["TIKTOK_ACCESS_TOKEN"],
    "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"],
}
ALL_KEYS: list[str] = [key for keys in MANAGED.values() for key in keys]

# Campos sensiveis: mascarados na tela e nunca devolvidos em texto claro.
SECRET_KEYS = {
    "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "IG_ACCESS_TOKEN", "TIKTOK_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN",
}


def _path() -> Path:
    return settings.data_dir / "credentials.json"


def load() -> dict[str, str]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("credentials.json invalido; ignorando o cofre")
        return {}
    return {k: str(v) for k, v in data.items() if isinstance(v, (str, int, float))}


def get(key: str) -> str:
    """Valor do cofre (prioridade) ou do .env. Sempre string, ja sem espacos."""
    stored = (load().get(key) or "").strip()
    if stored:
        return stored
    return (os.getenv(key) or "").strip()


def save(updates: dict[str, str]) -> None:
    """Mescla e grava com permissao restrita.

    Ignora chaves fora da allowlist e valores vazios — assim submeter um campo em
    branco NAO apaga o que ja estava (evita zerar um token por engano).
    """
    stored = load()
    for key, value in updates.items():
        if key not in ALL_KEYS:
            continue
        value = (value or "").strip()
        if value:
            stored[key] = value

    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)  # troca atomica: nunca deixa o arquivo pela metade


def clear(key: str) -> None:
    """Remove uma credencial do cofre (volta a valer o .env, se houver)."""
    stored = load()
    if key in stored:
        del stored[key]
        _path().write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")


def status() -> dict[str, dict[str, bool]]:
    """Por plataforma, quais chaves estao preenchidas (cofre ou .env)."""
    return {plat: {key: bool(get(key)) for key in keys} for plat, keys in MANAGED.items()}
