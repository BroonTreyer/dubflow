"""Telegram: distribuicao de cortes e catalogo do acervo.

Duas funcoes distintas:

- `send_clip` — manda um corte para o canal. Serve para qualquer episodio; e
  divulgacao, mesmo papel do Reels.
- `catalog` / `deliver_episode` — o acervo vendido por assinatura. Aqui existe um
  filtro: so entra episodio com licenca `licensed`, `owned` ou `public_domain`.
  Episodio `unknown` fica visivel no seu painel e fora do catalogo pago.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from app import credentials
from app.config import settings
from app.pipeline import archive
from app.publishers.base import PublishResult

log = logging.getLogger(__name__)

# Licencas que autorizam distribuicao comercial do episodio completo.
SELLABLE_LICENSES = {"licensed", "owned", "public_domain"}

name = "telegram"


def configured(channel_id: int | None = None) -> bool:
    return bool(credentials.get("TELEGRAM_BOT_TOKEN", channel_id)
                and credentials.get("TELEGRAM_CHANNEL_ID", channel_id))


def _url(method: str, channel_id: int | None = None) -> str:
    # Base configuravel: nuvem do Telegram (50 MB) ou Bot API local (ate 2 GB).
    base = (settings.telegram_api_base or "https://api.telegram.org").rstrip("/")
    return f"{base}/bot{credentials.get("TELEGRAM_BOT_TOKEN", channel_id)}/{method}"


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


def notify(chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> PublishResult:
    """Manda uma mensagem de texto a um chat (DM do comprador, avisos).

    So exige o token — e envio direto a um chat, nao usa o canal. `reply_markup`
    (opcional) anexa o teclado de botoes.
    """
    if not credentials.get("TELEGRAM_BOT_TOKEN"):
        return PublishResult(False, error="Telegram nao configurado")
    data: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        response = requests.post(_url("sendMessage"), data=data, timeout=30).json()
    except requests.RequestException as exc:
        return PublishResult(False, error=f"erro de rede: {exc}")
    if not response.get("ok"):
        return PublishResult(False, error=str(response.get("description")))
    return PublishResult(True, remote_id=str(response["result"]["message_id"]))


def send_photo_path(chat_id: str, image_path: Path, caption: str = "",
                    reply_markup: dict[str, Any] | None = None) -> PublishResult:
    """Envia uma imagem de um arquivo local (ex.: o banner de boas-vindas) a um chat,
    com legenda e teclado opcionais. So exige o token."""
    if not credentials.get("TELEGRAM_BOT_TOKEN"):
        return PublishResult(False, error="Telegram nao configurado")
    path = Path(image_path)
    if not path.exists():
        return PublishResult(False, error=f"imagem nao encontrada: {path}")
    data: dict[str, Any] = {"chat_id": chat_id, "caption": caption[:1024]}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        with path.open("rb") as fh:
            response = requests.post(
                _url("sendPhoto"), data=data,
                files={"photo": fh}, timeout=120,
            ).json()
    except requests.RequestException as exc:
        return PublishResult(False, error=f"erro de rede: {exc}")
    if not response.get("ok"):
        return PublishResult(False, error=str(response.get("description")))
    return PublishResult(True, remote_id=str(response["result"]["message_id"]))


# ------------------------------------------------------------------- canal VIP
# O acesso pago e um Canal VIP privado: quem assina entra por um link de convite
# de uso unico; quando a assinatura vence, e removido. So depende do token + do
# TELEGRAM_VIP_CHAT_ID (config global), nao do cofre por canal.


def vip_configured() -> bool:
    return bool(credentials.get("TELEGRAM_BOT_TOKEN")
                and credentials.get("TELEGRAM_VIP_CHAT_ID"))


def create_vip_invite(expire_seconds: int = 86_400) -> tuple[str | None, str | None]:
    """Gera um link de convite de USO UNICO para o canal VIP.

    Retorna (link, erro). member_limit=1 impede repasse a varias pessoas; o link
    ainda vale por `expire_seconds` (padrao 24h) so para o comprador entrar.
    """
    if not vip_configured():
        return None, "Canal VIP nao configurado (defina TELEGRAM_VIP_CHAT_ID)"
    payload: dict[str, Any] = {
        "chat_id": credentials.get("TELEGRAM_VIP_CHAT_ID"),
        "member_limit": 1,
    }
    if expire_seconds > 0:
        payload["expire_date"] = int(time.time()) + expire_seconds
    try:
        response = requests.post(_url("createChatInviteLink"), data=payload, timeout=30).json()
    except requests.RequestException as exc:
        return None, f"erro de rede: {exc}"
    if not response.get("ok"):
        return None, str(response.get("description"))
    return response["result"]["invite_link"], None


def remove_from_vip(user_id: str) -> PublishResult:
    """Expulsa o assinante do canal VIP quando a assinatura vence.

    Faz ban seguido de unban: expulsa sem banir para sempre, entao a pessoa pode
    voltar se renovar (recebe um novo link de convite na renovacao).
    """
    if not vip_configured():
        return PublishResult(False, error="Canal VIP nao configurado")
    vip = credentials.get("TELEGRAM_VIP_CHAT_ID")
    try:
        banned = requests.post(
            _url("banChatMember"),
            data={"chat_id": vip, "user_id": user_id},
            timeout=30,
        ).json()
        requests.post(
            _url("unbanChatMember"),
            data={"chat_id": vip, "user_id": user_id, "only_if_banned": "true"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return PublishResult(False, error=f"erro de rede: {exc}")
    if not banned.get("ok"):
        return PublishResult(False, error=str(banned.get("description")))
    return PublishResult(True)


def send_photo_b64(chat_id: str, image_b64: str, caption: str = "") -> PublishResult:
    """Envia uma imagem em base64 (ex.: o QR code do Pix) a um chat.

    So depende do token (e um envio de DM, nao usa o canal). Aceita tanto o base64
    cru quanto o formato 'data:image/png;base64,....'.
    """
    if not credentials.get("TELEGRAM_BOT_TOKEN"):
        return PublishResult(False, error="Telegram nao configurado")
    raw = image_b64.split(",", 1)[-1] if image_b64 else ""
    try:
        img = base64.b64decode(raw)
    except (ValueError, TypeError) as exc:
        return PublishResult(False, error=f"base64 invalido: {exc}")
    try:
        response = requests.post(
            _url("sendPhoto"),
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"photo": ("qr.png", img, "image/png")},
            timeout=60,
        ).json()
    except requests.RequestException as exc:
        return PublishResult(False, error=f"erro de rede: {exc}")
    if not response.get("ok"):
        return PublishResult(False, error=str(response.get("description")))
    return PublishResult(True, remote_id=str(response["result"]["message_id"]))


def publish_vip_episode(video_path: Path, caption: str) -> PublishResult:
    """Posta o VIDEO COMPLETO do episodio no canal VIP (so quem assina ve).

    E a metade paga da separacao: os cortes vao pro canal isca (send_clip), o
    completo vem para ca. Arquivos grandes (episodio de 1h) exigem um servidor Bot
    API local (TELEGRAM_API_BASE); com a nuvem padrao do Telegram o limite e 50 MB.
    """
    if not vip_configured():
        return PublishResult(False, error="Canal VIP nao configurado")
    path = Path(video_path)
    if not path.exists():
        return PublishResult(False, error=f"video do episodio nao encontrado: {path}")
    try:
        with path.open("rb") as fh:
            response = requests.post(
                _url("sendVideo"),
                data={"chat_id": credentials.get("TELEGRAM_VIP_CHAT_ID"),
                      "caption": caption[:1024], "supports_streaming": "true"},
                files={"video": fh},
                timeout=1800,  # upload de episodio inteiro pode demorar
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
