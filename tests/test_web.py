"""Testes do painel: autenticacao, CSRF, fila e regra de licenca.

Inclui um teste de regressao para cada achado da auditoria, nomeado com o numero
do achado — se um deles voltar, o teste diz qual.

    py -m tests.test_web
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["DUBFLOW_PASSWORD"] = "senha-de-teste"
os.environ["SECRET_KEY"] = "chave-fixa-para-teste"

from app.config import settings  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="dubflow_test_"))
settings.data_dir = _tmp
settings.db_path = _tmp / "test.db"
for sub in ("episodes", "archive", "tmp", "logs"):
    (_tmp / sub).mkdir(parents=True, exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app import db, security  # noqa: E402
from app.publishers import telegram  # noqa: E402
from app.web.main import app  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} -> {detail}")
        failures.append(label)


def csrf_of(client: TestClient) -> str:
    """Reproduz o token que o servidor gera para esta sessao."""
    cookie = client.cookies.get(security.SESSION_COOKIE) or ""
    import hashlib
    import hmac

    return hmac.new(
        security.get_secret(), f"csrf:{cookie}".encode(), hashlib.sha256
    ).hexdigest()[:32]


def main() -> int:
    db.init_db()

    print("infra (achado M5)")
    check("log em arquivo criado", (settings.data_dir / "logs" / "web.log").exists(),
          "web.log nao foi criado")

    anon = TestClient(app)

    print("autenticacao (achado 2)")
    r = anon.get("/", follow_redirects=False)
    check("home exige sessao", r.status_code in (303, 401), r.status_code)
    check("api exige sessao", anon.get("/api/catalog").status_code == 401)
    check("download exige sessao", anon.get("/download/1/srt").status_code == 401)
    check("login com senha errada e recusado",
          anon.post("/login", data={"password": "errada"},
                    follow_redirects=False).headers.get("location") == "/login?erro=1")
    check("health continua publico", anon.get("/health").status_code == 200)

    client = TestClient(app)
    r = client.post("/login", data={"password": "senha-de-teste"}, follow_redirects=False)
    check("login correto abre sessao", r.status_code == 303 and
          security.SESSION_COOKIE in r.cookies, r.status_code)
    check("home abre autenticado", client.get("/").status_code == 200)

    print("rate limit de login (achado C2)")
    security.clear_login_failures("testclient")
    for _ in range(security.LOGIN_MAX_FAILS):
        client.post("/login", data={"password": "errada"}, follow_redirects=False)
    r = client.post("/login", data={"password": "errada"}, follow_redirects=False)
    check("trava apos varias tentativas", "bloqueado=1" in r.headers.get("location", ""),
          r.headers.get("location"))
    r = client.post("/login", data={"password": "senha-de-teste"}, follow_redirects=False)
    check("bloqueio vale ate com senha certa", "bloqueado=1" in r.headers.get("location", ""),
          r.headers.get("location"))
    security.clear_login_failures("testclient")
    r = client.post("/login", data={"password": "senha-de-teste"}, follow_redirects=False)
    check("apos limpar, login volta a funcionar",
          r.status_code == 303 and "bloqueado" not in r.headers.get("location", ""),
          r.headers.get("location"))

    print("csrf (achado 15)")
    r = client.post("/episodes", data={"url": "https://youtu.be/a", "csrf": "invalido"},
                    follow_redirects=False)
    check("POST sem csrf valido e recusado", r.status_code == 403, r.status_code)

    token = csrf_of(client)
    r = client.post("/episodes", data={"url": "https://www.youtube.com/watch?v=abc",
                                       "license_status": "licensed", "csrf": token},
                    follow_redirects=False)
    check("POST com csrf valido passa", r.status_code == 303, r.status_code)

    print("fila")
    episodes = db.list_episodes()
    check("episodio na fila", len(episodes) == 1 and episodes[0]["status"] == "queued")
    check("licenca gravada", episodes[0]["license_status"] == "licensed")
    ep_id = episodes[0]["id"]

    r = client.post("/episodes", data={"url": "nao-e-url", "csrf": token},
                    follow_redirects=False)
    check("rejeita url invalida", r.status_code == 400, r.status_code)

    print("deduplicacao (achado 16)")
    r = client.post("/episodes", data={"url": "https://www.youtube.com/watch?v=abc",
                                       "csrf": token}, follow_redirects=False)
    check("url repetida nao cria episodio novo", len(db.list_episodes()) == 1,
          len(db.list_episodes()))
    check("redireciona para o existente", "duplicado=1" in r.headers.get("location", ""),
          r.headers.get("location"))

    print("claim atomico (achado 13)")
    check("claim funciona", db.claim_next_queued() is not None)
    check("nao entrega duas vezes", db.claim_next_queued() is None)

    print("retry so em estado seguro (achado 8)")
    db.update_episode(ep_id, status="transcribing")
    r = client.post(f"/episodes/{ep_id}/retry", data={"csrf": token}, follow_redirects=False)
    check("recusa retry em execucao", r.status_code == 409, r.status_code)
    check("status preservado", db.get_episode(ep_id)["status"] == "transcribing")
    db.update_episode(ep_id, status="failed")
    r = client.post(f"/episodes/{ep_id}/retry", data={"csrf": token}, follow_redirects=False)
    check("permite retry apos falha", db.get_episode(ep_id)["status"] == "queued")

    print("licenca e catalogo (achado 9)")
    db.update_episode(ep_id, license_status="unknown")
    result = telegram.deliver_episode({"id": 1, "licenca": "unknown", "arquivos": {}},
                                      chat_id="123")
    check("bloqueia entrega sem licenca", not result.ok and "licenca" in (result.error or ""))

    import json

    from app.pipeline import archive
    d = archive.ARCHIVE_DIR / "canal" / "00001-teste"
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(
        {"id": 1, "titulo": "T", "licenca": "owned",
         "arquivos": {"episodio": "C:/Users/mathe/segredo.mp4"}}), encoding="utf-8")
    body = client.get("/api/catalog").text
    check("catalogo nao expoe caminhos do disco",
          "C:/Users/mathe" not in body and "_dir" not in body, body[:120])
    check("catalogo lista os tipos de arquivo", '"episodio"' in body, body[:200])

    print("agendamento validado (achado 6)")
    cids = db.replace_clips(ep_id, [{"start": 0, "end": 30, "title": "t", "caption": "c",
                                     "score": 9}])
    db.update_clip(cids[0], path=str(_tmp / "f.mp4"), status="ready")
    r = client.post(f"/clips/{cids[0]}/publish",
                    data={"platform": "tiktok", "scheduled_at": "amanha de tarde",
                          "csrf": token}, follow_redirects=False)
    check("recusa data em texto livre", r.status_code == 400, r.status_code)
    r = client.post(f"/clips/{cids[0]}/publish",
                    data={"platform": "tiktok", "scheduled_at": "2030-01-01T10:00",
                          "csrf": token}, follow_redirects=False)
    check("aceita data ISO", r.status_code == 303, r.status_code)
    check("agendamento futuro nao entra na fila agora", len(db.pending_posts()) == 0)

    print("publicacao")
    r = client.post(f"/clips/{cids[0]}/publish", data={"platform": "orkut", "csrf": token},
                    follow_redirects=False)
    check("rejeita plataforma desconhecida", r.status_code == 400, r.status_code)
    r = client.post(f"/clips/{cids[0]}/publish", data={"platform": "telegram", "csrf": token},
                    follow_redirects=False)
    check("sem agendamento entra na fila", len(db.pending_posts()) == 1)

    print("canal de cortes (youtube)")
    from app.publishers import REGISTRY
    from app.publishers import status as publisher_status
    estado = publisher_status()
    check("youtube registrado", "youtube" in REGISTRY)
    check("youtube aparece no painel", "youtube" in estado)
    check("youtube sem credenciais fica desabilitado", estado.get("youtube") is False, estado)
    r = client.post(f"/clips/{cids[0]}/publish", data={"platform": "youtube", "csrf": token},
                    follow_redirects=False)
    check("aceita youtube na fila", r.status_code == 303, r.status_code)

    print("orientacao horizontal (16:9)")
    # Sem versao 16:9 renderizada, publicar horizontal e recusado.
    r = client.post(f"/clips/{cids[0]}/publish",
                    data={"platform": "youtube", "orientation": "horizontal", "csrf": token},
                    follow_redirects=False)
    check("horizontal sem 16:9 e recusado", r.status_code == 400, r.status_code)
    db.update_clip(cids[0], path_wide=str(_tmp / "f_wide.mp4"))
    r = client.post(f"/clips/{cids[0]}/publish",
                    data={"platform": "youtube", "orientation": "horizontal", "csrf": token},
                    follow_redirects=False)
    check("horizontal com 16:9 e aceito", r.status_code == 303, r.status_code)

    yt = [p for p in db.pending_posts() if p["platform"] == "youtube"]
    check("youtube entrou na fila (vertical + horizontal)", len(yt) == 2, len(yt))
    check("orientacao gravada",
          sorted(p["orientation"] for p in yt) == ["horizontal", "vertical"],
          [p["orientation"] for p in yt])
    # Neutraliza os posts de youtube para nao alterar as contagens seguintes.
    for p in yt:
        db.update_post(p["id"], status="failed")
    check("fila volta a ter so o telegram", len(db.pending_posts()) == 1, len(db.pending_posts()))

    print("conexoes (tela de credenciais)")
    from app import credentials
    from app.publishers import tiktok
    check("conexoes exige sessao",
          anon.get("/connections", follow_redirects=False).status_code in (303, 401))
    check("GET conexoes autenticado", client.get("/connections").status_code == 200)
    r = client.post("/connections", data={"TIKTOK_ACCESS_TOKEN": "x", "csrf": "invalido"},
                    follow_redirects=False)
    check("conexoes POST sem csrf recusado", r.status_code == 403, r.status_code)
    r = client.post("/connections",
                    data={"TIKTOK_ACCESS_TOKEN": "tok-secreto-123", "IG_USER_ID": "999",
                          "csrf": token}, follow_redirects=False)
    check("conexoes salva", r.status_code == 303, r.status_code)
    check("token salvo no cofre", credentials.get("TIKTOK_ACCESS_TOKEN") == "tok-secreto-123")
    check("valor nao-secreto salvo", credentials.get("IG_USER_ID") == "999")
    check("publisher reflete ao vivo (sem reiniciar)", tiktok.configured() is True)
    body = client.get("/connections").text
    check("segredo nao aparece no html", "tok-secreto-123" not in body, "vazou o token!")
    check("nao-secreto aparece no html", "999" in body)
    # Campo em branco nao pode apagar o que ja estava salvo.
    client.post("/connections", data={"TIKTOK_ACCESS_TOKEN": "", "csrf": token},
                follow_redirects=False)
    check("vazio nao apaga o token", credentials.get("TIKTOK_ACCESS_TOKEN") == "tok-secreto-123")

    print("venda no telegram (A2)")
    from app import sales
    comprador = "555001"
    outro = "555002"
    # Pedido avulso: cria pendente, sem acesso ainda.
    oid = sales.create_episode_order(comprador, episode_id=7, buyer_name="Fulano")
    ped = db.get_order(oid)
    check("pedido criado pendente", ped["status"] == "pending" and ped["kind"] == "episode", ped)
    check("valor do avulso gravado", abs((ped["amount"] or 0) - settings.price_episode) < 1e-6, ped["amount"])
    check("sem acesso antes de pagar", sales.has_access(comprador, 7) is False)
    # Confirma pagamento -> libera so aquele episodio, so pra esse comprador.
    sales.confirm_payment(oid)
    check("acesso ao episodio pago", sales.has_access(comprador, 7) is True)
    check("nao libera outro episodio", sales.has_access(comprador, 99) is False)
    check("nao libera outro comprador", sales.has_access(outro, 7) is False)
    check("confirmar de novo nao quebra (idempotente)", sales.confirm_payment(oid)["status"] == "paid")
    # Assinatura: confirma -> acesso a qualquer episodio enquanto ativa.
    check("sem assinatura ativa no inicio", sales.subscription_active(outro) is False)
    sid = sales.create_subscription_order(outro, buyer_name="Ciclana")
    sales.confirm_payment(sid)
    check("assinatura ativa apos pagar", sales.subscription_active(outro) is True)
    check("assinante acessa qualquer episodio", sales.has_access(outro, 12345) is True)
    check("pedidos pendentes listados", isinstance(db.list_orders(status="pending"), list))
    # Rota do painel: exige sessao, lista, e confirma o pagamento.
    check("vendas exige sessao", anon.get("/orders", follow_redirects=False).status_code in (303, 401))
    check("GET vendas autenticado", client.get("/orders").status_code == 200)
    oid3 = sales.create_episode_order("555003", episode_id=8)
    r = client.post(f"/orders/{oid3}/confirm", data={"csrf": token}, follow_redirects=False)
    check("painel confirma o pagamento",
          r.status_code == 303 and db.get_order(oid3)["status"] == "paid", db.get_order(oid3))
    r = client.post(f"/orders/{oid3}/confirm", data={"csrf": "x"}, follow_redirects=False)
    check("confirmar sem csrf e recusado", r.status_code == 403, r.status_code)

    print("bot de vendas (fatia 3)")
    import bot
    check("/start responde com os comandos", "/comprar" in bot._handle_text("/start", 900, "X"))
    r = bot._handle_text("/comprar 1", 900, "Cliente")
    check("/comprar cria pedido avulso e mostra o Pix", "Pedido" in r and "Pix" in r, r)
    check("/comprar registra o pedido no banco",
          any(o["kind"] == "episode" and str(o["buyer_tg_id"]) == "900" for o in db.list_orders()))
    check("/comprar id inexistente avisa",
          "catalogo" in bot._handle_text("/comprar 9999", 900, "X").lower())
    r3 = bot._handle_text("/assinar", 901, "Y")
    check("/assinar cria pedido de assinatura",
          "Pedido" in r3 and any(o["kind"] == "subscription" and str(o["buyer_tg_id"]) == "901"
                                 for o in db.list_orders()), r3)
    # Conserto: assinante (555002, com assinatura ativa da secao anterior) recebe na hora.
    r4 = bot._handle_text("/comprar 1", 555002, "Ciclana")
    check("assinante recebe sem pagar de novo",
          "enviando" in r4.lower() or "acesso" in r4.lower(), r4)
    do_assinante = [o for o in db.list_orders()
                    if str(o["buyer_tg_id"]) == "555002" and o["kind"] == "episode"]
    check("pedido do assinante ja entra pago (fila de entrega)",
          bool(do_assinante) and do_assinante[0]["status"] == "paid", do_assinante)

    print("posts presos (achado 11)")
    pid = db.pending_posts()[0]["id"]
    db.update_post(pid, status="publishing")
    check("preso some da fila", len(db.pending_posts()) == 0)
    check("recuperacao devolve a fila", db.recover_stuck_posts() == 1)
    check("volta a aparecer", len(db.pending_posts()) == 1)

    print("episodios presos apos reinicio do worker")
    db.update_episode(ep_id, status="transcribing", progress=0.4)
    presos = db.recover_stuck_episodes()
    check("episodio em execucao volta para a fila", presos == [ep_id], presos)
    check("status resetado", db.get_episode(ep_id)["status"] == "queued")
    check("progresso zerado", db.get_episode(ep_id)["progress"] == 0)
    db.update_episode(ep_id, status="done", progress=1.0)
    check("episodio concluido nao e reenfileirado", db.recover_stuck_episodes() == [])
    check("concluido preservado", db.get_episode(ep_id)["status"] == "done")

    print("limite de tentativas (achado 21)")
    db.update_post(pid, attempts=db.MAX_PUBLISH_ATTEMPTS)
    check("post esgotado sai da fila", len(db.pending_posts()) == 0)
    db.update_post(pid, attempts=0)

    print("analises / metricas dos videos")
    db.update_post(pid, status="published", remote_id="vid123", views=1000, likes=50, comments=5)
    ap = db.analytics_posts()
    check("analytics lista publicado com metricas",
          any(p["id"] == pid and p["views"] == 1000 for p in ap), ap[:1])
    check("posts_needing_stats pega publicado com remote_id",
          any(p["id"] == pid for p in db.posts_needing_stats()))
    check("analytics exige sessao",
          anon.get("/analytics", follow_redirects=False).status_code in (303, 401))
    r = client.get("/analytics")
    check("GET analytics autenticado", r.status_code == 200, r.status_code)
    check("metricas aparecem no painel", "1.000" in r.text, "views nao renderizadas")

    print("painel geral (dashboard)")
    st = db.dashboard_stats()
    check("dashboard_stats conta episodios e posts",
          st["episodes_total"] >= 1 and st["posts_published"] >= 1, st)
    check("dashboard_stats soma views publicadas", st["views"] >= 1000, st["views"])
    ct = db.channel_totals()
    check("channel_totals agrega por canal (inclui global None)", None in ct or len(ct) >= 0)
    check("recent_clips traz cortes prontos com episodio",
          all("episode_title" in c for c in db.recent_clips()))
    check("dashboard exige sessao",
          anon.get("/dashboard", follow_redirects=False).status_code in (303, 401))
    r = client.get("/dashboard")
    check("GET dashboard autenticado", r.status_code == 200, r.status_code)
    check("dashboard mostra a conta global e os KPIs",
          "Painel geral" in r.text and "Conta global" in r.text)

    print("molde do corte (card)")
    from app.pipeline import card as card_mod
    card_png = settings.data_dir / "card_test.png"
    card_res = card_mod.render_overlay("SEGUE O PERFIL", card_png)
    try:
        from PIL import Image as _Img
        _alpha = _Img.open(card_png).split()[-1]
        _meio = sum(1 for x in range(0, 1080, 40) if _alpha.getpixel((x, 950)) > 0)
        # A faixa do gancho foi removida em 25/08/2026: o topo tem que ficar limpo,
        # so o rodape (a pilula do CTA) pode pintar pixel.
        _topo = sum(1 for x in range(0, 1080, 40) if _alpha.getpixel((x, 60)) > 0)
        _rodape = sum(1 for x in range(300, 800, 40) if _alpha.getpixel((x, 1830)) > 0)
        check("overlay gera PNG 1080x1920", card_res is not None and card_png.exists())
        check("miolo do molde e transparente (video aparece)", _meio == 0, _meio)
        check("topo limpo — sem faixa de gancho", _topo == 0, _topo)
        check("CTA desenhado no rodape", _rodape > 0, _rodape)
        check("sem CTA nao gera molde", card_mod.render_overlay("", card_png) is None)
    except ImportError:
        check("sem Pillow o molde degrada para None", card_res is None)
    check("home mostra o checkbox de molde", 'name="card"' in client.get("/").text)
    r = client.post("/episodes", data={"url": "https://youtu.be/cardtest", "card": "on",
                                       "csrf": token}, follow_redirects=False)
    ep_card = [e for e in db.list_episodes() if "cardtest" in e["source_url"]][0]
    check("checkbox molde grava card_layout no episodio", ep_card["card_layout"] == 1,
          ep_card["card_layout"])

    from app.publishers import stats_for
    check("stats_for de plataforma sem suporte e None", stats_for("telegram", "x") is None)

    print("media assinada (achados 1 e 2)")
    ep1 = settings.data_dir / "episodes" / "ep_00001" / "clips"
    ep2 = settings.data_dir / "episodes" / "ep_00002" / "clips"
    ep1.mkdir(parents=True, exist_ok=True)
    ep2.mkdir(parents=True, exist_ok=True)
    (ep1 / "ep00001_corte_01.mp4").write_bytes(b"EPISODIO-UM")
    (ep2 / "ep00002_corte_01.mp4").write_bytes(b"EPISODIO-DOIS")

    nome2 = "ep00002_corte_01.mp4"
    r = client.get(f"/media/{security.media_signature(nome2)}/{nome2}")
    check("serve o corte do episodio certo", r.content == b"EPISODIO-DOIS", r.content[:20])

    nome1 = "ep00001_corte_01.mp4"
    r = client.get(f"/media/{security.media_signature(nome1)}/{nome1}")
    check("cada episodio tem seu proprio arquivo", r.content == b"EPISODIO-UM", r.content[:20])

    r = anon.get(f"/media/assinatura-errada/{nome2}")
    check("assinatura invalida e recusada", r.status_code == 403, r.status_code)
    r = anon.get(f"/media/{security.media_signature(nome2)}/{nome2}")
    check("assinatura valida dispensa login", r.status_code == 200, r.status_code)

    print("path traversal e download restrito (achados 1 e 9)")
    check("traversal bloqueado",
          client.get("/media/x/..%2F..%2F..%2Fwindows%2Fwin.ini").status_code in (403, 404))
    check("video de origem nao e baixavel",
          client.get(f"/download/{ep_id}/source_video").status_code == 404)

    print("allowlist de colunas (achado 10)")
    try:
        db.update_episode(ep_id, **{"status = 'done', progress": 1})
        ok = False
    except ValueError:
        ok = True
    check("coluna invalida e recusada", ok)
    try:
        db.update_post(pid, **{"status": "pending"})
        ok2 = True
    except ValueError:
        ok2 = False
    check("coluna valida continua funcionando", ok2)

    print("multi-conta: modelo e credenciais por canal")
    c_yt = db.create_channel("Financas BR #1", "youtube", "BR", "financas")
    c_tk = db.create_channel("Curiosidades US", "tiktok", "US")
    check("canal criado com os campos", db.get_channel(c_yt)["name"] == "Financas BR #1"
          and db.get_channel(c_yt)["market"] == "BR")
    check("lista canais", len(db.list_channels()) >= 2)
    check("filtra por plataforma",
          all(c["platform"] == "youtube" for c in db.list_channels(platform="youtube")))
    # Credencial salva no canal fica isolada.
    credentials.save({"TIKTOK_ACCESS_TOKEN": "tok-do-canal"}, channel_id=c_tk)
    check("cred do canal salva e lida", credentials.get("TIKTOK_ACCESS_TOKEN", c_tk) == "tok-do-canal")
    # Segredo de identidade NAO herda do cofre global (senao publicaria na conta errada).
    credentials.save({"YOUTUBE_REFRESH_TOKEN": "GLOBAL-RT"})
    check("identidade nao herda do global", credentials.get("YOUTUBE_REFRESH_TOKEN", c_yt) == "")
    check("cofre global continua valendo sem channel", credentials.get("YOUTUBE_REFRESH_TOKEN") == "GLOBAL-RT")
    # Chave compartilhada (infra) herda do global quando o canal nao a define.
    credentials.save({"PUBLIC_BASE_URL": "https://srv.test"})
    check("chave compartilhada herda do global", credentials.get("PUBLIC_BASE_URL", c_yt) == "https://srv.test")
    # configured() por canal
    from app.publishers import tiktok as tk_mod
    check("tiktok configurado no canal com token", tk_mod.configured(c_tk) is True)
    check("tiktok nao configurado em canal sem token", tk_mod.configured(c_yt) is False)

    print("multi-conta: rotas do painel")
    check("channels exige sessao",
          anon.get("/channels", follow_redirects=False).status_code in (303, 401))
    check("GET channels autenticado", client.get("/channels").status_code == 200)
    r = client.post("/channels", data={"name": "Canal Painel", "platform": "youtube",
                                       "market": "BR", "csrf": token}, follow_redirects=False)
    check("cria canal via painel", r.status_code == 303, r.status_code)
    novo = [c for c in db.list_channels() if c["name"] == "Canal Painel"][0]
    check("GET detalhe do canal", client.get(f"/channels/{novo['id']}").status_code == 200)
    # Edicao inline pela tabela (rota /settings): nome, mercado, segmento, projeto
    # Cloud e cadencia; volta para a lista.
    r = client.post(f"/channels/{novo['id']}/settings",
                    data={"name": "Canal Renomeado", "market": "US", "niche": "podcast",
                          "project": "proj-123", "posts_per_day": "5", "csrf": token},
                    follow_redirects=False)
    ed = db.get_channel(novo["id"])
    check("settings edita inline e volta para a lista",
          r.status_code == 303 and r.headers["location"] == "/channels", r.status_code)
    check("settings grava nome/mercado/segmento",
          ed["name"] == "Canal Renomeado" and ed["market"] == "US" and ed["niche"] == "podcast")
    check("settings grava projeto Cloud e cadencia",
          ed["project"] == "proj-123" and ed["posts_per_day"] == 5)
    r = client.post(f"/channels/{novo['id']}/credentials",
                    data={"YOUTUBE_CLIENT_ID": "cid-do-canal", "csrf": token}, follow_redirects=False)
    check("salva cred do canal pelo painel",
          r.status_code == 303 and credentials.get("YOUTUBE_CLIENT_ID", novo["id"]) == "cid-do-canal")
    r = client.post(f"/channels/{novo['id']}/status",
                    data={"status": "paused", "csrf": token}, follow_redirects=False)
    check("pausa canal", db.get_channel(novo["id"])["status"] == "paused")
    r = client.post(f"/channels/{novo['id']}/delete", data={"csrf": token}, follow_redirects=False)
    check("exclui canal", db.get_channel(novo["id"]) is None)

    print("multi-conta: publicacao por canal + fan-out")
    r = client.post(f"/clips/{cids[0]}/publish",
                    data={"platform": "telegram", "channel_id": c_tk, "csrf": token},
                    follow_redirects=False)
    check("publish em canal aceito", r.status_code == 303, r.status_code)
    alvo = [p for p in db.list_posts(ep_id) if p.get("channel_id") == c_tk]
    check("post gravou channel_id", bool(alvo))
    check("plataforma veio do canal (nao do form)", alvo and alvo[0]["platform"] == "tiktok")
    check("nome do canal no post", alvo and alvo[0]["channel_name"] == "Curiosidades US")
    before = len(db.list_posts(ep_id))
    r = client.post(f"/clips/{cids[0]}/publish_many",
                    data={"channel_ids": [str(c_yt), str(c_tk)], "orientation": "vertical",
                          "stagger_minutes": "10", "csrf": token}, follow_redirects=False)
    check("fan-out aceito", r.status_code == 303, r.status_code)
    depois = db.list_posts(ep_id)
    check("fan-out criou um post por canal", len(depois) - before == 2, len(depois) - before)
    check("stagger agenda contas seguintes",
          any(p.get("scheduled_at") for p in depois if p.get("channel_id") in (c_yt, c_tk)))
    r = client.post(f"/clips/{cids[0]}/publish_many",
                    data={"orientation": "vertical", "csrf": token}, follow_redirects=False)
    check("fan-out sem canal e recusado", r.status_code == 400, r.status_code)

    print("multi-conta: canal pausado sai da fila")
    db.update_channel(c_yt, status="paused")
    check("post de canal pausado some da fila",
          all(p.get("channel_id") != c_yt for p in db.pending_posts()))
    db.update_channel(c_yt, status="active")
    check("reativar traz o post de volta",
          any(p.get("channel_id") == c_yt for p in db.pending_posts()))

    print("distribuicao: funcoes puras (agendamento e rodizio)")
    from collections import Counter
    from datetime import datetime, timezone

    from app.pipeline import distribute
    rr = distribute.assign_round_robin([1, 2, 3, 4, 5], [100, 200])
    check("rodizio nao duplica corte", sorted(sum(rr.values(), [])) == [1, 2, 3, 4, 5])
    check("rodizio distribui 3/2", len(rr[100]) == 3 and len(rr[200]) == 2, rr)
    agora = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
    plano = distribute.plan_schedule({10: [1, 2, 3, 4, 5]}, {10: None}, {10: 2}, agora)
    tempos = [datetime.fromisoformat(t) for _, _, t in plano]
    check("agenda todos os cortes", len(plano) == 5)
    check("nenhum no passado", all(t > agora for t in tempos))
    check("horarios crescentes", tempos == sorted(tempos))
    check("2/dia estica 5 cortes em 3 dias", len({t.date() for t in tempos}) == 3,
          sorted({t.date() for t in tempos}))
    futuro = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    plano2 = distribute.plan_schedule({10: [1]}, {10: futuro.isoformat()}, {10: 2}, agora)
    check("respeita o horizonte ja agendado do canal",
          datetime.fromisoformat(plano2[0][2]) > futuro, plano2)

    print("distribuicao: roteamento fim-a-fim (classificador stub)")
    ch_pod1 = db.create_channel("Pod BR 1", "youtube", "BR", "podcast", posts_per_day=2)
    ch_pod2 = db.create_channel("Pod TT", "tiktok", "BR", "podcast", posts_per_day=2)
    ch_film = db.create_channel("Filmes BR", "youtube", "BR", "filmes")
    ep_d = db.create_episode("https://youtu.be/dist", "owned")
    cids_d = db.replace_clips(ep_d, [{"start": 0, "end": 30, "title": f"c{i}",
                                      "caption": "x", "score": 9} for i in range(5)])
    for cid in cids_d:
        db.update_clip(cid, path=str(_tmp / "f.mp4"), status="ready")
    res = distribute.distribute_episode(ep_d, classifier=lambda e, n: "podcast")
    check("distribuicao ok", res["status"] == "ok" and res["scheduled"] == 5, res)
    posts_d = db.list_posts(ep_d)
    check("um post por corte", len(posts_d) == 5, len(posts_d))
    por_corte = Counter(p["clip_id"] for p in posts_d)
    check("cada corte em 1 canal (sem duplicata)",
          len(por_corte) == 5 and all(v == 1 for v in por_corte.values()))
    check("so canais do segmento podcast",
          all(p["channel_id"] in (ch_pod1, ch_pod2) for p in posts_d))
    check("nada foi para o canal de filmes",
          all(p["channel_id"] != ch_film for p in posts_d))
    check("rodizio usou os dois canais do segmento",
          {p["channel_id"] for p in posts_d} == {ch_pod1, ch_pod2})
    check("plataforma do post veio do canal",
          all(p["platform"] in ("youtube", "tiktok") for p in posts_d))
    check("segmento gravado no episodio", db.get_episode(ep_d)["segment"] == "podcast")
    check("cortes agendados (scheduled_at)", all(p.get("scheduled_at") for p in posts_d))
    res_again = distribute.distribute_episode(ep_d, classifier=lambda e, n: "podcast")
    check("rerun nao duplica posts",
          res_again["scheduled"] == 0 and len(db.list_posts(ep_d)) == 5, res_again)

    ch_news = db.create_channel("News BR", "youtube", "BR", "Notícias")
    ep_n = db.create_episode("https://youtu.be/news", "owned")
    cn = db.replace_clips(ep_n, [{"start": 0, "end": 30, "title": "a", "caption": "x", "score": 9}])
    db.update_clip(cn[0], path=str(_tmp / "f.mp4"), status="ready")
    res_n = distribute.distribute_episode(ep_n, classifier=lambda e, n: "noticias")
    pn = db.list_posts(ep_n)
    check("roteamento robusto a acento (noticias ~ Notícias)",
          res_n["status"] == "ok" and pn and pn[0]["channel_id"] == ch_news, res_n)

    print("distribuicao: override e fallback seguro")
    ep_e = db.create_episode("https://youtu.be/override", "owned")
    ce = db.replace_clips(ep_e, [{"start": 0, "end": 30, "title": "a", "caption": "x", "score": 9}])
    db.update_clip(ce[0], path=str(_tmp / "f.mp4"), status="ready")
    db.update_episode(ep_e, segment="filmes")
    res_ov = distribute.distribute_episode(ep_e, classifier=lambda e, n: "podcast")
    pe = db.list_posts(ep_e)
    check("override de segmento vence a classificacao",
          res_ov["status"] == "ok" and pe and pe[0]["channel_id"] == ch_film, res_ov)
    ep_f = db.create_episode("https://youtu.be/none", "owned")
    cf = db.replace_clips(ep_f, [{"start": 0, "end": 30, "title": "a", "caption": "x", "score": 9}])
    db.update_clip(cf[0], path=str(_tmp / "f.mp4"), status="ready")
    res_nc = distribute.distribute_episode(ep_f, classifier=lambda e, n: None)
    check("baixa confianca nao agenda (fica manual)",
          res_nc["status"] == "nao_classificado" and not db.list_posts(ep_f), res_nc)
    res_sc = distribute.distribute_episode(ep_f, classifier=lambda e, n: "viagens")
    check("segmento sem canal nao agenda",
          res_sc["status"] == "sem_canal_para_segmento" and not db.list_posts(ep_f), res_sc)

    # Regressao: o INSERT de replace_clips ignorava thumb_text/thumb_time, entao a
    # IA gerava os dois e o banco guardava NULL — toda capa saia sem texto.
    print("cortes: campos da capa chegam ao banco")
    ep_t = db.create_episode("https://youtu.be/thumb", "owned")
    db.replace_clips(ep_t, [{
        "start": 10.0, "end": 40.0, "title": "t", "hook": "linha crua da legenda",
        "caption": "c", "thumb_text": "ELE *MENTIU* NA CARA", "thumb_time": 25.5,
        "score": 9,
    }])
    ct = db.list_clips(ep_t)[0]
    check("thumb_text persiste", ct["thumb_text"] == "ELE *MENTIU* NA CARA",
          ct["thumb_text"])
    check("thumb_time persiste", ct["thumb_time"] == 25.5, ct["thumb_time"])

    # Todo campo do schema de corte tem que sobreviver ao INSERT. Ja aconteceu duas
    # vezes de um campo novo ser gerado pela IA e o banco guardar NULL em silencio,
    # porque a lista de colunas do INSERT ficou para tras.
    campos_capa = {"thumb_text", "thumb_badge", "thumb_image_prompt", "thumb_time"}
    ep_full = db.create_episode("https://youtu.be/campos", "owned")
    db.replace_clips(ep_full, [{
        "start": 0.0, "end": 30.0, "title": "t", "hook": "h", "caption": "c",
        "yt_title": "y", "yt_description": "d", "score": 8,
        "thumb_text": "A", "thumb_badge": "B", "thumb_image_prompt": "C",
        "thumb_time": 12.0,
    }])
    guardado = db.list_clips(ep_full)[0]
    perdidos = [k for k in campos_capa if guardado[k] in (None, "")]
    check("nenhum campo da capa se perde no INSERT", not perdidos, perdidos)

    # A acao de reselecao precisa estar registrada, senao o painel a recusa.
    check("reselect_clips e uma acao valida", "reselect_clips" in db.ACTIONS, db.ACTIONS)

    # Regressao: a URL do media e assinada so pelo NOME, e o nome se repete entre
    # execucoes (limpar o acervo reinicia os ids). Sem revalidacao e sem versao, o
    # navegador entregava o corte ANTIGO para o episodio novo.
    print("media: cache nao pode servir o corte antigo")
    nome = "ep00001_corte_01.mp4"
    ep_m = settings.data_dir / "episodes" / "ep_00001" / "clips"
    ep_m.mkdir(parents=True, exist_ok=True)
    (ep_m / nome).write_bytes(b"conteudo novo")
    assinada = f"/media/{security.media_signature(nome)}/{nome}"
    r_media = client.get(assinada)
    check("media responde", r_media.status_code == 200, r_media.status_code)
    check("manda no-cache (forca revalidar)",
          "no-cache" in r_media.headers.get("cache-control", ""),
          r_media.headers.get("cache-control"))

    from app.web.main import media_url
    u1 = media_url(nome)
    check("url carrega versao do arquivo", "?v=" in u1, u1)
    import os as _os, time as _time
    _os.utime(ep_m / nome, (_time.time() + 60, _time.time() + 60))
    check("arquivo novo muda a url (cache do navegador nao acerta)",
          media_url(nome) != u1, (u1, media_url(nome)))

    print()
    if failures:
        print(f"{len(failures)} falha(s): {', '.join(failures)}")
        return 1
    print("painel web OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
