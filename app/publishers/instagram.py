"""Publicacao de Reels via Instagram Graph API.

A API nao aceita upload de arquivo: ela busca o video por URL publica. Por isso
`PUBLIC_BASE_URL` precisa apontar para um endereco que a Meta consiga acessar
(o proprio servidor exposto, um bucket, ou um tunel tipo cloudflared).

Requer conta Instagram Business/Creator vinculada a uma Pagina do Facebook e um
token de acesso com `instagram_content_publish`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from app import credentials
from app.publishers.base import PublishResult
from app.security import media_signature

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v21.0"

name = "instagram"


def configured() -> bool:
    return bool(credentials.get("IG_USER_ID") and credentials.get("IG_ACCESS_TOKEN") and credentials.get("PUBLIC_BASE_URL"))


def public_url_for(video_path: Path) -> str:
    """URL assinada do arquivo, servida por /media do proprio app.

    A assinatura HMAC deixa a URL alcancavel pela Meta (que nao faz login) sem
    tornar o acervo publico: sem a assinatura correta o servidor recusa, e o
    nome do arquivo nao e enumeravel.
    """
    name = video_path.name
    return f"{credentials.get("PUBLIC_BASE_URL").rstrip('/')}/media/{media_signature(name)}/{name}"


def publish(video_path: Path, caption: str, title: str | None = None,
            thumb_path: Path | None = None) -> PublishResult:
    if not configured():
        return PublishResult(False, error="Instagram nao configurado (IG_USER_ID/TOKEN/PUBLIC_BASE_URL)")

    try:
        container = requests.post(
            f"{GRAPH}/{credentials.get("IG_USER_ID")}/media",
            data={
                "media_type": "REELS",
                "video_url": public_url_for(video_path),
                "caption": caption[:2200],
                "share_to_feed": "true",
                "access_token": credentials.get("IG_ACCESS_TOKEN"),
            },
            timeout=60,
        )
        body = container.json()
        if "id" not in body:
            return PublishResult(False, error=_explain(body))
        creation_id = body["id"]

        # O Instagram processa o video de forma assincrona; publicar antes de
        # FINISHED devolve erro.
        status = _wait_ready(creation_id)
        if status != "FINISHED":
            return PublishResult(False, error=f"processamento terminou como {status}")

        published = requests.post(
            f"{GRAPH}/{credentials.get("IG_USER_ID")}/media_publish",
            data={"creation_id": creation_id, "access_token": credentials.get("IG_ACCESS_TOKEN")},
            timeout=60,
        ).json()
        if "id" not in published:
            return PublishResult(False, error=f"publicacao falhou: {published}")

        media_id = published["id"]
        permalink = _permalink(media_id)
        return PublishResult(True, remote_id=media_id, permalink=permalink)

    except requests.RequestException as exc:
        return PublishResult(False, error=f"erro de rede: {exc}")


def stats(remote_id: str) -> dict[str, int | None] | None:
    """Curtidas e comentarios do Reels. None se nao der para consultar."""
    token = credentials.get("IG_ACCESS_TOKEN")
    if not (remote_id and token):
        return None
    try:
        body = requests.get(
            f"{GRAPH}/{remote_id}",
            params={"fields": "like_count,comments_count", "access_token": token},
            timeout=30,
        ).json()
    except requests.RequestException as exc:
        log.warning("stats do Instagram falharam: %s", exc)
        return None
    if "like_count" not in body and "comments_count" not in body:
        return None
    return {"views": None, "likes": body.get("like_count"), "comments": body.get("comments_count")}


def _wait_ready(creation_id: str, timeout_seconds: int = 900, interval: int = 10) -> str:
    deadline = time.time() + timeout_seconds
    status = "IN_PROGRESS"
    while time.time() < deadline:
        response = requests.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code", "access_token": credentials.get("IG_ACCESS_TOKEN")},
            timeout=30,
        ).json()
        status = response.get("status_code", "UNKNOWN")
        if status in {"FINISHED", "ERROR", "EXPIRED"}:
            return status
        time.sleep(interval)
    return f"TIMEOUT({status})"


def _explain(body: dict) -> str:
    """Traduz o erro da Graph API para algo acionavel.

    O token de acesso expira em ~60 dias. Sem esta traducao, a falha aparece como
    um dicionario cru da Meta e parece bug do pipeline, nao credencial vencida.
    """
    error = body.get("error") or {}
    code = error.get("code")
    message = error.get("message", "")

    if code in (190, 102) or "expired" in message.lower() or "session" in message.lower():
        return (
            "IG_ACCESS_TOKEN expirado ou invalido. Gere um novo token de longa duracao "
            "no Facebook Developers e atualize o .env."
        )
    if code == 200 or "permission" in message.lower():
        return (
            "Token sem permissao instagram_content_publish, ou a conta nao e "
            "Business/Creator vinculada a uma Pagina."
        )
    if "media_url" in message.lower() or "download" in message.lower():
        return (
            "A Meta nao conseguiu baixar o video. Confirme que PUBLIC_BASE_URL e "
            f"acessivel pela internet. Detalhe: {message}"
        )
    return f"criacao do container falhou: {message or body}"


def _permalink(media_id: str) -> str | None:
    try:
        response = requests.get(
            f"{GRAPH}/{media_id}",
            params={"fields": "permalink", "access_token": credentials.get("IG_ACCESS_TOKEN")},
            timeout=30,
        ).json()
        return response.get("permalink")
    except requests.RequestException:
        return None
