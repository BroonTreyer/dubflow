"""Publicacao de Shorts via YouTube Data API v3 (upload resumavel de arquivo).

Ao contrario do Instagram, o YouTube aceita upload direto — nao precisa de URL
publica. Mas exige OAuth2: o token de acesso vive ~1h, entao guardamos um
`refresh_token` de longa duracao no .env e trocamos por um access token novo a
cada publicacao. O `scripts/youtube_auth.py` gera esse refresh token uma vez.

O corte ja e 9:16 e curto (25-75s); com `#Shorts` na descricao o YouTube o
classifica como Short automaticamente.

Por seguranca, o padrao e `privacyStatus=private` — igual o TikTok sai como
rascunho. Nada vai ao ar sem `YOUTUBE_PRIVACY=public` explicito no .env.

Requer o escopo `https://www.googleapis.com/auth/youtube.upload` no token.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

import requests

from app import credentials
from app.config import settings
from app.publishers.base import PublishResult

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

# Limites da API: titulo do YouTube tem no maximo 100 caracteres; o conjunto de
# tags nao pode passar de 500. Ultrapassar qualquer um faz o insert falhar inteiro.
TITLE_MAX = 100
TAGS_TOTAL_MAX = 500

name = "youtube"


def configured() -> bool:
    return bool(
        credentials.get("YOUTUBE_CLIENT_ID")
        and credentials.get("YOUTUBE_CLIENT_SECRET")
        and credentials.get("YOUTUBE_REFRESH_TOKEN")
    )


def publish(video_path: Path, caption: str, title: str | None = None) -> PublishResult:
    if not configured():
        return PublishResult(
            False,
            error="YouTube nao configurado (YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN)",
        )

    video_path = Path(video_path)
    if not video_path.exists():
        return PublishResult(False, error=f"arquivo nao encontrado: {video_path}")

    token = _access_token()
    if token is None:
        return PublishResult(
            False,
            error="nao consegui renovar o access token do YouTube. O refresh token "
            "pode ter sido revogado — gere um novo com scripts/youtube_auth.py.",
        )

    # Video vertical vira Short (#Shorts); horizontal vai como video comum.
    yt_title, description, tags = _metadata(caption, title, is_short=_is_portrait(video_path))
    title = yt_title
    size = video_path.stat().st_size

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": settings.youtube_category_id,
        },
        "status": {
            "privacyStatus": credentials.get("YOUTUBE_PRIVACY") or settings.youtube_privacy,
            # Sem esta declaracao a API recusa o upload: e obrigatoria desde as
            # regras de conteudo infantil (COPPA). Estes cortes nao sao para criancas.
            "selfDeclaredMadeForKids": False,
        },
    }

    try:
        # 1. Inicia o upload resumavel: a API devolve, no header Location, a URL
        # para onde os bytes do video vao.
        init = requests.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(size),
            },
            json=body,
            timeout=60,
        )
        if init.status_code != 200:
            return PublishResult(False, error=_explain(init))
        session_url = init.headers.get("Location")
        if not session_url:
            return PublishResult(False, error="a API nao devolveu a URL de upload")

        # 2. Envia o arquivo inteiro num unico PUT.
        with video_path.open("rb") as fh:
            upload = requests.put(
                session_url,
                data=fh,
                headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
                timeout=1800,
            )
        if upload.status_code not in (200, 201):
            return PublishResult(False, error=_explain(upload))

        video_id = (upload.json() or {}).get("id")
        if not video_id:
            return PublishResult(False, error="upload aceito mas sem id de video na resposta")

        return PublishResult(
            True,
            remote_id=video_id,
            permalink=f"https://www.youtube.com/shorts/{video_id}",
        )

    except requests.RequestException as exc:
        return PublishResult(False, error=f"erro de rede: {exc}")


def _access_token() -> str | None:
    """Troca o refresh token de longa duracao por um access token de ~1h."""
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "client_id": credentials.get("YOUTUBE_CLIENT_ID"),
                "client_secret": credentials.get("YOUTUBE_CLIENT_SECRET"),
                "refresh_token": credentials.get("YOUTUBE_REFRESH_TOKEN"),
                "grant_type": "refresh_token",
            },
            timeout=30,
        ).json()
    except requests.RequestException as exc:
        log.error("falha ao renovar token do YouTube: %s", exc)
        return None
    token = response.get("access_token")
    if not token:
        log.error("refresh do YouTube recusado: %s", response.get("error_description") or response)
    return token


def _metadata(caption: str, title: str | None, is_short: bool) -> tuple[str, str, list[str]]:
    """Deriva titulo, descricao e tags para o YouTube.

    Usa o `title` do corte quando informado (o melhor titulo, escolhido por Claude
    na selecao); sem ele, cai para a primeira linha da legenda. A legenda inteira
    vira descricao. So Shorts levam #Shorts.
    """
    caption = (caption or "").strip()
    lines = [ln.strip() for ln in caption.splitlines() if ln.strip()]
    base = (title or "").strip() or (lines[0] if lines else "Corte")

    # O YouTube recusa < e > no titulo; corta no limite de 100.
    yt_title = base.replace("<", "(").replace(">", ")")[:TITLE_MAX].strip() or "Corte"

    description = caption
    if is_short and "#shorts" not in caption.lower():
        description = (caption + "\n\n#Shorts").strip()

    return yt_title, description, _tags_from(caption)


def _is_portrait(video_path: Path) -> bool:
    """True se o video e vertical (Short). Assume vertical quando nao da para medir."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        w, h = (int(v) for v in out.stdout.strip().split("x")[:2])
        return h >= w
    except (OSError, ValueError, subprocess.SubprocessError):
        return True


def _tags_from(caption: str) -> list[str]:
    """Extrai as hashtags da legenda como tags, respeitando o teto de 500 chars."""
    tags: list[str] = []
    total = 0
    for raw in re.findall(r"#(\w+)", caption):
        if raw.lower() == "shorts" or raw in tags:
            continue
        # +1 aproxima a virgula que a API conta entre as tags.
        if total + len(raw) + 1 > TAGS_TOTAL_MAX:
            break
        tags.append(raw)
        total += len(raw) + 1
    return tags


def _explain(response: requests.Response) -> str:
    """Traduz o erro da API para algo acionavel em vez do JSON cru do Google."""
    try:
        error = (response.json() or {}).get("error") or {}
    except ValueError:
        error = {}
    message = error.get("message") or response.text[:300]
    status = response.status_code

    if status == 401:
        return (
            "token do YouTube invalido ou expirado. Gere um novo refresh token com "
            "scripts/youtube_auth.py e atualize o .env."
        )
    if status == 403:
        low = message.lower()
        if "quota" in low:
            return (
                "cota diaria da YouTube Data API esgotada (upload custa ~1600 unidades "
                "de 10000/dia). Tente amanha ou peca aumento de cota no Google Cloud."
            )
        if "uploadlimitexceeded" in low or "limit" in low:
            return "limite de uploads do canal atingido. Aguarde antes de publicar mais."
        return f"permissao negada: {message}"
    return f"HTTP {status}: {message}"
