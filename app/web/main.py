"""Painel web do dubflow (FastAPI).

Cola o link, acompanha o progresso, revisa os cortes e dispara a publicacao.
O processamento pesado roda no worker; a UI so escreve na fila.

Toda rota do painel exige sessao. A unica excecao e `/media/{assinatura}/{arquivo}`,
que precisa ser alcancavel pela Meta — protegida por HMAC em vez de login.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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

from app import credentials, db, sales, security
from app.config import MARKET_OPTIONS, TARGET_LANGUAGES, configure_logging, settings
from app.pipeline import archive
from app.publishers import REGISTRY, status as publisher_status

configure_logging("web")
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
def login_form(request: Request, erro: str = "", bloqueado: str = ""):
    return templates.TemplateResponse(request, "login.html", {"erro": erro, "bloqueado": bloqueado})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    ip = request.client.host if request.client else "?"

    espera = security.login_retry_after(ip)
    if espera:
        log.warning("login bloqueado por forca bruta de %s (%ds restantes)", ip, espera)
        return RedirectResponse("/login?bloqueado=1", status_code=303,
                                headers={"Retry-After": str(espera)})

    if not security.check_password(password):
        security.record_login_failure(ip)
        log.warning("tentativa de login recusada de %s", ip)
        return RedirectResponse("/login?erro=1", status_code=303)

    security.clear_login_failures(ip)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        security.SESSION_COOKIE,
        security.issue_session(),
        max_age=security.SESSION_TTL,
        httponly=True,                    # fora do alcance de JavaScript
        samesite="lax",                   # nao acompanha requisicoes de outros sites
        secure=settings.cookie_secure,    # exige HTTPS quando o painel nao e local
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
            "target_langs": TARGET_LANGS,
            "csrf": security.csrf_token(request),
        },
    )


_WEEKDAYS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def _day_label(iso_date: str) -> str:
    """'2026-08-25' -> 'seg 25/08'. Devolve o proprio texto se nao parsear."""
    try:
        d = datetime.fromisoformat(iso_date)
    except (TypeError, ValueError):
        return iso_date
    return f"{_WEEKDAYS_PT[d.weekday()]} {d:%d/%m}"


@app.get("/dashboard", response_class=HTMLResponse, dependencies=panel)
def dashboard(request: Request):
    """Visao geral: KPIs, contas (global + canais), calendario, cortes e metricas."""
    stats = db.dashboard_stats()
    channels = db.list_channels()
    totals = db.channel_totals()

    # Cache do status por canal (evita reabrir o cofre varias vezes por linha).
    channel_ready = {ch["id"]: publisher_status(ch["id"]).get(ch["platform"], False)
                     for ch in channels}
    global_ready = publisher_status()  # conta global (cofre .env / conexoes)

    accounts = [{
        "id": None, "name": "Conta global (.env / conexões)", "platform": None,
        "market": None, "project": None, "status": "active",
        "platforms_ready": global_ready, "connected": any(global_ready.values()),
        "totals": totals.get(None, {}), "detail_url": "/connections",
    }]
    for ch in channels:
        accounts.append({
            "id": ch["id"], "name": ch["name"], "platform": ch["platform"],
            "market": ch["market"], "project": ch["project"], "status": ch["status"],
            "platforms_ready": None, "connected": channel_ready[ch["id"]],
            "totals": totals.get(ch["id"], {}), "detail_url": f"/channels/{ch['id']}",
        })

    # Calendario: agrupa os posts agendados por dia (YYYY-MM-DD).
    by_day: dict[str, list[dict]] = {}
    for post in db.scheduled_posts():
        by_day.setdefault((post["scheduled_at"] or "")[:10], []).append(post)
    calendar_days = [{"date": d, "label": _day_label(d), "posts": by_day[d]}
                     for d in sorted(by_day)]

    # Pendencias acionaveis (instrucoes).
    attention: list[dict] = []
    for ch in channels:
        if not channel_ready[ch["id"]]:
            attention.append({
                "text": f"{ch['name']} ({ch['platform']}) ainda não conecta — falta o refresh token.",
                "cmd": (f".venv\\Scripts\\python.exe -m scripts.youtube_auth --channel {ch['id']}"
                        if ch["platform"] == "youtube" else None),
                "url": f"/channels/{ch['id']}",
            })
    episodes = db.list_episodes()
    n_failed = sum(1 for e in episodes if e["status"] == "failed")
    if n_failed:
        attention.append({"text": f"{n_failed} episódio(s) falharam no processamento.",
                          "cmd": None, "url": "/"})
    n_no_seg = sum(1 for e in episodes
                   if e["status"] == "done" and not (e.get("segment") or "").strip())
    if n_no_seg:
        attention.append({
            "text": f"{n_no_seg} episódio(s) prontos sem segmento — cortes não distribuídos.",
            "cmd": None, "url": "/"})
    if stats["posts_failed"]:
        attention.append({"text": f"{stats['posts_failed']} publicação(ões) falharam.",
                          "cmd": None, "url": "/analytics"})

    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": stats,
        "accounts": accounts,
        "calendar_days": calendar_days,
        "scheduled_count": sum(len(d["posts"]) for d in calendar_days),
        "recent_clips": db.recent_clips(),
        "top_posts": db.analytics_posts(limit=8),
        "attention": attention,
        "media_sig": security.media_signature,
    })


@app.get("/analytics", response_class=HTMLResponse, dependencies=panel)
def analytics(request: Request):
    posts = db.analytics_posts()
    totals = {
        "views": sum(p.get("views") or 0 for p in posts),
        "likes": sum(p.get("likes") or 0 for p in posts),
        "comments": sum(p.get("comments") or 0 for p in posts),
        "count": len(posts),
    }
    return templates.TemplateResponse(
        request, "analytics.html", {"posts": posts, "totals": totals}
    )


@app.get("/orders", response_class=HTMLResponse, dependencies=panel)
def orders(request: Request, ok: int = 0):
    return templates.TemplateResponse(
        request,
        "orders.html",
        {
            "orders": db.list_orders(),
            "csrf": security.csrf_token(request),
            "ok": bool(ok),
        },
    )


@app.post("/orders/{order_id}/confirm", dependencies=panel)
def confirm_order(request: Request, order_id: int, csrf: str = Form("")):
    """Confirma o Pix recebido: o pedido vira 'pago' e o worker entrega."""
    security.require_csrf(request, csrf)
    order = db.get_order(order_id)
    if order is None:
        raise HTTPException(404, "pedido nao encontrado")
    sales.confirm_payment(order_id)
    return RedirectResponse("/orders?ok=1", status_code=303)


@app.post("/orders/{order_id}/cancel", dependencies=panel)
def cancel_order(request: Request, order_id: int, csrf: str = Form("")):
    security.require_csrf(request, csrf)
    order = db.get_order(order_id)
    if order is None:
        raise HTTPException(404, "pedido nao encontrado")
    if order["status"] == "pending":
        db.update_order(order_id, status="canceled")
    return RedirectResponse("/orders", status_code=303)


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


# --------------------------------------------------------------------------- canais (multi-conta)


@app.get("/channels", response_class=HTMLResponse, dependencies=panel)
def channels_page(request: Request):
    chans = db.list_channels()
    for ch in chans:
        module = REGISTRY.get(ch["platform"])
        ch["ready"] = module.configured(ch["id"]) if module else False
    return templates.TemplateResponse(
        request,
        "channels.html",
        {
            "channels": chans,
            "platforms": list(REGISTRY.keys()),
            "markets": MARKET_OPTIONS,
            "csrf": security.csrf_token(request),
        },
    )


@app.post("/channels", dependencies=panel)
def add_channel(request: Request, name: str = Form(...), platform: str = Form(...),
                market: str = Form("BR"), niche: str = Form(""),
                posts_per_day: int = Form(3), project: str = Form(""),
                csrf: str = Form("")):
    security.require_csrf(request, csrf)
    if platform not in REGISTRY:
        raise HTTPException(400, f"plataforma desconhecida: {platform}")
    try:
        channel_id = db.create_channel(name, platform, market, niche or None,
                                       posts_per_day, project or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(f"/channels/{channel_id}", status_code=303)


@app.post("/channels/{channel_id}/settings", dependencies=panel)
def update_channel_settings(request: Request, channel_id: int, name: str = Form(...),
                            niche: str = Form(""), market: str = Form("BR"),
                            posts_per_day: int = Form(3), project: str = Form(""),
                            csrf: str = Form("")):
    """Edita nome, segmento (niche), mercado, cadencia e projeto Cloud do canal."""
    security.require_csrf(request, csrf)
    if db.get_channel(channel_id) is None:
        raise HTTPException(404, "canal nao encontrado")
    name = name.strip()
    if not name:
        raise HTTPException(400, "o canal precisa de um nome")
    db.update_channel(
        channel_id,
        name=name,
        niche=(niche.strip() or None),
        market=(market.strip() or "BR"),
        posts_per_day=max(1, int(posts_per_day or 3)),
        project=(project.strip() or None),
    )
    # Volta para a lista: a edicao acontece inline na tabela de /channels, entao
    # o usuario continua preenchendo as outras linhas sem sair da tela.
    return RedirectResponse("/channels", status_code=303)


@app.get("/channels/{channel_id}", response_class=HTMLResponse, dependencies=panel)
def channel_detail(request: Request, channel_id: int, salvo: int = 0):
    channel = db.get_channel(channel_id)
    if channel is None:
        raise HTTPException(404, "canal nao encontrado")
    platform = channel["platform"]
    keys = credentials.MANAGED.get(platform, [])
    values = {
        key: credentials.get(key, channel_id)
        for key in keys
        if key not in credentials.SECRET_KEYS
    }
    module = REGISTRY.get(platform)
    return templates.TemplateResponse(
        request,
        "channel.html",
        {
            "channel": channel,
            "markets": MARKET_OPTIONS,
            "keys": keys,
            "secret_keys": credentials.SECRET_KEYS,
            "shared_keys": credentials.SHARED_KEYS,
            "status": credentials.status(channel_id).get(platform, {}),
            "values": values,
            "ready": module.configured(channel_id) if module else False,
            "csrf": security.csrf_token(request),
            "salvo": bool(salvo),
        },
    )


@app.post("/channels/{channel_id}/credentials", dependencies=panel)
async def save_channel_credentials(request: Request, channel_id: int, csrf: str = Form("")):
    security.require_csrf(request, csrf)
    channel = db.get_channel(channel_id)
    if channel is None:
        raise HTTPException(404, "canal nao encontrado")
    form = await request.form()
    keys = credentials.MANAGED.get(channel["platform"], [])
    updates = {key: str(form.get(key) or "") for key in keys}
    credentials.save(updates, channel_id)
    return RedirectResponse(f"/channels/{channel_id}?salvo=1", status_code=303)


@app.post("/channels/{channel_id}/status", dependencies=panel)
def set_channel_status(request: Request, channel_id: int,
                       status: str = Form(...), csrf: str = Form("")):
    security.require_csrf(request, csrf)
    if status not in db.CHANNEL_STATES:
        raise HTTPException(400, f"status invalido: {status}")
    if db.get_channel(channel_id) is None:
        raise HTTPException(404, "canal nao encontrado")
    db.update_channel(channel_id, status=status)
    return RedirectResponse("/channels", status_code=303)


@app.post("/channels/{channel_id}/delete", dependencies=panel)
def remove_channel(request: Request, channel_id: int, csrf: str = Form("")):
    security.require_csrf(request, csrf)
    if db.get_channel(channel_id) is None:
        raise HTTPException(404, "canal nao encontrado")
    db.delete_channel(channel_id)
    credentials.clear_channel(channel_id)  # apaga o cofre do canal do disco
    return RedirectResponse("/channels", status_code=303)


# Idiomas de destino oferecidos na ingestao (codigo, rotulo). Derivado da fonte
# unica em config para nao divergir do roteamento e do prompt de traducao.
TARGET_LANGS = [(code, spec["label"]) for code, spec in TARGET_LANGUAGES.items()]
_TARGET_LANG_CODES = set(TARGET_LANGUAGES)


@app.post("/episodes", dependencies=panel)
def create_episode(
    request: Request,
    url: str = Form(...),
    license_status: str = Form("unknown"),
    lang_dst: str = Form(""),
    csrf: str = Form(""),
):
    security.require_csrf(request, csrf)

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "informe uma URL http(s) valida")
    if license_status not in db.LICENSE_STATES:
        license_status = "unknown"
    if lang_dst not in _TARGET_LANG_CODES:
        lang_dst = settings.target_lang

    # Reprocessar o mesmo video paga a traducao de novo — o erro mais caro que um
    # duplo clique consegue causar. Manda para o episodio existente.
    existing = db.find_active_by_url(url)
    if existing:
        return RedirectResponse(f"/episodes/{existing['id']}?duplicado=1", status_code=303)

    db.create_episode(url, license_status, lang_dst)
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
            "channels": db.list_channels(only_active=True),
            "segments": sorted({(c["niche"] or "").strip()
                                for c in db.list_channels(only_active=True) if (c["niche"] or "").strip()}),
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


@app.post("/episodes/{episode_id}/segment", dependencies=panel)
def set_segment(request: Request, episode_id: int,
                segment: str = Form(""), csrf: str = Form("")):
    """Override manual do segmento — vence a classificacao automatica na distribuicao."""
    security.require_csrf(request, csrf)
    if db.get_episode(episode_id) is None:
        raise HTTPException(404, "episodio nao encontrado")
    db.update_episode(episode_id, segment=(segment.strip() or None))
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
                     scheduled_at: str = Form(""), channel_id: str = Form(""),
                     csrf: str = Form("")):
    security.require_csrf(request, csrf)

    clip = db.get_clip(clip_id)
    if clip is None:
        raise HTTPException(404, "corte nao encontrado")
    if clip.get("status") != "ready" or not clip.get("path"):
        raise HTTPException(400, "corte ainda nao foi renderizado")

    # Um canal escolhido define a plataforma (e as credenciais). Sem canal, cai no
    # cofre global — o comportamento de conta unica de antes.
    chan_id = _channel_arg(channel_id)
    if chan_id is not None:
        channel = db.get_channel(chan_id)
        if channel is None:
            raise HTTPException(400, "canal nao encontrado")
        platform = channel["platform"]

    if platform not in REGISTRY:
        raise HTTPException(400, f"plataforma desconhecida: {platform}")
    if orientation not in ("vertical", "horizontal"):
        raise HTTPException(400, "orientacao invalida")
    if orientation == "horizontal" and not clip.get("path_wide"):
        raise HTTPException(400, "este corte nao tem versao horizontal (16:9) renderizada")

    db.create_post(clip_id, platform, _parse_schedule(scheduled_at), orientation, chan_id)
    return RedirectResponse(f"/episodes/{clip['episode_id']}", status_code=303)


@app.post("/clips/{clip_id}/publish_many", dependencies=panel)
async def publish_many(request: Request, clip_id: int):
    """Enfileira o mesmo corte em varios canais de uma vez, com espacamento.

    O espacamento (stagger) escalona os horarios de publicacao entre as contas —
    postar dezenas de contas no mesmo segundo e o padrao classico que dispara os
    detectores de spam multi-conta.

    Le tudo do form manualmente (channel_ids e multivalorado); por isso o CSRF
    tambem vem do form, nao de um parametro Form separado.
    """
    form = await request.form()
    security.require_csrf(request, str(form.get("csrf") or ""))

    clip = db.get_clip(clip_id)
    if clip is None:
        raise HTTPException(404, "corte nao encontrado")
    if clip.get("status") != "ready" or not clip.get("path"):
        raise HTTPException(400, "corte ainda nao foi renderizado")

    raw_ids = form.getlist("channel_ids")
    orientation = form.get("orientation") or "vertical"
    if orientation not in ("vertical", "horizontal"):
        raise HTTPException(400, "orientacao invalida")
    if orientation == "horizontal" and not clip.get("path_wide"):
        raise HTTPException(400, "este corte nao tem versao horizontal (16:9) renderizada")
    try:
        stagger = max(0, int(form.get("stagger_minutes") or 0))
    except ValueError:
        stagger = 0

    base = datetime.now(timezone.utc)
    created = 0
    for i, raw in enumerate(raw_ids):
        chan_id = _channel_arg(raw)
        if chan_id is None:
            continue
        channel = db.get_channel(chan_id)
        if channel is None or channel["status"] != "active":
            continue
        # Escalonado: cada canal seguinte publica `stagger` min depois. Sem stagger,
        # entra imediato (scheduled_at NULL).
        sched = None
        if stagger:
            sched = (base + timedelta(minutes=stagger * i)).isoformat(timespec="seconds")
        db.create_post(clip_id, channel["platform"], sched, orientation, chan_id)
        created += 1

    if created == 0:
        raise HTTPException(400, "selecione ao menos um canal ativo")
    return RedirectResponse(f"/episodes/{clip['episode_id']}", status_code=303)


def _channel_arg(raw: str) -> int | None:
    """Converte o campo channel_id do formulario em int, ou None quando vazio."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(400, "channel_id invalido")


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
            "eta_seconds": db.eta_seconds(episode),
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
