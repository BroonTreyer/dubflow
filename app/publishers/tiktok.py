"""Publicacao via TikTok Content Posting API (upload direto de arquivo).

Enquanto o app estiver em modo sandbox/unaudited, os posts saem como rascunho
privado — o TikTok so libera publicacao direta apos auditoria do app. O fluxo de
codigo e o mesmo nos dois casos.

Requer `video.publish` no escopo do token.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from app import credentials
from app.publishers.base import PublishResult

log = logging.getLogger(__name__)

API = "https://open.tiktokapis.com/v2"

name = "tiktok"


def configured() -> bool:
    return bool(credentials.get("TIKTOK_ACCESS_TOKEN"))


def publish(video_path: Path, caption: str, title: str | None = None,
            thumb_path: Path | None = None) -> PublishResult:
    if not configured():
        return PublishResult(False, error="TikTok nao configurado (TIKTOK_ACCESS_TOKEN)")

    video_path = Path(video_path)
    if not video_path.exists():
        return PublishResult(False, error=f"arquivo nao encontrado: {video_path}")

    size = video_path.stat().st_size
    headers = {
        "Authorization": f"Bearer {credentials.get("TIKTOK_ACCESS_TOKEN")}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    try:
        init = requests.post(
            f"{API}/post/publish/video/init/",
            headers=headers,
            json={
                "post_info": {
                    "title": caption[:2200],
                    "privacy_level": "SELF_ONLY",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": size,  # arquivo inteiro em um chunk
                    "total_chunk_count": 1,
                },
            },
            timeout=60,
        ).json()

        data = init.get("data") or {}
        upload_url = data.get("upload_url")
        publish_id = data.get("publish_id")
        if not upload_url or not publish_id:
            return PublishResult(False, error=f"init falhou: {init.get('error') or init}")

        with video_path.open("rb") as fh:
            upload = requests.put(
                upload_url,
                data=fh,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                    "Content-Range": f"bytes 0-{size - 1}/{size}",
                },
                timeout=1800,
            )
        if upload.status_code not in (200, 201, 204):
            return PublishResult(False, error=f"upload falhou: HTTP {upload.status_code}")

        status = _wait_status(publish_id, headers)
        if status in {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}:
            return PublishResult(True, remote_id=publish_id)
        return PublishResult(False, remote_id=publish_id, error=f"status final: {status}")

    except requests.RequestException as exc:
        return PublishResult(False, error=f"erro de rede: {exc}")


def _wait_status(publish_id: str, headers: dict[str, str], timeout_seconds: int = 900) -> str:
    deadline = time.time() + timeout_seconds
    status = "PROCESSING"
    while time.time() < deadline:
        response = requests.post(
            f"{API}/post/publish/status/fetch/",
            headers=headers,
            json={"publish_id": publish_id},
            timeout=30,
        ).json()
        status = (response.get("data") or {}).get("status", "UNKNOWN")
        if status in {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"}:
            return status
        time.sleep(10)
    return f"TIMEOUT({status})"
