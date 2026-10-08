"""Divisao em partes (limite de 90s do Reels por API): render, legenda e fila.

    .venv\\Scripts\\python.exe -m pytest tests/test_partes_filmes.py -q
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scripts import instagram_filmes as ig
from scripts import molde_filmes as mf
from scripts import painel_filmes as pf
from tests.test_filmes import _video, pastas  # noqa: F401  (fixture)
from tests.test_instagram_filmes import MetaFalsa


def test_partes_iguais_e_limite():
    c = mf.normalizar_corte({})
    assert mf.partes(80, c, 89) == [(0.0, 0.0)]                     # cabe: corte intacto
    assert mf.partes(150, c, 89) == [(0.0, 75.0), (75.0, 150.0)]    # 2 x 75, nao 89 + 61
    p = mf.partes(154.62, mf.normalizar_corte({"inicio": 4.62}), 89)
    assert len(p) == 2 and p[0][0] == 4.62 and p[-1][1] == 154.62
    assert all(b - a <= 89 for a, b in mf.partes(1000, c, 89))
    assert mf.partes(200, mf.normalizar_corte({"inicio": 10, "fim": 60}), 89) == [(10.0, 60.0)]


def test_fatiar_legenda_zera_o_tempo_na_parte():
    falas = [{"id": 0, "start": 10, "end": 12, "text": "a"},
             {"id": 1, "start": 49, "end": 52, "text": "b"},
             {"id": 2, "start": 70, "end": 71, "text": "c"}]
    parte2 = mf._fatiar(falas, 50, 50)
    assert [(f["text"], f["start"], f["end"]) for f in parte2] == [("b", 0.0, 2), ("c", 20, 21)]


def test_render_divide_em_partes_com_selo_e_cta_final(pastas):  # noqa: F811
    v = _video(mf.ENTRADA / "longo.mp4", 640, 360, segundos=100)
    molde = mf._validar({"parte_max": 40})
    vistos = []
    original = mf.renderizar

    def espia(*a, **k):
        vistos.append((a[2]["cta"], k.get("parte")))  # (video, saida, molde, ...)
        return original(*a, **k)

    mf.renderizar, antes = espia, mf.renderizar
    try:
        saidas = mf.renderizar_video(v, molde, "gancho")
    finally:
        mf.renderizar = antes
    assert [s.name for s in saidas] == [f"longo_molde_p{k}.mp4" for k in (1, 2, 3)]
    assert mf.saidas_de(v) == saidas and mf.pronto(v)
    duracoes = [mf.duracao(s) for s in saidas]
    assert all(d <= 40.5 for d in duracoes) and abs(sum(duracoes) - 100) < 1.5
    assert vistos == [(molde["cta"], "PARTE 1"), (molde["cta"], "PARTE 2"),
                      (molde["cta_ultima"], "PARTE 3")]
    # renderizar de novo com limite maior troca 3 partes por 1 arquivo, sem sobra
    mf.renderizar_video(v, mf._validar({"parte_max": 90}), "gancho", {"fim": 30})
    assert [s.name for s in mf.saidas_de(v)] == ["longo_molde.mp4"]
    assert not list(mf.SAIDA.glob("longo_molde_p*"))


def test_falha_numa_parte_nao_deixa_metade(pastas, monkeypatch):  # noqa: F811
    v = _video(mf.ENTRADA / "longo.mp4", 320, 240, segundos=30)
    original, chamadas = mf.renderizar, []

    def quebra_na_segunda(*a, **k):
        chamadas.append(1)
        if len(chamadas) == 2:
            raise RuntimeError("disco cheio")
        return original(*a, **k)

    monkeypatch.setattr(mf, "renderizar", quebra_na_segunda)
    with pytest.raises(RuntimeError):
        mf.renderizar_video(v, mf._validar({"parte_max": 10}), "g")
    assert not mf.pronto(v) and not list(mf.SAIDA.iterdir())


@pytest.fixture
def meta(pastas, monkeypatch):  # noqa: F811
    falsa = MetaFalsa()
    monkeypatch.setattr(ig.requests, "get", falsa.get)
    monkeypatch.setattr(ig.requests, "post", falsa.post)
    monkeypatch.setattr(ig.time, "sleep", lambda s: None)
    monkeypatch.setattr(mf, "duracao", lambda v: 30.0)
    monkeypatch.setattr(ig.link_publico, "compartilhar", falsa.compartilhar)
    monkeypatch.setattr(ig.link_publico, "disponivel", lambda: True)
    return falsa


def _pronto_em_partes(nome, n):
    (mf.ENTRADA / nome).write_bytes(b"original")
    for k in range(1, n + 1):
        mf.saida_parte(mf.ENTRADA / nome, k, n).write_bytes(b"x" * 100)


def test_agenda_uma_publicacao_por_parte_em_ordem(meta, monkeypatch):
    ig.conectar("T")
    _pronto_em_partes("a.mp4", 2)
    monkeypatch.setattr(pf, "_duracao", lambda v: 110.0)   # 110s = 2 partes de 55s
    api = TestClient(pf.app, base_url="http://127.0.0.1")
    r = api.post("/api/ig/agendar", json={"videos": ["a.mp4"]}).json()
    assert r["agendados"] == 2
    p1, p2 = sorted(r["fila"], key=lambda x: x["parte"])
    assert p1["quando"] < p2["quando"]
    assert "Parte 1/2" in p1["legenda"] and "Parte 2/2" in p2["legenda"]
    assert api.post("/api/ig/agendar", json={"videos": ["a.mp4"]}).json()["agendados"] == 0


def test_parte_2_espera_a_parte_1(meta):
    ig.conectar("T")
    _pronto_em_partes("a.mp4", 2)
    ig.agendar([{"video": "a.mp4", "parte": k, "partes": 2, "legenda": str(k)} for k in (1, 2)])
    p1 = next(x for x in ig.fila() if x["parte"] == 1)
    futuro = datetime.now() + timedelta(days=30)
    ig._atualizar(p1["id"], quando=(futuro + timedelta(days=1)).isoformat(timespec="minutes"))
    assert ig.processar_vencidos(futuro) == 0          # so a parte 2 venceu: espera
    ig._atualizar(p1["id"], quando=futuro.isoformat(timespec="minutes"))
    ig.processar_vencidos(futuro)
    ig.processar_vencidos(futuro)
    assert [x["status"] for x in sorted(ig.fila(), key=lambda x: x["parte"])] == \
        ["publicado", "publicado"]


def test_video_de_154s_sobe_o_teto_agora_e_15_min(meta, monkeypatch):
    # a doc da Meta (10/2026) aceita Reels de ate 15 min por API
    ig.conectar("T")
    _pronto_em_partes("a.mp4", 1)
    monkeypatch.setattr(mf, "duracao", lambda v: 154.0)
    assert ig.publicar(mf.saidas_de(mf.ENTRADA / "a.mp4")[0], "x")["media_id"] == "MEDIA1"


def test_horarios_de_quem_posta_a_noite_atravessam_a_meia_noite():
    cfg = {**ig.CONFIG_PADRAO, "por_dia": 5, "inicio": "22:35", "intervalo_min": 180}
    h = ig.horarios(6, cfg, depois_de=datetime(2026, 10, 6, 12, 0))
    assert [x.strftime("%d %H:%M") for x in h] == \
        ["06 22:35", "07 01:35", "07 04:35", "07 07:35", "07 10:35", "07 22:35"]
    # intervalo que estouraria 24h nao invade a janela seguinte: cada janela de
    # 5h em 5h a partir das 22:35 so tem 5 horarios (22:35 ... 18:35), nunca 10.
    # Os da janela de ontem que ainda nao passaram (13:35, 18:35) valem.
    largo = {**cfg, "por_dia": 10, "intervalo_min": 300}
    h = ig.horarios(7, largo, depois_de=datetime(2026, 10, 6, 12, 0))
    assert [x.strftime("%d %H:%M") for x in h] == \
        ["06 13:35", "06 18:35", "06 22:35", "07 03:35", "07 08:35", "07 13:35", "07 18:35"]


def test_reorganizar_reespaca_mantendo_a_ordem(meta):
    ig.conectar("T")
    for n in ("a.mp4", "b.mp4"):
        _pronto_em_partes(n, 2)
    ig.agendar([{"video": v, "parte": k, "partes": 2, "legenda": "x"}
                for v in ("a.mp4", "b.mp4") for k in (1, 2)])
    for i, x in enumerate(sorted(ig.fila(), key=lambda x: (x["video"], x["parte"]))):
        ig._atualizar(x["id"], quando=f"2030-01-{10 + i:02d}T22:35")   # 1 por dia, como o bug
    ig.salvar_config({"por_dia": 5, "inicio": "22:35", "intervalo_min": 180})
    fila = sorted(ig.reorganizar(), key=lambda x: x["quando"])
    assert [(x["video"], x["parte"]) for x in fila] == \
        [("a.mp4", 1), ("a.mp4", 2), ("b.mp4", 1), ("b.mp4", 2)]
    horas = [datetime.fromisoformat(x["quando"]) for x in fila]
    assert all(b - a == timedelta(hours=3) for a, b in zip(horas, horas[1:]))


def test_render_antigo_longo_nao_conta_como_pronto(meta, monkeypatch):
    # 155s renderizado inteiro (antes da divisao): pendente, e nao entra na fila
    (mf.ENTRADA / "longo.mp4").write_bytes(b"original")
    mf.saida_de(mf.ENTRADA / "longo.mp4").write_bytes(b"render antigo inteiro")
    monkeypatch.setattr(pf, "_duracao", lambda v: 155.0)
    ig.conectar("T")
    api = TestClient(pf.app, base_url="http://127.0.0.1")
    v = api.get("/api/videos").json()[0]
    assert v["pronto"] is False and v["partes_previstas"] == 3 and v["partes_prontas"] == 1
    r = api.post("/api/ig/agendar", json={"videos": ["longo.mp4"]})
    assert r.status_code == 400 and ig.fila() == []


def test_processing_failed_vira_explicacao_e_nao_repete():
    e = ig._explicar({"debug_info": {"retriable": False, "type": "ProcessingFailedError",
                                     "message": "Request processing failed"}})
    assert e.fatal and "Duracao maxima de cada parte" in str(e)


def test_parte_de_render_antigo_nao_e_publicada(meta):
    # agendado como parte 1 de 2; depois renderizado de novo em 3 partes
    ig.conectar("T")
    _pronto_em_partes("a.mp4", 2)
    ig.agendar([{"video": "a.mp4", "parte": 1, "partes": 2, "legenda": "x"}])
    mf.apagar_saidas(mf.ENTRADA / "a.mp4")
    _pronto_em_partes("a.mp4", 3)
    ig.processar_vencidos(datetime.now() + timedelta(days=30))
    item = ig.fila()[0]
    assert item["status"] == "erro" and "3 parte" in item["erro"]
    assert not any("rupload" in c[1] for c in meta.chamadas)


def test_tirar_todos_os_pendentes_mantem_publicados(meta):
    ig.conectar("T")
    _pronto_em_partes("a.mp4", 1)
    _pronto_em_partes("b.mp4", 1)
    ig.agendar([{"video": v, "legenda": "x"} for v in ("a.mp4", "b.mp4")])
    ig._atualizar(ig.fila()[0]["id"], status="publicado")
    assert [x["status"] for x in ig.remover_pendentes()] == ["publicado"]


def test_padrao_de_parte_e_60s():
    assert mf._validar({})["parte_max"] == 60


def test_codificacao_segue_a_especificacao_do_reels():
    cod = mf.codificacao_reels(60)
    assert cod[cod.index("-b:a") + 1] == "128k" and cod[cod.index("-ar") + 1] == "48000"
    assert cod[cod.index("-use_editlist") + 1] == "0" and "+faststart" in cod
    assert cod[cod.index("-g") + 1] == "60"
    assert mf.kbps_maximo(60) == mf.TETO_KBPS                  # curto: teto normal
    k = mf.kbps_maximo(900)                                    # 15 min cabe em 280 MB
    assert (k + 128) * 900 / 8 / 1000 <= mf.TETO_MB


def test_parte_max_aceita_ate_15_min():
    assert mf._validar({"parte_max": 900})["parte_max"] == 900
    assert mf._validar({"parte_max": 5000})["parte_max"] == 900


def test_legenda_do_post_com_e_sem_marcador():
    com = {**ig.CONFIG_PADRAO, "legenda_modelo": "{gancho} {parte}", "hashtags": ""}
    assert ig.montar_legenda("G", com, 2, 3) == "G Parte 2/3"
    assert ig.montar_legenda("G", com, 1, 1) == "G"
    sem = {**ig.CONFIG_PADRAO, "legenda_modelo": "{gancho}", "hashtags": "#x"}
    assert ig.montar_legenda("G", sem, 1, 2) == "G\n\nParte 1/2\n\n#x"
