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
