"""Cofre local das credenciais das plataformas, editavel pela tela de Conexoes.

Guardado em `data/credentials.json` — que ja fica fora do git (a pasta `data/`
inteira e ignorada) e com permissao restrita, como o `.secret_key`.

Regra de prioridade: o valor salvo pelo painel vence o do `.env`; quando o cofre
nao tem a chave, cai no `.env`. Assim da para conectar contas pelo painel sem
reiniciar e sem editar arquivo — mas quem ja usa `.env` continua funcionando.

Multi-conta: passando `channel_id`, o valor vem do cofre daquele canal
(`data/channels/<id>/credentials.json`). Credenciais de IDENTIDADE da conta
(tokens, user_id, channel_id do Telegram) NUNCA herdam do cofre global — senao um
canal sem credencial publicaria na conta errada. So chaves compartilhadas de
infra/preferencia (ver SHARED_KEYS) caem no global/.env como padrao.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

# Chaves que a tela de Conexoes gerencia, agrupadas por plataforma.
MANAGED: dict[str, list[str]] = {
    "youtube": ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_PRIVACY"],
    "instagram": ["IG_USER_ID", "IG_ACCESS_TOKEN", "PUBLIC_BASE_URL"],
    "tiktok": ["TIKTOK_ACCESS_TOKEN"],
    "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID", "TELEGRAM_VIP_CHAT_ID"],
    "pix": ["ABACATEPAY_TOKEN", "PUSHINPAY_TOKEN"],
}
ALL_KEYS: list[str] = [key for keys in MANAGED.values() for key in keys]

# Campos sensiveis: mascarados na tela e nunca devolvidos em texto claro.
SECRET_KEYS = {
    "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "IG_ACCESS_TOKEN", "TIKTOK_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN",
    "PUSHINPAY_TOKEN", "ABACATEPAY_TOKEN",
}

# Chaves compartilhadas entre canais (infra do servidor / preferencia / credencial
# do APP, nao identidade da CONTA): podem herdar do cofre global/.env quando o canal
# nao as define. Todas as demais sao identidade da conta e, com channel_id, NUNCA
# herdam.
#
# YOUTUBE_CLIENT_ID/SECRET identificam o app (o projeto no Google Cloud), nao a
# conta — a conta e o YOUTUBE_REFRESH_TOKEN, que fica por canal. Compartilha-los
# evita recolar as credenciais do app em cada canal e NAO muda para qual conta
# publica (isso e decidido pelo refresh token, sempre por canal).
SHARED_KEYS = {
    "PUBLIC_BASE_URL", "YOUTUBE_PRIVACY",
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
}


def _path(channel_id: int | None = None) -> Path:
    if channel_id is None:
        return settings.data_dir / "credentials.json"
    return settings.data_dir / "channels" / str(channel_id) / "credentials.json"


def load(channel_id: int | None = None) -> dict[str, str]:
    path = _path(channel_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("cofre de credenciais invalido (%s); ignorando", path.name)
        return {}
    return {k: str(v) for k, v in data.items() if isinstance(v, (str, int, float))}


def get(key: str, channel_id: int | None = None) -> str:
    """Valor do cofre (prioridade) ou do .env. Sempre string, ja sem espacos.

    Com channel_id, le o cofre do canal. Se o canal nao tem a chave, so herda do
    cofre global/.env quando ela e compartilhada (SHARED_KEYS); identidade da conta
    fica vazia — publicar na conta errada e pior que falhar por falta de credencial.
    """
    if channel_id is not None:
        stored = (load(channel_id).get(key) or "").strip()
        if stored:
            return stored
        if key not in SHARED_KEYS:
            return ""
    stored = (load().get(key) or "").strip()
    if stored:
        return stored
    return (os.getenv(key) or "").strip()


def save(updates: dict[str, str], channel_id: int | None = None) -> None:
    """Mescla e grava com permissao restrita, no cofre global ou do canal.

    Ignora chaves fora da allowlist e valores vazios — assim submeter um campo em
    branco NAO apaga o que ja estava (evita zerar um token por engano).
    """
    stored = load(channel_id)
    for key, value in updates.items():
        if key not in ALL_KEYS:
            continue
        value = (value or "").strip()
        if value:
            stored[key] = value

    path = _path(channel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)  # troca atomica: nunca deixa o arquivo pela metade


def clear(key: str, channel_id: int | None = None) -> None:
    """Remove uma credencial do cofre (volta a valer o padrao, se houver)."""
    stored = load(channel_id)
    if key in stored:
        del stored[key]
        _path(channel_id).write_text(
            json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def clear_channel(channel_id: int) -> None:
    """Apaga o cofre inteiro de um canal (ao remover o canal)."""
    d = _path(channel_id).parent
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def status(channel_id: int | None = None) -> dict[str, dict[str, bool]]:
    """Por plataforma, quais chaves estao preenchidas (cofre ou .env)."""
    return {plat: {key: bool(get(key, channel_id)) for key in keys}
            for plat, keys in MANAGED.items()}
