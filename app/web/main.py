"""Painel web do dubflow (FastAPI).

Cola o link, acompanha o progresso, revisa os cortes e dispara a publicacao.
O processamento pesado roda no worker; a UI so escreve na fila.

Toda rota do painel exige sessao. A unica excecao e `/media/{assinatura}/{arquivo}`,
que precisa ser alcancavel pela Meta — protegida por HMAC em vez de login.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from app import credentials, db, security
from app.config import settings
from app.pipeline import archive
from app.publishers import REGISTRY, status as publisher_status

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="dubflow", version="0.2.0")
db.init_db()

# Dependencia aplicada a todas as rotas do painel.
panel = [Depends(security.require_session)]


@app.on_event("startup")
def warn_about_exposure() -> None:
    if not security.get_password():
        log.warning(
            "DUBFLOW_PASSWORD nao definida — o painel vai recusar todos os logins. "
            "Defina uma senha no .env."
        )
    if settings.host not in ("127.0.0.1", "localhost"):
        log.warning("painel exposto em %s — confirme que a senha e forte", settings.host)


@app.exception_handler(401)
def unauthorized(request: Request, exc: HTTPException):
    """Navegador vai para o login; chamada de API recebe 401 limpo."""
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=401)


# --------------------------------------------------------------------------- sessao


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, erro: str = ""):
    return templates.TemplateResponse(request, "login.html", {"erro": erro})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if not security.check_password(password):
        log.warning("tentativa de login recusada de %s", request.client.host if request.client else "?")
        return RedirectResponse("/login?erro=1", status_code=303)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        security.SESSION_COOKIE,
        security.issue_session(),
        max_age=security.SESSION_TTL,
        httponly=True,      # fora do alcance de JavaScript
        samesite="lax",     # nao acompanha requisicoes vindas de outros sites
        secure=False,       # troque para True atras de HTTPS
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(security.SESSION_COOKIE)
    return response


# --------------------------------------------------------------------------- painel


@app.get("/", response_class=HTMLResponse, dependencies=panel)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "episodes": db.list_episodes(),
            "publishers": publisher_status(),
            "license_states": db.LICENSE_STATES,
            "csrf": security.csrf_token(request),
        },
    )


@app.get("/connections", response_class=HTMLResponse, dependencies=panel)
def connections(request: Request, salvo: int = 0):
    # Valores nao-sensiveis podem ser mostrados preenchidos; segredos, nunca.
    values = {
        key: credentials.get(key)
        for key in credentials.ALL_KEYS
        if key not in credentials.SECRET_KEYS
    }
    return templates.TemplateResponse(
        request,
        "connections.html",
        {
            "managed": credentials.MANAGED,
            "secret_keys": credentials.SECRET_KEYS,
            "status": credentials.status(),
            "ready": publisher_status(),
            "values": values,
            "csrf": security.csrf_token(request),
            "salvo": bool(salvo),
        },
    )


@app.post("/connections", dependencies=panel)
async def save_connections(request: Request, csrf: str = Form("")):
    security.require_csrf(request, csrf)
    form = await request.form()
    # So chaves da allowlist do cofre; valores vazios sao ignorados no save().
    updates = {key: str(form.get(key) or "") for key in credentials.ALL_KEYS}
    credentials.save(updates)
    return RedirectResponse("/connections?salvo=1", status_code=303)


@app.post("/episodes", dependencies=panel)
def create_episode(
    request: Request,
    url: str = Form(...),
    license_status: str = Form("unknown"),
    csrf: str = Form(""),
):
    security.require_csrf(request, csrf)

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "informe uma URL http(s) valida")
    if license_status not in db.LICENSE_STATES:
        license_status = "unknown"

    # Reprocessar o mesmo video paga a traducao de novo — o erro mais caro que um
    # duplo clique consegue causar. Manda para o episodio existente.
    existing = db.find_active_by_url(url)
    if existing:
        return RedirectResponse(f"/episodes/{existing['id']}?duplicado=1", status_code=303)

    db.create_episode(url, license_status)
    return RedirectResponse("/", status_code=303)


@app.get("/episodes/{episode_id}", response_class=HTMLResponse, dependencies=panel)
def episode_detail(request: Request, episode_id: int, duplicado: int = 0):
    episode = db.get_episode(episode_id)
    if episode is None:
        raise HTTPException(404, "episodio nao encontrado")
    return templates.TemplateResponse(
        request,
        "episode.html",
        {
            "episode": episode,
            "clips": db.list_clips(episode_id),
            "posts": db.list_posts(episode_id),
            "publishers": publisher_status(),
            "license_states": db.LICENSE_STATES,
            "csrf": security.csrf_token(request),
            "duplicado": bool(duplicado),
            "media_sig": security.media_signature,
        },
    )


@app.post("/episodes/{episode_id}/license", dependencies=panel)
def set_license(request: Request, episode_id: int,
                license_status: str = Form(...), csrf: str = Form("")):
    security.require_csrf(request, csrf)
    if license_status not in db.LICENSE_STATES:
        raise HTTPException(400, "licenca invalida")
    db.update_episode(episode_id, license_status=license_status)
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


# Estados em que reprocessar e seguro. Reenfileirar um episodio em andamento
# colocaria dois workers no mesmo job, disputando GPU e escrevendo os mesmos arquivos.
RETRYABLE = {"failed", "done", "canceled"}


@app.post("/episodes/{episode_id}/retry", dependencies=panel)
def retry_episode(request: Request, episode_id: int, csrf: str = Form("")):
    security.require_csrf(request, csrf)
    episode = db.get_episode(episode_id)
    if episode is None:
        raise HTTPException(404, "episodio nao encontrado")
    if episode["status"] not in RETRYABLE:
        raise HTTPException(
            409,
            f"episodio esta em '{episode['status']}'. Espere terminar ou falhar "
            "antes de reprocessar.",
        )
    db.update_episode(episode_id, status="queued", progress=0, error=None)
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@app.post("/episodes/{episode_id}/action", dependencies=panel)
def request_action(request: Request, episode_id: int,
                   action: str = Form(...), csrf: str = Form("")):
    """Enfileira queima da legenda ou re-render dos cortes."""
    security.require_csrf(request, csrf)

    episode = db.get_episode(episode_id)
    if episode is None:
        raise HTTPException(404, "episodio nao encontrado")
    if action not in db.ACTIONS:
        raise HTTPException(400, f"acao invalida: {action}")
    if episode["status"] not in ("done", "failed"):
        raise HTTPException(409, "espere o episodio terminar antes de pedir esta acao")

    db.request_action(episode_id, action)
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@app.post("/clips/{clip_id}/publish", dependencies=panel)
def schedule_publish(request: Request, clip_id: int, platform: str = Form(...),
                     orientation: str = Form("vertical"),
                     scheduled_at: str = Form(""), csrf: str = Form("")):
    security.require_csrf(request, csrf)

    clip = db.get_clip(clip_id)
    if clip is None:
        raise HTTPException(404, "corte nao encontrado")
    if clip.get("status") != "ready" or not clip.get("path"):
        raise HTTPException(400, "corte ainda nao foi renderizado")
    if platform not in REGISTRY:
        raise HTTPException(400, f"plataforma desconhecida: {platform}")
    if orientation not in ("vertical", "horizontal"):
        raise HTTPException(400, "orientacao invalida")
    if orientation == "horizontal" and not clip.get("path_wide"):
        raise HTTPException(400, "este corte nao tem versao horizontal (16:9) renderizada")

    db.create_post(clip_id, platform, _parse_schedule(scheduled_at), orientation)
    return RedirectResponse(f"/episodes/{clip['episode_id']}", status_code=303)


def _parse_schedule(raw: str) -> str | None:
    """Normaliza o agendamento para ISO-8601 em UTC.

    A fila compara `scheduled_at` como string. Texto livre passa direto pela
    comparacao lexicografica e o post fica invisivel para o worker — publicado
    nunca, sem erro nenhum. Melhor recusar na entrada.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(
            400,
            "data de agendamento invalida — use o formato AAAA-MM-DDTHH:MM "
            "(o campo de data do navegador ja envia assim)",
        )
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # horario local do servidor
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- api


@app.get("/api/episodes/{episode_id}", dependencies=panel)
def api_episode(episode_id: int):
    """Usado pelo polling da UI para atualizar a barra de progresso."""
    episode = db.get_episode(episode_id)
    if episode is None:
        raise HTTPException(404)
    meta = episode.get("meta") or {}
    return JSONResponse(
        {
            "id": episode["id"],
            "status": episode["status"],
            "progress": episode["progress"],
            "title": episode["title"],
            "error": episode["error"],
            "clips": len([c for c in db.list_clips(episode_id) if c["status"] == "ready"]),
            "untranslated": meta.get("untranslated_segments", 0),
            "pending_action": episode.get("pending_action"),
        }
    )


@app.get("/api/catalog", dependencies=panel)
def api_catalog(include_unlicensed: bool = False):
    """Catalogo do acervo. Por padrao mostra apenas o que pode ser distribuido."""
    from app.publishers import telegram

    items = telegram.catalog(only_sellable=not include_unlicensed)
    # Caminhos absolutos descrevem a maquina, nao o acervo: fora da resposta.
    return JSONResponse(
        [
            {
                "id": item.get("id"),
                "titulo": item.get("titulo"),
                "canal": item.get("canal"),
                "duracao_segundos": item.get("duracao_segundos"),
                "idioma_origem": item.get("idioma_origem"),
                "idioma_destino": item.get("idioma_destino"),
                "licenca": item.get("licenca"),
                "criado_em": item.get("criado_em"),
                "arquivos": sorted((item.get("arquivos") or {}).keys()),
            }
            for item in items
        ]
    )


# --------------------------------------------------------------------------- arquivos


@app.get("/media/{signature}/{filename}")
def media(signature: str, filename: str):
    """Serve os cortes renderizados — usado pelo Instagram, que busca por URL.

    Sem sessao (a Meta nao faz login), mas com assinatura HMAC: a URL nao e
    adivinhavel nem enumeravel. O nome do arquivo carrega o id do episodio
    (`ep00042_corte_01.mp4`), entao o caminho e resolvido de forma exata em vez
    de por glob — que antes devolvia o corte de outro episodio.
    """
    safe_name = Path(filename).name
    if not security.verify_media_signature(safe_name, signature):
        raise HTTPException(403, "assinatura invalida")

    episode_dir = _episode_dir_from_clip_name(safe_name)
    if episode_dir is None:
        raise HTTPException(404, "arquivo nao encontrado")

    candidate = episode_dir / "clips" / safe_name
    root = (settings.data_dir / "episodes").resolve()
    resolved = candidate.resolve()
    # Cinto e suspensorio: mesmo com nome saneado, so servimos de dentro do acervo.
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise HTTPException(404, "arquivo nao encontrado")
    media_type = "image/jpeg" if resolved.suffix.lower() in (".jpg", ".jpeg") else "video/mp4"
    return FileResponse(resolved, media_type=media_type)


def _episode_dir_from_clip_name(name: str) -> Path | None:
    """`ep00042_corte_01.mp4` -> data/episodes/ep_00042"""
    if not name.startswith("ep") or "_corte_" not in name:
        return None
    digits = name[2:].split("_", 1)[0]
    if not digits.isdigit():
        return None
    return settings.data_dir / "episodes" / f"ep_{int(digits):05d}"


# Artefatos que o painel pode baixar. O video de origem fica de fora: e o
# material bruto de terceiro, e nao ha motivo para expo-lo por HTTP.
DOWNLOADABLE = {"srt", "ass", "translated", "transcript", "episode_burned"}


@app.get("/download/{episode_id}/{kind}", dependencies=panel)
def download(episode_id: int, kind: str):
    if kind not in DOWNLOADABLE:
        raise HTTPException(404, f"artefato '{kind}' nao e baixavel")
    episode = db.get_episode(episode_id)
    if episode is None:
        raise HTTPException(404)
    path = (episode.get("paths") or {}).get(kind)
    if not path or not Path(path).exists():
        raise HTTPException(404, f"artefato '{kind}' indisponivel")
    return FileResponse(path, filename=Path(path).name)


@app.get("/health")
def health():
    """Sonda de disponibilidade — sem contagens nem estado interno."""
    return Response(content="ok", media_type="text/plain")


@app.get("/api/status", dependencies=panel)
def api_status():
    return {
        "queued": sum(1 for e in db.list_episodes(200) if e["status"] == "queued"),
        "archive": len(archive.list_archive()),
        "publishers": publisher_status(),
    }
