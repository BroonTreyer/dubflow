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

    print()
    if failures:
        print(f"{len(failures)} falha(s): {', '.join(failures)}")
        return 1
    print("painel web OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
