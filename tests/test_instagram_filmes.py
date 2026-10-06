"""Publicacao no Instagram (scripts/instagram_filmes.py) com a Graph API SIMULADA.

Nenhuma chamada sai para a Meta: requests.get/post sao trocados por um falso.

    .venv\\Scripts\\python.exe -m pytest tests/test_instagram_filmes.py -q
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scripts import instagram_filmes as ig
from scripts import molde_filmes as mf
from scripts import painel_filmes as pf


class Resp:
    def __init__(self, corpo, status=200):
        self.corpo, self.status_code, self.text = corpo, status, str(corpo)

    def json(self):
        return self.corpo


class MetaFalsa:
    """Responde como a Graph API; registra cada chamada."""

    def __init__(self):
        self.chamadas = []
        self.status_container = "FINISHED"
        self.erro_media = None

    def get(self, url, params=None, **_):
        self.chamadas.append(("GET", url, params))
        if url.endswith("/me/accounts"):
            return Resp({"data": [
                {"name": "Pagina A", "access_token": "TOKEN_PAGINA_A",
                 "instagram_business_account": {"id": "17841", "username": "achadinhos._isa"}},
                {"name": "Pagina sem IG", "access_token": "x"}]})
        if url.endswith("/oauth/access_token"):
            return Resp({"access_token": "TOKEN_LONGO"})
        if url.endswith("/content_publishing_limit"):
            return Resp({"data": [{"quota_usage": 3, "config": {"quota_total": 50}}]})
        if url.endswith("/CONT1"):
            return Resp({"status_code": self.status_container})
        if url.endswith("/MEDIA1"):
            return Resp({"permalink": "https://instagram.com/reel/abc"})
        return Resp({"error": {"message": "rota desconhecida " + url}}, 400)

    def post(self, url, data=None, headers=None, **_):
        corpo = data.read() if hasattr(data, "read") else data
        self.chamadas.append(("POST", url, len(corpo) if isinstance(corpo, bytes) else corpo,
                              headers))
        if url.endswith("/media"):
            if self.erro_media:
                return Resp({"error": self.erro_media}, 400)
            return Resp({"id": "CONT1", "uri": "https://rupload.facebook.com/ig-api-upload/CONT1"})
        if "rupload" in url:
            return Resp({"success": True})
        if url.endswith("/media_publish"):
            return Resp({"id": "MEDIA1"})
        return Resp({"error": {"message": "rota desconhecida"}}, 400)


@pytest.fixture
def meta(tmp_path, monkeypatch):
    raiz = tmp_path / "filmes"
    for nome, valor in (("RAIZ", raiz), ("ENTRADA", raiz / "entrada"),
                        ("SAIDA", raiz / "saida"), ("ARQ_MOLDE", raiz / "molde.json")):
        monkeypatch.setattr(mf, nome, valor)
    (raiz / "entrada").mkdir(parents=True)
    (raiz / "saida").mkdir()
    falsa = MetaFalsa()
    monkeypatch.setattr(ig.requests, "get", falsa.get)
    monkeypatch.setattr(ig.requests, "post", falsa.post)
    monkeypatch.setattr(ig.time, "sleep", lambda s: None)
    return falsa


def _pronto(nome="a.mp4"):
    (mf.ENTRADA / nome).write_bytes(b"original")
    mf.saida_de(mf.ENTRADA / nome).write_bytes(b"x" * 1234)


def test_conectar_acha_so_paginas_com_instagram_e_troca_token(meta):
    cfg = ig.conectar("TOKEN_CURTO", "app", "segredo")
    assert [c["username"] for c in cfg["contas"]] == ["achadinhos._isa"]
    assert all("token" not in c for c in cfg["contas"])          # nao vaza pro navegador
    assert ig._conta(None)["token"] == "TOKEN_PAGINA_A"          # guarda o da Pagina
    contas = [c for c in meta.chamadas if c[1].endswith("/me/accounts")]
    assert contas[0][2]["access_token"] == "TOKEN_LONGO"         # usou o token trocado


def test_publicar_sobe_o_arquivo_direto(meta):
    ig.conectar("T")
    _pronto()
    r = ig.publicar(mf.saida_de(mf.ENTRADA / "a.mp4"), "legenda")
    assert r == {"media_id": "MEDIA1", "permalink": "https://instagram.com/reel/abc"}
    upload = next(c for c in meta.chamadas if "rupload" in c[1])
    assert upload[2] == 1234                                     # bytes do arquivo
    assert upload[3]["file_size"] == "1234"
    assert upload[3]["Authorization"] == "OAuth TOKEN_PAGINA_A"
    criar = next(c for c in meta.chamadas if c[1].endswith("/media"))
    assert criar[2]["upload_type"] == "resumable" and "video_url" not in criar[2]


def test_horarios_respeitam_por_dia_intervalo_e_ocupados():
    cfg = {**ig.CONFIG_PADRAO, "por_dia": 3, "inicio": "09:00", "intervalo_min": 120}
    agora = datetime(2026, 10, 6, 10, 0)
    h = ig.horarios(5, cfg, depois_de=agora, ocupados=[datetime(2026, 10, 6, 11, 0)])
    assert [x.strftime("%d %H:%M") for x in h] == \
        ["06 13:00", "07 09:00", "07 11:00", "07 13:00", "08 09:00"]


def test_agendar_nao_duplica_e_gera_legenda(meta):
    ig.conectar("T")
    _pronto("a.mp4")
    _pronto("b.mp4")
    (mf.ENTRADA / "a.txt").write_text("Gancho do A", encoding="utf-8")
    api = TestClient(pf.app, base_url="http://127.0.0.1")
    r = api.post("/api/ig/agendar", json={"videos": ["a.mp4", "b.mp4"]}).json()
    assert r["agendados"] == 2
    assert "Gancho do A" in next(x for x in r["fila"] if x["video"] == "a.mp4")["legenda"]
    r = api.post("/api/ig/agendar", json={"videos": ["a.mp4"]}).json()
    assert r["agendados"] == 0                                   # ja estava na fila
    assert "TOKEN" not in api.get("/api/ig").text                # token nunca sai


def test_fila_publica_um_por_vez(meta):
    ig.conectar("T")
    _pronto("a.mp4")
    _pronto("b.mp4")
    ig.agendar([{"video": "a.mp4", "legenda": "1"}, {"video": "b.mp4", "legenda": "2"}])
    depois = datetime.now() + timedelta(days=30)
    assert ig.processar_vencidos(depois) == 1
    assert sorted(x["status"] for x in ig.fila()) == ["pendente", "publicado"]
    assert ig.processar_vencidos(depois) == 1
    assert all(x["status"] == "publicado" and x["permalink"] for x in ig.fila())


def test_token_invalido_vira_erro_sem_repetir(meta):
    ig.conectar("T")
    _pronto()
    ig.agendar([{"video": "a.mp4", "legenda": "x"}])
    meta.erro_media = {"code": 190, "message": "Error validating access token"}
    ig.processar_vencidos(datetime.now() + timedelta(days=30))
    item = ig.fila()[0]
    assert item["status"] == "erro" and "Token" in item["erro"]


def test_limite_diario_adia_sem_gastar_tentativa(meta):
    ig.conectar("T")
    _pronto()
    ig.agendar([{"video": "a.mp4", "legenda": "x"}])
    meta.erro_media = {"code": 9, "error_subcode": 2207042, "message": "limit reached"}
    agora = datetime.now() + timedelta(days=30)
    ig.processar_vencidos(agora)
    item = ig.fila()[0]
    assert item["status"] == "pendente" and item["tentativas"] == 0
    assert datetime.fromisoformat(item["quando"]) >= agora + timedelta(minutes=59)


def test_erro_temporario_repete_e_depois_desiste(meta):
    ig.conectar("T")
    _pronto()
    ig.agendar([{"video": "a.mp4", "legenda": "x"}])
    meta.erro_media = {"code": 1, "message": "erro qualquer"}
    agora = datetime.now() + timedelta(days=30)
    for i in range(ig.TENTATIVAS):
        ig.processar_vencidos(agora + timedelta(hours=i))
    item = ig.fila()[0]
    assert item["status"] == "erro" and item["tentativas"] == ig.TENTATIVAS


def test_publicando_no_boot_vira_erro_para_nao_duplicar(meta):
    ig.conectar("T")
    _pronto()
    ig.agendar([{"video": "a.mp4", "legenda": "x"}])
    ig._atualizar(ig.fila()[0]["id"], status="publicando")
    ig.recuperar_presos()
    assert ig.fila()[0]["status"] == "erro"


def test_nao_apaga_video_com_post_agendado(meta):
    ig.conectar("T")
    _pronto()
    ig.agendar([{"video": "a.mp4", "legenda": "x"}])
    api = TestClient(pf.app, base_url="http://127.0.0.1")
    r = api.post("/api/limpar", json={"alvo": "originais", "videos": ["a.mp4"]})
    assert r.status_code == 409 and (mf.ENTRADA / "a.mp4").exists()


def _so_paginas_sem_ig(permissoes):
    def get(url, **k):
        if url.endswith("/me/permissions"):
            return Resp({"data": [{"permission": p, "status": "granted"} for p in permissoes]})
        return Resp({"data": [{"name": "Achadinhos da Isa"}]})
    return get


def test_conectar_sem_instagram_aponta_permissao_faltando(meta, monkeypatch):
    monkeypatch.setattr(ig.requests, "get", _so_paginas_sem_ig(["pages_show_list"]))
    with pytest.raises(ig.ErroInstagram, match="Faltam permissoes.*instagram_basic") as e:
        ig.conectar("T")
    assert "Achadinhos da Isa" in str(e.value)


def test_conectar_sem_instagram_com_permissoes_ok(meta, monkeypatch):
    monkeypatch.setattr(ig.requests, "get", _so_paginas_sem_ig(list(ig.PERMISSOES)))
    with pytest.raises(ig.ErroInstagram, match="nao foi marcada"):
        ig.conectar("T")
