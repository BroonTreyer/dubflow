"""Publicacao no Instagram (scripts/instagram_filmes.py) com a Graph API SIMULADA.

Nenhuma chamada sai para a Meta: requests.get/post sao trocados por um falso.

    .venv\\Scripts\\python.exe -m pytest tests/test_instagram_filmes.py -q
"""

from __future__ import annotations

import contextlib
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
        self.falhas_processamento = 0   # quantos uploads devolvem ProcessingFailedError
        self.erro_comentario = None
        self.erro_insights = None
        self.expira = 0                 # expires_at do debug_token
        self.links = []                 # arquivos entregues pelo link temporario (simulado)

    @contextlib.contextmanager
    def compartilhar(self, arquivo):
        self.links.append(arquivo)
        yield "https://teste.trycloudflare.com/v/TOKEN.mp4"

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
        if url.endswith("/debug_token"):
            return Resp({"data": {"expires_at": self.expira, "data_access_expires_at": 0}})
        if url.endswith("/MEDIA1/insights"):
            if self.erro_insights:
                return Resp({"error": self.erro_insights}, 400)
            return Resp({"data": [{"name": "views", "values": [{"value": 1500}]},
                                  {"name": "reach", "values": [{"value": 900}]},
                                  {"name": "likes", "total_value": {"value": 80}}]})
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
            if self.falhas_processamento:
                self.falhas_processamento -= 1
                return Resp({"debug_info": {"retriable": False, "type": "ProcessingFailedError",
                                            "message": "Request processing failed"}}, 500)
            return Resp({"success": True})
        if url.endswith("/media_publish"):
            return Resp({"id": "MEDIA1"})
        if url.endswith("/MEDIA1/comments"):
            if self.erro_comentario:
                return Resp({"error": self.erro_comentario}, 400)
            return Resp({"id": "COMENT1"})
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
    monkeypatch.setattr(ig.link_publico, "compartilhar", falsa.compartilhar)
    monkeypatch.setattr(ig.link_publico, "disponivel", lambda: True)
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


# ------------------------------------------------------------------ 10/2026

def _pronto_em_partes(nome, n):
    (mf.ENTRADA / nome).write_bytes(b"original")
    for k in range(1, n + 1):
        mf.saida_parte(mf.ENTRADA / nome, k, n).write_bytes(b"x" * 1234)


def test_app_fica_guardado_e_nunca_vai_pro_navegador(meta):
    ig.conectar("T1", "app", "segredo")
    publica = ig.config_publica()
    assert publica["app_salvo"] is True and "app_secret" not in publica
    meta.chamadas.clear()
    ig.conectar("T2")                                           # sem preencher de novo
    troca = next(c for c in meta.chamadas if c[1].endswith("/oauth/access_token"))
    assert troca[2]["client_secret"] == "segredo" and troca[2]["fb_exchange_token"] == "T2"
    api = TestClient(pf.app, base_url="http://127.0.0.1")
    assert "segredo" not in api.get("/api/ig").text


def test_conectar_avisa_quando_o_token_vai_vencer(meta):
    meta.expira = int(datetime.now().timestamp()) + 3600        # token curto, sem app
    cfg = ig.conectar("T")
    assert "vence em poucas horas" in cfg["aviso"]
    meta.expira = 0
    assert "aviso" not in ig.conectar("T", "app", "segredo")


def test_meta_nao_processou_sem_link_explica_a_duracao(meta, monkeypatch):
    monkeypatch.setattr(ig.link_publico, "disponivel", lambda: False)
    ig.conectar("T")
    _pronto()
    meta.falhas_processamento = 1
    with pytest.raises(ig.ErroInstagram, match="duracao conta") as e:
        ig.publicar(mf.saida_de(mf.ENTRADA / "a.mp4"), "legenda")
    assert e.value.fatal
    assert len([c for c in meta.chamadas if "rupload" in c[1]]) == 1


def test_meta_nao_processou_o_direto_vai_por_link(meta):
    ig.conectar("T")
    _pronto()
    meta.falhas_processamento = 1
    r = ig.publicar(mf.saida_de(mf.ENTRADA / "a.mp4"), "legenda")
    assert r["media_id"] == "MEDIA1" and len(meta.links) == 1


def test_video_longo_vai_por_link_sem_rupload(meta, monkeypatch):
    ig.conectar("T")
    _pronto()
    monkeypatch.setattr(mf, "duracao", lambda v: 155.0)
    r = ig.publicar(mf.saida_de(mf.ENTRADA / "a.mp4"), "legenda")
    assert r["media_id"] == "MEDIA1"
    criar = next(c for c in meta.chamadas if c[1].endswith("/media"))
    assert criar[2]["video_url"].startswith("https://") and "upload_type" not in criar[2]
    assert not any("rupload" in c[1] for c in meta.chamadas)


def test_link_que_falha_vira_erro_que_repete(meta, monkeypatch):
    ig.conectar("T")
    _pronto()
    monkeypatch.setattr(mf, "duracao", lambda v: 155.0)

    @contextlib.contextmanager
    def quebra(arquivo):
        raise ig.link_publico.ErroLink("o tunel nao respondeu")
        yield
    monkeypatch.setattr(ig.link_publico, "compartilhar", quebra)
    with pytest.raises(ig.ErroInstagram, match="tunel") as e:
        ig.publicar(mf.saida_de(mf.ENTRADA / "a.mp4"), "x")
    assert not e.value.fatal                                     # tenta de novo em 10 min


def test_video_acima_de_15_min_e_recusado_antes_do_upload(meta, monkeypatch):
    ig.conectar("T")
    _pronto()
    monkeypatch.setattr(mf, "duracao", lambda v: 901.0)
    with pytest.raises(ig.ErroInstagram, match="15 min"):
        ig.publicar(mf.saida_de(mf.ENTRADA / "a.mp4"), "x")
    assert not any("rupload" in c[1] for c in meta.chamadas)


def test_primeiro_comentario_e_falha_dele_nao_derruba_o_post(meta):
    ig.conectar("T")
    _pronto()
    video = mf.saida_de(mf.ENTRADA / "a.mp4")
    ig.publicar(video, "x", comentario="Qual o filme?")
    c = next(c for c in meta.chamadas if c[1].endswith("/comments"))
    assert c[2]["message"] == "Qual o filme?"
    meta.erro_comentario = {"code": 10, "message": "permission"}
    r = ig.publicar(video, "x", comentario="Qual o filme?")
    assert r["media_id"] == "MEDIA1" and "instagram_manage_comments" in r["comentario_erro"]


def test_fila_guarda_media_id_e_comentario_da_config(meta):
    ig.conectar("T")
    ig.salvar_config({"comentario": "Comenta"})
    _pronto()
    ig.agendar([{"video": "a.mp4", "legenda": "x"}])
    ig.processar_vencidos(datetime.now() + timedelta(days=30))
    item = ig.fila()[0]
    assert item["status"] == "publicado" and item["media_id"] == "MEDIA1"
    assert any(c[1].endswith("/comments") for c in meta.chamadas)


def test_parte_seguinte_fica_bloqueada_e_volta_junto(meta):
    ig.conectar("T")
    _pronto_em_partes("a.mp4", 3)
    ig.agendar([{"video": "a.mp4", "parte": k, "partes": 3, "legenda": str(k)}
                for k in range(1, 4)])
    p1, p2, p3 = sorted(ig.fila(), key=lambda x: x["parte"])
    ig._atualizar(p1["id"], status="erro", erro="Token expirado ou invalido")
    assert ig.bloqueados() == {p2["id"], p3["id"]}
    fila = ig.repetir([p1["id"]])
    por_parte = {x["parte"]: x for x in fila}
    assert all(x["status"] == "pendente" and x["tentativas"] == 0 for x in fila)
    assert por_parte[1]["quando"] < por_parte[2]["quando"] < por_parte[3]["quando"]
    assert ig.bloqueados() == set()


def test_repetir_pela_api_todos_com_erro(meta):
    ig.conectar("T")
    _pronto("a.mp4")
    _pronto("b.mp4")
    ig.agendar([{"video": v, "legenda": "x"} for v in ("a.mp4", "b.mp4")])
    for x in ig.fila():
        ig._atualizar(x["id"], status="erro", erro="falhou")
    api = TestClient(pf.app, base_url="http://127.0.0.1")
    r = api.post("/api/ig/repetir", json={"todos": True}).json()
    assert r["repetidos"] == 2 and all(x["status"] == "pendente" for x in r["fila"])
    assert len({x["quando"] for x in r["fila"]}) == 2           # horarios diferentes


def _duas_contas(meta, monkeypatch):
    def get(url, params=None, **k):
        if url.endswith("/me/accounts"):
            return Resp({"data": [
                {"name": "A", "access_token": "TA",
                 "instagram_business_account": {"id": "1", "username": "conta_a"}},
                {"name": "B", "access_token": "TB",
                 "instagram_business_account": {"id": "2", "username": "conta_b"}}]})
        return MetaFalsa.get(meta, url, params)
    monkeypatch.setattr(ig.requests, "get", get)
    ig.conectar("T")


def test_distribuir_reparte_videos_e_mantem_partes_juntas(meta, monkeypatch):
    _duas_contas(meta, monkeypatch)
    itens = [{"video": "a.mp4", "parte": 1, "partes": 2, "legenda": "x"},
             {"video": "a.mp4", "parte": 2, "partes": 2, "legenda": "x"},
             {"video": "b.mp4", "legenda": "x"}, {"video": "c.mp4", "legenda": "x"}]
    fila = ig.agendar(itens, distribuir=True)
    conta_de = {}
    for x in fila:
        conta_de.setdefault(x["video"], set()).add(x["username"])
    assert len(conta_de["a.mp4"]) == 1                          # partes na mesma conta
    assert {x["username"] for x in fila} == {"conta_a", "conta_b"}
    assert len(ig.agendar(itens, distribuir=True)) == 4         # nao agenda de novo


def test_metricas_e_media_por_gancho(meta):
    ig.conectar("T")
    _pronto()
    ig.agendar([{"video": "a.mp4", "legenda": "x", "gancho": "Ninguem esperava"}])
    ig.processar_vencidos(datetime.now() + timedelta(days=30))
    assert ig.atualizar_metricas() == {"atualizados": 1, "erro": None}
    assert ig.atualizar_metricas()["atualizados"] == 0          # ja atualizado ha pouco
    res = ig.resultados()
    assert res["posts"][0]["metricas"] == {"views": 1500, "reach": 900, "likes": 80}
    assert res["ganchos"] == [{"gancho": "Ninguem esperava", "posts": 1, "media_views": 1500}]


def test_metricas_sem_permissao_explica(meta):
    ig.conectar("T")
    _pronto()
    ig.agendar([{"video": "a.mp4", "legenda": "x"}])
    ig.processar_vencidos(datetime.now() + timedelta(days=30))
    meta.erro_insights = {"code": 10, "message": "permission"}
    assert "instagram_manage_insights" in ig.atualizar_metricas(forcar=True)["erro"]


def test_avisos_de_token_e_notificacao_sem_repetir(meta, monkeypatch):
    agora = datetime.now()
    meta.expira = int((agora + timedelta(days=1)).timestamp())
    ig.conectar("T", "app", "segredo")
    assert any("vence em" in a["texto"] for a in ig.avisos(agora))
    mandados = []
    from scripts import notificar
    monkeypatch.setattr(notificar, "windows", lambda t, x: mandados.append(x) or True)
    assert ig.notificar_avisos(agora) == 1
    assert ig.notificar_avisos(agora + timedelta(hours=1)) == 0  # nao repete toda hora
    assert ig.notificar_avisos(agora + timedelta(hours=13)) == 1
    vencido = [a for a in ig.avisos(agora + timedelta(days=2)) if a["nivel"] == "erro"]
    assert vencido and "venceu" in vencido[0]["texto"]


def test_erro_de_token_de_antes_da_reconexao_nao_vira_aviso(meta):
    ig.conectar("T")
    _pronto()
    ig.agendar([{"video": "a.mp4", "legenda": "x"}])
    meta.erro_media = {"code": 190, "message": "Error validating access token"}
    ig.processar_vencidos(datetime.now() + timedelta(days=30))
    assert any(a["chave"].startswith("invalido:") for a in ig.avisos())
    ig._atualizar(ig.fila()[0]["id"], erro_em="2026-01-01T00:00:00")   # erro antigo
    ig.conectar("T")
    assert not any(a["chave"].startswith("invalido:") for a in ig.avisos())


def test_horarios_da_madrugada_da_janela_de_ontem():
    cfg = {**ig.CONFIG_PADRAO, "por_dia": 5, "inicio": "22:35", "intervalo_min": 180}
    h = ig.horarios(2, cfg, depois_de=datetime(2026, 10, 7, 2, 0))
    assert [x.strftime("%d %H:%M") for x in h] == ["07 04:35", "07 07:35"]


def test_agendar_com_legenda_da_ia(meta, monkeypatch):
    ig.conectar("T")
    ig.salvar_config({"legenda_ia": True})
    _pronto("a.mp4")
    _pronto("b.mp4")
    monkeypatch.setattr(pf.lia, "gerar",
                        lambda pedidos, fixas: ["Texto da IA" if p["video"].name == "a.mp4"
                                                else None for p in pedidos])
    api = TestClient(pf.app, base_url="http://127.0.0.1")
    r = api.post("/api/ig/agendar", json={"videos": ["a.mp4", "b.mp4"]}).json()
    leg = {x["video"]: x["legenda"] for x in r["fila"]}
    assert leg["a.mp4"] == "Texto da IA" and "#filmes" in leg["b.mp4"]   # b cai no modelo
    assert r["legendas_sem_ia"] == 1
