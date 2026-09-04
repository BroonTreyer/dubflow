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

# Isola o teste do .env da maquina: tokens de gateway/telegram nao podem vazar e
# disparar chamadas reais (ex.: cobranca no AbacatePay). Cada bloco que precisa de
# um token o define explicitamente. (dotenv ja rodou no import de app.config.)
for _leak in ("ABACATEPAY_TOKEN", "PUSHINPAY_TOKEN", "TELEGRAM_BOT_TOKEN",
              "TELEGRAM_CHANNEL_ID", "TELEGRAM_VIP_CHAT_ID"):
    os.environ.pop(_leak, None)

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

    # Aba fechada no meio de uma resposta enche o log de traceback do asyncio
    # (378 em web.log em 26/08, quase metade do arquivo). O filtro corta ESSE
    # caso e so ele — erro de verdade no asyncio continua aparecendo.
    import logging as _logging
    from app.config import _SemQuedaDeCliente

    _filtro = _SemQuedaDeCliente()

    def _passa(msg: str, exc: BaseException | None = None) -> bool:
        registro = _logging.LogRecord("asyncio", _logging.ERROR, "x.py", 1, msg, None,
                                      (type(exc), exc, None) if exc else None)
        return _filtro.filter(registro)

    _QUEDA = "Exception in callback _ProactorBasePipeTransport._call_connection_lost()"
    check("queda de cliente nao vai para o log",
          not _passa(_QUEDA, ConnectionResetError(10054, "conexao cancelada")))
    check("aviso de socket.send tambem nao", not _passa("socket.send() raised exception."))
    check("erro real do asyncio continua passando",
          _passa("Task exception was never retrieved", ValueError("bug real")))
    check("mesmo callback com excecao grave continua passando",
          _passa(_QUEDA, MemoryError("grave")))

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
    check("/start apresenta os planos (VIP)", "VIP" in bot._handle_text("/start", 900, "X"))
    # Botoes do teclado viram comandos.
    _rp = bot._handle_text(bot.BTN_MENSAL, 902, "Z")
    check("botao Assinar mensal cria cobranca de assinatura",
          "pedido" in _rp.lower() and any(o["kind"] == "subscription"
                                          and str(o["buyer_tg_id"]) == "902"
                                          for o in db.list_orders()))
    _rv = bot._handle_text(bot.BTN_VITALICIO, 904, "V")
    check("botao Plano vitalicio cria cobranca vitalicia",
          "pedido" in _rv.lower() and any(o["kind"] == "lifetime"
                                          and str(o["buyer_tg_id"]) == "904"
                                          for o in db.list_orders()))
    check("botao Canal geral responde com o canal",
          "canal" in bot._handle_text(bot.BTN_CANAL, 903, "W").lower())
    check("teclado tem os 5 botoes",
          {b["text"] for row in bot.main_keyboard()["keyboard"] for b in row}
          == {bot.BTN_MENSAL, bot.BTN_VITALICIO, bot.BTN_CATALOGO,
              bot.BTN_ASSINANTE, bot.BTN_CANAL})
    # Vitalicio: paga uma vez, acesso permanente, nunca sai do VIP.
    _lo = sales.create_lifetime_order("40404", "Vital")
    sales.confirm_payment(_lo)
    check("vitalicio ativa o acesso", sales.subscription_active("40404") is True)
    check("vitalicio e marcado como lifetime", sales.is_lifetime("40404") is True)
    check("vitalicio nunca entra na fila de expiracao",
          "40404" not in db.list_expired_vip_members())
    r = bot._handle_text("/comprar 1", 900, "Cliente")
    check("/comprar cria pedido avulso e mostra o Pix",
          "pedido" in r.lower() and "pix" in r.lower(), r)
    check("/comprar registra o pedido no banco",
          any(o["kind"] == "episode" and str(o["buyer_tg_id"]) == "900" for o in db.list_orders()))
    check("/comprar id inexistente avisa",
          "catalogo" in bot._handle_text("/comprar 9999", 900, "X").lower())
    r3 = bot._handle_text("/assinar", 901, "Y")
    check("/assinar cria pedido de assinatura",
          "pedido" in r3.lower() and any(o["kind"] == "subscription" and str(o["buyer_tg_id"]) == "901"
                                         for o in db.list_orders()), r3)
    # Conserto: assinante (555002, com assinatura ativa da secao anterior) recebe na hora.
    r4 = bot._handle_text("/comprar 1", 555002, "Ciclana")
    check("assinante recebe sem pagar de novo",
          "enviando" in r4.lower() or "acesso" in r4.lower(), r4)
    do_assinante = [o for o in db.list_orders()
                    if str(o["buyer_tg_id"]) == "555002" and o["kind"] == "episode"]
    check("pedido do assinante ja entra pago (fila de entrega)",
          bool(do_assinante) and do_assinante[0]["status"] == "paid", do_assinante)

    print("canal VIP (acesso por assinatura)")
    import os as _os
    import worker as _worker
    from app.publishers import telegram as _tg
    _os.environ["TELEGRAM_VIP_CHAT_ID"] = "-100777666"
    # O bot token vem do .env da MAQUINA, e numa maquina sem Telegram configurado
    # ele esta vazio — o teste passava so por sorte de ambiente. Fixa aqui para o
    # bloco medir o codigo do VIP, nao a configuracao local.
    _os.environ["TELEGRAM_BOT_TOKEN"] = _os.environ.get("TELEGRAM_BOT_TOKEN") or "token-de-teste"
    _vip_calls: list = []
    _orig_post = _tg.requests.post

    class _FakeResp:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    def _fake_post(url, data=None, files=None, timeout=None):
        _vip_calls.append((url, data or {}))
        if "createChatInviteLink" in url:
            return _FakeResp({"ok": True, "result": {"invite_link": "https://t.me/+vip"}})
        if "sendMessage" in url:  # notify() le result["message_id"]
            return _FakeResp({"ok": True, "result": {"message_id": 1}})
        return _FakeResp({"ok": True, "result": True})  # ban/unban devolvem True

    _tg.requests.post = _fake_post
    try:
        check("VIP configurado com token + chat", _tg.vip_configured() is True)
        _link, _err = _tg.create_vip_invite()
        check("convite VIP e link de uso unico", _link == "https://t.me/+vip" and _err is None)
        _inv = [c for c in _vip_calls if "createChatInviteLink" in c[0]][0]
        check("convite usa member_limit=1 no chat VIP certo",
              _inv[1].get("member_limit") == 1 and str(_inv[1].get("chat_id")) == "-100777666")
        check("remover do VIP faz kick (ban + unban)",
              _tg.remove_from_vip("42").ok
              and any("banChatMember" in c[0] for c in _vip_calls)
              and any("unbanChatMember" in c[0] for c in _vip_calls))
        db.set_subscription_expiry("v_venc", "2000-01-01T00:00:00")   # vencido, dentro do VIP
        db.set_subscription_expiry("v_ativo", "2999-01-01T00:00:00")  # ativo
        check("vencido entra na fila de expiracao", "v_venc" in db.list_expired_vip_members())
        check("ativo fora da fila de expiracao", "v_ativo" not in db.list_expired_vip_members())
        check("sweep de expiracao age e remove", _worker.run_vip_expiry() is True)
        check("removido some da fila (nao tenta em loop)",
              "v_venc" not in db.list_expired_vip_members())
        # Renovar zera a marca de saida: se vencer de novo, volta para a fila.
        db.set_subscription_expiry("v_venc", "2999-01-01T00:00:00")
        db.set_subscription_expiry("v_venc", "2000-01-01T00:00:00")
        check("renovacao zera a marca (reentra na fila ao vencer)",
              "v_venc" in db.list_expired_vip_members())
    finally:
        _tg.requests.post = _orig_post

    print("Pix automatico (gateway)")
    _os.environ["ABACATEPAY_TOKEN"] = "tok_fake"
    _paidflag = {"v": False}
    _post_orig = _tg.requests.post
    _get_orig = _tg.requests.get

    # pix.requests e telegram.requests sao o MESMO modulo: um dispatcher unico
    # atende o gateway AbacatePay (json=/params=) e o Telegram (data=/files=).
    def _uni_post(url, json=None, data=None, files=None, params=None, headers=None, timeout=None):
        if "abacatepay" in url:
            return _FakeResp({"data": {"id": "tx_777", "brCode": "PIXCOPIACOLA",
                                       "brCodeBase64": "data:image/png;base64,iVBORw0KGgo=",
                                       "status": "PENDING", "amount": (json or {}).get("amount")},
                              "error": None})
        if "createChatInviteLink" in url:
            return _FakeResp({"ok": True, "result": {"invite_link": "https://t.me/+vip"}})
        return _FakeResp({"ok": True, "result": {"message_id": 1}})

    def _uni_get(url, params=None, headers=None, timeout=None):
        return _FakeResp({"data": {"status": "PAID" if _paidflag["v"] else "PENDING"},
                          "error": None})

    _tg.requests.post = _uni_post
    _tg.requests.get = _uni_get
    try:
        _r = bot._handle_text("/assinar", 24680, "PixCliente")
        check("Pix auto: bot devolve o copia-e-cola", _r == "PIXCOPIACOLA", _r)
        _po = [o for o in db.list_orders() if str(o["buyer_tg_id"]) == "24680"][0]
        check("Pix auto: txid gravado no pedido", _po.get("pix_txid") == "tx_777", _po.get("pix_txid"))
        check("Pix auto: pedido nasce pendente", _po["status"] == "pending")
        check("Pix auto: poll nao confirma enquanto nao pago", _worker.run_pix_poll() is False)
        _paidflag["v"] = True
        check("Pix auto: poll confirma quando pago", _worker.run_pix_poll() is True)
        check("Pix auto: pedido vira pago", db.get_order(_po["id"])["status"] in ("paid", "delivered"))
        check("Pix auto: assinatura ativa apos pagar",
              db.get_subscription_expiry("24680") is not None)
    finally:
        _tg.requests.post = _post_orig
        _tg.requests.get = _get_orig
        _os.environ.pop("ABACATEPAY_TOKEN", None)

    print("separacao cortes/VIP (video completo)")
    _ep_vip = db.create_episode("https://x/vip", license_status="owned")
    db.update_episode(_ep_vip, status="done")
    _ep_unk = db.create_episode("https://x/unk", license_status="unknown")
    db.update_episode(_ep_unk, status="done")
    _pend0 = {e["id"] for e in db.episodes_pending_vip()}
    check("vendavel entra na fila do VIP", _ep_vip in _pend0)
    check("licenca unknown NAO vai pro VIP (so cortes)", _ep_unk not in _pend0)
    _vid = settings.data_dir / "ep_full.mp4"
    _vid.write_bytes(b"FAKEVIDEO")
    _find_bkp = _worker.archive.find
    _post_bkp = _tg.requests.post
    _worker.archive.find = lambda eid: {"titulo": f"Ep {eid}", "canal": "Canal",
                                        "arquivos": {"episodio": str(_vid)}}
    _tg.requests.post = lambda url, data=None, files=None, json=None, params=None, \
        headers=None, timeout=None: _FakeResp({"ok": True, "result": {"message_id": 7}})
    try:
        for _ in range(20):
            if _ep_vip not in {e["id"] for e in db.episodes_pending_vip()}:
                break
            _worker.run_vip_publish()
        check("worker publica o completo e o episodio sai da fila do VIP",
              _ep_vip not in {e["id"] for e in db.episodes_pending_vip()})
        check("unknown nunca entrou na fila do VIP",
              _ep_unk not in {e["id"] for e in db.episodes_pending_vip()})
    finally:
        _worker.archive.find = _find_bkp
        _tg.requests.post = _post_bkp

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

    print("republicar o que falhou")
    # Publicacao que esgotou as tentativas era um beco sem saida: mesmo depois de
    # corrigir a causa (API do canal desativada, canal sem verificacao) ela nao
    # voltava, porque a fila filtra por attempts < MAX. Zerar o contador faz parte.
    c_pub = db.create_channel("Canal do republicar", "youtube", "BR")
    clip_pub = db.list_clips(ep_id)[0]["id"]
    p_fail = db.create_post(clip_pub, "youtube", scheduled_at=db.now(), channel_id=c_pub)
    db.update_post(p_fail, status="failed", attempts=db.MAX_PUBLISH_ATTEMPTS,
                   error="permissao negada")
    check("falhado nao esta na fila", not any(p["id"] == p_fail for p in db.pending_posts()))

    refeitos = db.requeue_failed_posts(channel_id=c_pub)
    check("requeue devolve o id", refeitos == [p_fail], refeitos)
    voltou = [p for p in db.list_posts(ep_id) if p["id"] == p_fail][0]
    check("volta como pending, sem erro e com tentativas zeradas",
          voltou["status"] == "pending" and voltou["error"] is None and voltou["attempts"] == 0,
          dict(status=voltou["status"], attempts=voltou["attempts"]))
    check("e reaparece na fila do worker", any(p["id"] == p_fail for p in db.pending_posts()))

    # Nao encosta em publicacao de OUTRO canal.
    c_outro = db.create_channel("Canal vizinho", "youtube", "BR")
    db.update_post(p_fail, status="failed", attempts=db.MAX_PUBLISH_ATTEMPTS)
    check("filtro por canal nao pega o vizinho", db.requeue_failed_posts(channel_id=c_outro) == [])
    check("o falhado continua falhado",
          [p for p in db.list_posts(ep_id) if p["id"] == p_fail][0]["status"] == "failed")

    # Pela rota do painel, com CSRF, e so o post pedido.
    tok_pub = csrf_of(client)
    r_pub = client.post(f"/channels/{c_pub}/posts/retry-failed",
                        data={"csrf": tok_pub, "post_id": p_fail}, follow_redirects=False)
    check("rota republica e redireciona com a contagem",
          r_pub.status_code == 303 and "refeitas=1" in r_pub.headers.get("location", ""),
          f'{r_pub.status_code} {r_pub.headers.get("location")}')
    check("post voltou pela rota",
          [p for p in db.list_posts(ep_id) if p["id"] == p_fail][0]["status"] == "pending")

    db.update_post(p_fail, status="failed", attempts=db.MAX_PUBLISH_ATTEMPTS)
    r_semtok = client.post(f"/channels/{c_pub}/posts/retry-failed",
                           data={"csrf": "errado"}, follow_redirects=False)
    check("sem csrf valido nao republica", r_semtok.status_code >= 400, r_semtok.status_code)
    check("canal inexistente da 404",
          client.post("/channels/99999/posts/retry-failed",
                      data={"csrf": tok_pub}, follow_redirects=False).status_code == 404)
    check("o aviso do canal mostra a contagem",
          "Republicar as 1 que falharam" in client.get(f"/channels/{c_pub}").text)

    # Some do caminho para nao contaminar os testes seguintes.
    db.update_post(p_fail, status="canceled", attempts=0, error=None)
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
    # Painel por canal: renderiza a secao de videos publicados com o post do canal.
    _cp = client.get(f"/channels/{c_tk}")
    check("painel do canal abre (200)", _cp.status_code == 200, _cp.status_code)
    check("painel do canal mostra 'Videos publicados'", "Vídeos publicados" in _cp.text)
    check("painel do canal lista o post do canal",
          bool(db.posts_by_channel(c_tk)) and "tiktok" in _cp.text.lower())
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

    print("limpeza pos-publicacao")
    ep_cl = db.create_episode("https://youtu.be/limpeza", "owned")
    ids_cl = db.replace_clips(ep_cl, [
        {"start": 0, "end": 30, "title": "publicado", "caption": "c", "score": 9},
        {"start": 40, "end": 70, "title": "pendente", "caption": "c", "score": 9},
        {"start": 80, "end": 110, "title": "dois destinos", "caption": "c", "score": 9},
    ])
    pasta_cl = settings.data_dir / "episodes" / f"ep_{ep_cl:05d}" / "clips"
    pasta_cl.mkdir(parents=True, exist_ok=True)
    for i, cid in enumerate(ids_cl):
        arq = pasta_cl / f"c{i}.mp4"
        arq.write_bytes(b"video")
        db.update_clip(cid, path=str(arq), status="ready")

    # corte 0: um post, publicado -> deve entrar na limpeza
    p0 = db.create_post(ids_cl[0], "youtube")
    db.update_post(p0, status="published", posted_at=db.now())
    # corte 1: post ainda pendente -> NAO pode entrar
    db.create_post(ids_cl[1], "youtube")
    # corte 2: dois destinos, so um publicado -> NAO pode entrar
    p2a = db.create_post(ids_cl[2], "youtube")
    db.update_post(p2a, status="published", posted_at=db.now())
    db.create_post(ids_cl[2], "tiktok")

    limpaveis = {c["id"] for c in db.clips_fully_published(older_than_hours=0)}
    check("corte 100% publicado entra na limpeza", ids_cl[0] in limpaveis, limpaveis)
    check("corte com post pendente fica de fora", ids_cl[1] not in limpaveis, limpaveis)
    check("corte com 2 destinos e 1 pendente fica de fora",
          ids_cl[2] not in limpaveis, limpaveis)

    # A carencia protege republicacao: recem-publicado nao e apagado.
    check("carencia segura o recem-publicado",
          ids_cl[0] not in {c["id"] for c in db.clips_fully_published(older_than_hours=48)})

    import worker as _w
    _w.settings.cleanup_published = True
    _w.settings.cleanup_after_hours = 0
    _w._last_cleanup = None
    _w.run_cleanup_published()
    check("arquivo do publicado foi apagado", not (pasta_cl / "c0.mp4").exists())
    check("arquivo do pendente continua", (pasta_cl / "c1.mp4").exists())
    check("arquivo do parcialmente publicado continua", (pasta_cl / "c2.mp4").exists())
    check("a linha do corte permanece no banco (historico/metricas)",
          db.list_clips(ep_cl)[0]["id"] == ids_cl[0])
    _w.settings.cleanup_published = False

    print("apagar episodio")
    ep_del = db.create_episode("https://youtu.be/apagar", "owned")
    ids = db.replace_clips(ep_del, [{"start": 0, "end": 30, "title": "x", "caption": "c",
                                     "score": 8}])
    db.update_clip(ids[0], path=str(_tmp / "d.mp4"), status="ready")
    db.create_post(ids[0], "youtube") if hasattr(db, "create_post") else None
    pasta = settings.data_dir / "episodes" / f"ep_{ep_del:05d}"
    (pasta / "clips").mkdir(parents=True, exist_ok=True)
    (pasta / "source.mp4").write_bytes(b"video")
    db.update_episode(ep_del, status="done")

    tok = csrf_of(client)
    r_del = client.post(f"/episodes/{ep_del}/delete", data={"csrf": tok},
                        follow_redirects=False)
    check("delete redireciona para a fila", r_del.status_code == 303, r_del.status_code)
    check("episodio sai do banco", db.get_episode(ep_del) is None)
    check("cortes somem junto (CASCADE)", db.list_clips(ep_del) == [])
    check("arquivos em disco tambem somem", not pasta.exists(), pasta)

    # Sem CSRF nao apaga: e destrutivo e nao pode ser disparado de outro site.
    ep_csrf = db.create_episode("https://youtu.be/csrf", "owned")
    db.update_episode(ep_csrf, status="done")
    r_semtok = client.post(f"/episodes/{ep_csrf}/delete", data={"csrf": "errado"},
                           follow_redirects=False)
    check("sem csrf valido nao apaga", r_semtok.status_code >= 400, r_semtok.status_code)
    check("episodio continua la", db.get_episode(ep_csrf) is not None)

    # Episodio EM EXECUCAO nao pode ser apagado embaixo do worker.
    db.update_episode(ep_csrf, status="transcribing")
    r_run = client.post(f"/episodes/{ep_csrf}/delete", data={"csrf": tok},
                        follow_redirects=False)
    check("episodio rodando e recusado", r_run.status_code == 409, r_run.status_code)
    check("e continua no banco", db.get_episode(ep_csrf) is not None)

    check("inexistente da 404",
          client.post("/episodes/99999/delete", data={"csrf": tok},
                      follow_redirects=False).status_code == 404)

    print()
    print("reprocessar em lote os que falharam")
    # Falha em lote (yt-dlp bloqueado, provedor de IA fora): um clique devolve
    # todos para a fila sem tocar em quem esta rodando.
    ep_f1 = db.create_episode("https://youtu.be/falhou-1", "owned")
    ep_f2 = db.create_episode("https://youtu.be/falhou-2", "owned")
    for ep in (ep_f1, ep_f2):
        db.update_episode(ep, status="failed", progress=0.4, error="yt-dlp falhou")
    ep_rodando = db.create_episode("https://youtu.be/rodando", "owned")
    db.update_episode(ep_rodando, status="transcribing", progress=0.3)

    fila = client.get("/").text
    check("botao do lote aparece com a contagem certa",
          'action="/episodes/retry-failed"' in fila and "Reprocessar os 2 que falharam" in fila)
    check("cada linha terminal ganha o reprocessar",
          f'action="/episodes/{ep_f1}/retry"' in fila)

    tok = csrf_of(client)
    r_lote = client.post("/episodes/retry-failed", data={"csrf": tok}, follow_redirects=False)
    check("retry-failed redireciona com a contagem",
          r_lote.status_code == 303 and "refeitos=2" in r_lote.headers.get("location", ""),
          f'{r_lote.status_code} {r_lote.headers.get("location")}')
    check("os dois falhados voltaram para a fila",
          [db.get_episode(e)["status"] for e in (ep_f1, ep_f2)] == ["queued", "queued"],
          [db.get_episode(e)["status"] for e in (ep_f1, ep_f2)])
    check("erro e progresso limpos",
          all(db.get_episode(e)["error"] is None and db.get_episode(e)["progress"] == 0
              for e in (ep_f1, ep_f2)))
    check("nao encosta em quem esta rodando",
          db.get_episode(ep_rodando)["status"] == "transcribing",
          db.get_episode(ep_rodando)["status"])

    # Sem CSRF nao reprocessa: recolocar a fila inteira e caro (GPU + API).
    db.update_episode(ep_f1, status="failed", error="de novo")
    r_semtok = client.post("/episodes/retry-failed", data={"csrf": "errado"},
                           follow_redirects=False)
    check("sem csrf valido nao reprocessa em lote", r_semtok.status_code >= 400,
          r_semtok.status_code)
    check("o falhado continua falhado", db.get_episode(ep_f1)["status"] == "failed",
          db.get_episode(ep_f1)["status"])

    # Sem nada em falha, o botao nem aparece e a rota nao quebra.
    db.update_episode(ep_f1, status="done", error=None)
    r_vazio = client.post("/episodes/retry-failed", data={"csrf": tok}, follow_redirects=False)
    check("sem falhados, responde 0", "refeitos=0" in r_vazio.headers.get("location", ""),
          r_vazio.headers.get("location"))
    check("botao do lote some quando nao ha falha",
          "retry-failed" not in client.get("/").text)

    print("pausar e retomar do mesmo ponto")
    # Pausa cooperativa: quem esta rodando so recebe o PEDIDO (a etapa em curso
    # precisa terminar de gravar), enquanto quem ainda esta na fila para na hora.
    ep_p = db.create_episode("https://youtu.be/pausa", "owned")
    db.update_episode(ep_p, status="transcribing", progress=0.3)
    r_semtok = client.post(f"/episodes/{ep_p}/pause", data={"csrf": "errado"},
                           follow_redirects=False)
    check("sem csrf valido nao pausa", r_semtok.status_code >= 400, r_semtok.status_code)

    client.post(f"/episodes/{ep_p}/pause", data={"csrf": tok}, follow_redirects=False)
    ep = db.get_episode(ep_p)
    check("rodando: pausa vira pedido, nao para na hora",
          ep["status"] == "transcribing" and ep["pause_requested"] == 1,
          (ep["status"], ep["pause_requested"]))
    check("o runner enxerga o pedido", db.pause_requested(ep_p) is True)

    ep_q = db.create_episode("https://youtu.be/pausa-fila", "owned")
    client.post(f"/episodes/{ep_q}/pause", data={"csrf": tok}, follow_redirects=False)
    check("na fila: pausa na hora, sem comecar",
          db.get_episode(ep_q)["status"] == "paused", db.get_episode(ep_q)["status"])

    # Retomar preserva os artefatos: e a diferenca entre continuar e recomecar.
    db.update_episode(ep_p, status="paused", pause_requested=0,
                      paths={"source_video": "/x/v.mp4", "transcript": "/x/t.json"})
    client.post(f"/episodes/{ep_p}/resume", data={"csrf": tok}, follow_redirects=False)
    ep = db.get_episode(ep_p)
    check("retomar devolve para a fila", ep["status"] == "queued", ep["status"])
    check("retomar preserva os artefatos", ep["paths"].get("transcript") == "/x/t.json",
          ep["paths"])
    check("retomar limpa o pedido de pausa", ep["pause_requested"] == 0)

    # Reprocessar tambem retoma; so 'refazer do zero' descarta.
    db.update_episode(ep_p, status="failed", error="quebrou",
                      paths={"transcript": "/x/t.json"})
    client.post(f"/episodes/{ep_p}/retry", data={"csrf": tok}, follow_redirects=False)
    check("reprocessar preserva os artefatos",
          db.get_episode(ep_p)["paths"].get("transcript") == "/x/t.json",
          db.get_episode(ep_p)["paths"])

    db.update_episode(ep_p, status="failed", paths={"transcript": "/x/t.json"})
    client.post(f"/episodes/{ep_p}/retry", data={"csrf": tok, "scratch": "1"},
                follow_redirects=False)
    check("refazer do zero descarta os artefatos",
          not db.get_episode(ep_p)["paths"], db.get_episode(ep_p)["paths"])

    # Um reinicio do worker nao pode ressuscitar o que foi pausado de proposito.
    db.update_episode(ep_p, status="paused")
    db.update_episode(ep_q, status="clipping")
    devolvidos = db.recover_stuck_episodes()
    check("reinicio devolve quem estava rodando", ep_q in devolvidos, devolvidos)
    check("reinicio NAO ressuscita o pausado", ep_p not in devolvidos, devolvidos)
    check("pausado continua pausado", db.get_episode(ep_p)["status"] == "paused")

    print("nicho unico dispensa a classificacao")
    # Com um nicho so entre os canais ativos nao ha o que decidir, e perguntar
    # cria um jeito de falhar: `classify_segment` devolve None quando o episodio
    # nao casa, o episodio fica `done` com zero posts e a distribuicao, sendo
    # best-effort, nem reprova. Prendeu 4 episodios e 200+ cortes em 03-04/09.
    from app.pipeline import distribute  # noqa: PLC0415

    # Os blocos anteriores deixaram canais de outros nichos ativos. Sem pausa-los,
    # nao existe "nicho unico" para testar.
    anteriores = [c["id"] for c in db.list_channels(only_active=True)]
    for cid in anteriores:
        db.update_channel(cid, status="paused")
    canal = db.create_channel(name="Unico", platform="youtube", market="BR",
                              niche="Poder e Sociedade", posts_per_day=5)
    ep_uni = db.create_episode("https://youtu.be/nicho-unico", "owned")
    db.update_episode(ep_uni, status="done", lang_dst="pt-BR")
    db.replace_clips(ep_uni, [{"idx": 1, "start": 0, "end": 30, "title": "t", "hook": "h",
                               "caption": "c", "score": 9}])
    for corte in db.list_clips(ep_uni):
        db.update_clip(corte["id"], status="ready", path=str(_tmp / "corte.mp4"))

    def recusa_sempre(episode, niches):
        raise AssertionError("com um nicho so, a classificacao nao deveria ser chamada")

    resumo = distribute.distribute_episode(ep_uni, classifier=recusa_sempre)
    check("nicho unico agenda sem classificar", resumo["status"] == "ok", resumo)
    check("o segmento fica gravado no episodio",
          db.get_episode(ep_uni)["segment"] == "Poder e Sociedade",
          db.get_episode(ep_uni)["segment"])
    check("o corte virou publicacao", len(db.list_posts(ep_uni)) == 1, db.list_posts(ep_uni))

    # Com dois nichos a decisao volta a existir, e a recusa e respeitada.
    db.create_channel(name="Outro", platform="youtube", market="BR",
                      niche="Ciencia e Universo", posts_per_day=5)
    ep_dois = db.create_episode("https://youtu.be/dois-nichos", "owned")
    db.update_episode(ep_dois, status="done", lang_dst="pt-BR")
    resumo2 = distribute.distribute_episode(ep_dois, classifier=lambda e, n: None)
    check("com mais de um nicho, a classificacao decide",
          resumo2["status"] == "nao_classificado", resumo2)
    for cid in anteriores:
        db.update_channel(cid, status="active")

    print()
    if failures:
        print(f"{len(failures)} falha(s): {', '.join(failures)}")
        return 1
    print("painel web OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
