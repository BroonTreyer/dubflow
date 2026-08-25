"""Telegram: distribuicao de cortes e catalogo do acervo.

Duas funcoes distintas:

- `send_clip` — manda um corte para o canal. Serve para qualquer episodio; e
  divulgacao, mesmo papel do Reels.
- `catalog` / `deliver_episode` — o acervo vendido por assinatura. Aqui existe um
  filtro: so entra episodio com licenca `licensed`, `owned` ou `public_domain`.
  Episodio `unknown` fica visivel no seu painel e fora do catalogo pago.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

from app import credentials
from app.pipeline import archive
from app.publishers.base import PublishResult

log = logging.getLogger(__name__)

API = "https://api.telegram.org"

# Licencas que autorizam distribuicao comercial do episodio completo.
SELLABLE_LICENSES = {"licensed", "owned", "public_domain"}

name = "telegram"


def configured(channel_id: int | None = None) -> bool:
    return bool(credentials.get("TELEGRAM_BOT_TOKEN", channel_id)
                and credentials.get("TELEGRAM_CHANNEL_ID", channel_id))


def _url(method: str, channel_id: int | None = None) -> str:
    return f"{API}/bot{credentials.get("TELEGRAM_BOT_TOKEN", channel_id)}/{method}"


def send_clip(video_path: Path, caption: str, chat_id: str | None = None,
              channel_id: int | None = None) -> PublishResult:
    """Envia um corte para o canal de divulgacao."""
    if not configured(channel_id):
        return PublishResult(False, error="Telegram nao configurado")

    target = chat_id or credentials.get("TELEGRAM_CHANNEL_ID", channel_id)
    try:
        with Path(video_path).open("rb") as fh:
            response = requests.post(
                _url("sendVideo", channel_id),
                data={
                    "chat_id": target,
                    "caption": caption[:1024],
                    "supports_streaming": "true",
                },
                files={"video": fh},
                timeout=600,
            ).json()
    except requests.RequestException as exc:
        return PublishResult(False, error=f"erro de rede: {exc}")

    if not response.get("ok"):
        return PublishResult(False, error=str(response.get("description")))
    message_id = str(response["result"]["message_id"])
    return PublishResult(True, remote_id=message_id)


def notify(chat_id: str, text: str) -> PublishResult:
    """Manda uma mensagem de texto a um chat (confirmacao de assinatura, avisos)."""
    if not configured():
        return PublishResult(False, error="Telegram nao configurado")
    try:
        response = requests.post(
            _url("sendMessage"), data={"chat_id": chat_id, "text": text}, timeout=30
        ).json()
    except requests.RequestException as exc:
        return PublishResult(False, error=f"erro de rede: {exc}")
    if not response.get("ok"):
        return PublishResult(False, error=str(response.get("description")))
    return PublishResult(True, remote_id=str(response["result"]["message_id"]))


def catalog(only_sellable: bool = True) -> list[dict[str, Any]]:
    """Lista o acervo publicavel.

    `only_sellable=True` (padrao) remove episodios sem licenca definida. E o
    ponto unico que separa "processei este video" de "posso vender este video".
    """
    items = archive.list_archive()
    if not only_sellable:
        return items
    return [item for item in items if item.get("licenca") in SELLABLE_LICENSES]


def deliver_episode(meta: dict[str, Any], chat_id: str) -> PublishResult:
    """Entrega um episodio do acervo a um assinante."""
    # A licenca e checada antes de tudo: e regra de negocio, nao depende de o
    # bot estar configurado. Assim a barreira vale igual em qualquer ambiente.
    if meta.get("licenca") not in SELLABLE_LICENSES:
        return PublishResult(
            False,
            error=(
                f"episodio {meta.get('id')} tem licenca '{meta.get('licenca')}'. "
                "Defina a licenca como licensed/owned/public_domain antes de distribuir."
            ),
        )

    if not configured():
        return PublishResult(False, error="Telegram nao configurado")

    files = meta.get("arquivos") or {}
    video = files.get("episodio")
    if not video or not Path(video).exists():
        return PublishResult(False, error="arquivo do episodio nao encontrado no acervo")

    caption = f"{meta.get('titulo')}\n{meta.get('canal') or ''}".strip()[:1024]
    result = send_clip(Path(video), caption, chat_id=chat_id)

    legenda = files.get("legenda_ptbr")
    if result.ok and legenda and Path(legenda).exists():
        try:
            with Path(legenda).open("rb") as fh:
                requests.post(
                    _url("sendDocument"),
                    data={"chat_id": chat_id},
                    files={"document": fh},
                    timeout=120,
                )
        except requests.RequestException as exc:
            log.warning("legenda nao enviada: %s", exc)

    return result


def publish(video_path: Path, caption: str, title: str | None = None,
            thumb_path: Path | None = None, channel_id: int | None = None) -> PublishResult:
    """Interface uniforme com os demais publishers."""
    return send_clip(video_path, caption, channel_id=channel_id)
