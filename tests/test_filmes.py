"""Testes do molde de filmes (scripts/molde_filmes.py) e do painel (painel_filmes.py).

Video sintetico (testsrc) e pastas temporarias: nao toca em filmes/ de verdade.

    .venv\\Scripts\\python.exe -m pytest tests/test_filmes.py -q
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts import molde_filmes as mf
from scripts import painel_filmes as pf


def _video(path: Path, w: int = 720, h: int = 1280, segundos: float = 3) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30",
         "-f", "lavfi", "-i", "sine", "-t", str(segundos), "-shortest",
         "-c:v", "libx264", "-preset", "ultrafast", str(path)],
        check=True)
    return path


def _probe(path: Path) -> dict:
    saida = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    return json.loads(saida)


@pytest.fixture
def pastas(tmp_path, monkeypatch):
    raiz = tmp_path / "filmes"
    monkeypatch.setattr(mf, "RAIZ", raiz)
    monkeypatch.setattr(mf, "ENTRADA", raiz / "entrada")
    monkeypatch.setattr(mf, "SAIDA", raiz / "saida")
    monkeypatch.setattr(mf, "ARQ_MOLDE", raiz / "molde.json")
    (raiz / "entrada").mkdir(parents=True)
    (raiz / "saida").mkdir()
    pf._quadros.clear()
    pf._duracoes.clear()
    return raiz


@pytest.fixture
def api(pastas):
    # o painel so aceita Host local (protecao contra DNS rebinding)
    return TestClient(pf.app, base_url="http://127.0.0.1")


# ------------------------------------------------------------------ molde

def test_molde_invalido_volta_pro_padrao():
    m = mf._validar({"cor_fundo": "azul", "tamanho_cta": "999", "ganchos": "x",
                     "caixa_alta": 0, "perfil": 123})
    assert m["cor_fundo"] == mf.PADRAO["cor_fundo"]
    assert m["tamanho_cta"] == mf.LIMITES["tamanho_cta"][1]
    assert m["ganchos"] == mf.PADRAO["ganchos"]
    assert m["caixa_alta"] is False
    assert m["perfil"] == "123"


def test_molde_corrompido_nao_derruba(pastas):
    mf.ARQ_MOLDE.write_text("{nao e json", encoding="utf-8")
    assert mf.carregar_molde()["perfil"] == mf.PADRAO["perfil"]


def test_gancho_automatico_estavel_pelo_nome():
    molde = mf._validar({})
    v = Path("x/filme_abc.mp4")
    assert mf.gancho_de(v, 0, molde) == mf.gancho_de(v, 99, molde)


@pytest.mark.parametrize("wh", [(1920, 1080), (720, 1280), (1080, 1080), (4000, 300)])
def test_caixa_do_video_cabe_na_tela(wh):
    molde = mf._validar({})
    img, (x, y, w, h) = mf.montar_fundo(molde, "Um gancho bem comprido " * 4, wh)
    assert img.size == (mf.LARGURA, mf.ALTURA)
    assert 0 <= x and x + w <= mf.LARGURA and 0 <= y and y + h <= mf.ALTURA
    assert w % 2 == 0 and h % 2 == 0  # libx264 exige lados pares


# ------------------------------------------------------------------ corte

def test_corte_nunca_some_com_o_quadro():
    c = mf.normalizar_corte({"topo": 80, "base": 80, "esq": -5, "dir": "x"})
    assert c["topo"] + c["base"] <= 90
    assert c["esq"] == 0 and c["dir"] == 0


def test_area_corte_pares_e_dentro():
    x, y, w, h = mf.area_corte((721, 1281), mf.normalizar_corte({"topo": 33.3, "esq": 7}))
    assert all(n % 2 == 0 for n in (x, y, w, h))
    assert x + w <= 721 and y + h <= 1281


def test_corte_proprio_zerado_vence_o_padrao(pastas):
    v = _video(mf.ENTRADA / "a.mp4")
    molde = mf._validar({"corte_padrao": {"topo": 20}})
    assert mf.corte_de(v, molde)["topo"] == 20
    mf.salvar_corte(v, {})                     # corte proprio zerado
    assert mf.corte_de(v, molde)["topo"] == 0
    mf.salvar_corte(v, None)                   # volta pro padrao
    assert mf.corte_de(v, molde)["topo"] == 20


# ------------------------------------------------------------------ render

def test_render_com_corte_tempo_e_cantos(pastas):
    v = _video(mf.ENTRADA / "a.mp4", 1280, 720, segundos=5)
    saida = mf.SAIDA / "a_molde.mp4"
    progresso = []
    molde = mf._validar({"cantos_video": 30})
    mf.renderizar(v, saida, molde, "teste",
                  {"topo": 10, "base": 10, "inicio": 1, "fim": 3}, progresso.append)
    info = _probe(saida)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - 2) < 0.2
    assert progresso and progresso[-1] <= 1
    assert not list(mf.SAIDA.glob("*.parcial.mp4"))


def test_render_que_falha_nao_deixa_lixo(pastas):
    v = mf.ENTRADA / "quebrado.mp4"
    v.write_bytes(b"isso nao e video")
    with pytest.raises(subprocess.CalledProcessError):
        mf.renderizar(v, mf.SAIDA / "q.mp4", mf._validar({}), "x")
    assert not list(mf.SAIDA.iterdir())


# ------------------------------------------------------------------ painel

def test_host_externo_e_recusado(pastas):
    r = TestClient(pf.app, base_url="http://evil.example").get("/api/status")
    assert r.status_code == 400


def test_upload_substitui_e_invalida_render(api):
    origem = _video(mf.ENTRADA.parent / "x.mp4")
    with open(origem, "rb") as f:
        assert api.post("/api/upload", files={"arquivos": ("v.mp4", f, "video/mp4")}).is_success
    mf.saida_de(mf.ENTRADA / "v.mp4").write_bytes(b"render velho")
    with open(origem, "rb") as f:
        api.post("/api/upload", files={"arquivos": ("v.mp4", f, "video/mp4")})
    assert not mf.saida_de(mf.ENTRADA / "v.mp4").exists()
    assert not list(mf.ENTRADA.glob("*.enviando"))


def test_upload_ignora_caminho_malicioso(api):
    r = api.post("/api/upload", files={"arquivos": ("../../fora.mp4", b"x", "video/mp4")})
    assert r.json()["salvos"] == ["fora.mp4"]
    assert (mf.ENTRADA / "fora.mp4").exists()
    assert not (mf.RAIZ.parent / "fora.mp4").exists()


def test_previa_de_video_curto(api):
    _video(mf.ENTRADA / "curto.mp4", segundos=0.5)
    r = api.post("/api/previa", json={"video": "curto.mp4", "molde": {}})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_lote_corte_render_e_limpeza(api):
    nomes = [f"v{i}.mp4" for i in range(4)]
    for n in nomes:
        _video(mf.ENTRADA / n, segundos=2)

    r = api.post("/api/corte", json={"videos": nomes[:2], "corte": {"topo": 10}})
    assert r.json()["alterados"] == 2
    r = api.post("/api/corte/padrao", json={"corte": {"base": 5}})
    lista = {v["nome"]: v for v in api.get("/api/videos").json()}
    assert lista["v0.mp4"]["corte"]["topo"] == 10 and lista["v0.mp4"]["corte_proprio"]
    assert lista["v3.mp4"]["corte"]["base"] == 5 and not lista["v3.mp4"]["corte_proprio"]

    assert api.post("/api/renderizar", json={"simultaneos": 3}).json()["total"] == 4
    for _ in range(120):
        s = api.get("/api/status").json()
        if not s["rodando"]:
            break
        time.sleep(0.25)
    assert s["feitos"] == 4 and not s["erros"]
    assert all(v["pronto"] for v in api.get("/api/videos").json())

    # apagar exige selecao explicita ou "todos"
    assert api.post("/api/limpar", json={"alvo": "originais"}).status_code == 400
    r = api.post("/api/limpar", json={"alvo": "originais", "videos": ["v0.mp4"]})
    assert r.json()["limpos"] == 1 and not (mf.ENTRADA / "v0.mp4").exists()
    r = api.post("/api/limpar", json={"alvo": "originais", "todos": True,
                                      "somente_prontos": True})
    assert r.json()["limpos"] == 3
    assert api.get("/api/videos").json() == []


def test_mudar_corte_padrao_invalida_so_quem_usa_o_padrao(api):
    for n in ("p.mp4", "q.mp4"):
        _video(mf.ENTRADA / n, segundos=1)
        mf.saida_de(mf.ENTRADA / n).write_bytes(b"render")
    api.post("/api/corte", json={"videos": ["p.mp4"], "corte": {}})  # p: corte proprio
    mf.saida_de(mf.ENTRADA / "p.mp4").write_bytes(b"render")
    r = api.post("/api/corte/padrao", json={"corte": {"topo": 15}})
    assert r.json()["renders_apagados"] == 1
    assert mf.saida_de(mf.ENTRADA / "p.mp4").exists()
    assert not mf.saida_de(mf.ENTRADA / "q.mp4").exists()


# ------------------------------------------------------------------ 2a revisao

def test_video_de_celular_com_rotacao(pastas):
    v = _video(mf.ENTRADA / "deitado.mp4", 1280, 720, segundos=1)
    girado = mf.ENTRADA / "girado.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-display_rotation", "90",
                    "-i", str(v), "-c", "copy", str(girado)], check=True)
    assert mf.dimensoes(girado) == (720, 1280)
    mf.renderizar(girado, mf.SAIDA / "g.mp4", mf._validar({}), "x", {"topo": 10})
    assert (mf.SAIDA / "g.mp4").exists()


def test_inicio_depois_do_fim_e_recusado(pastas):
    v = _video(mf.ENTRADA / "a.mp4", segundos=2)
    with pytest.raises(ValueError):
        mf.renderizar(v, mf.SAIDA / "a.mp4", mf._validar({}), "x", {"inicio": 5})
    assert not list(mf.SAIDA.iterdir())


def test_texto_comprido_nao_vaza(pastas):
    molde = mf._validar({"perfil": "@" + "perfil_gigante" * 6, "cta": "SEGUE " * 12})
    img, _ = mf.montar_fundo(molde, "@" + "palavraenorme" * 8, (1280, 720))
    # nenhuma tinta na faixa de 8px das bordas laterais
    fundo = mf._cor(molde["cor_fundo"])
    for x in (*range(0, 8), *range(mf.LARGURA - 8, mf.LARGURA)):
        assert all(img.getpixel((x, y)) == fundo for y in range(0, mf.ALTURA, 4))


def test_mesmo_nome_outra_extensao_nao_colide(api):
    origem = _video(mf.ENTRADA.parent / "x.mp4", segundos=1)
    dados = origem.read_bytes()
    api.post("/api/upload", files={"arquivos": ("cena.mp4", dados, "video/mp4")})
    r = api.post("/api/upload", files={"arquivos": ("cena.mov", dados, "video/quicktime")})
    novo = r.json()["salvos"][0]
    assert novo != "cena.mov" and Path(novo).stem != "cena"
    assert mf.saida_de(mf.ENTRADA / novo) != mf.saida_de(mf.ENTRADA / "cena.mp4")


def test_edicao_bloqueada_durante_render(api, monkeypatch):
    _video(mf.ENTRADA / "a.mp4", segundos=1)
    monkeypatch.setitem(pf._status, "rodando", True)
    monkeypatch.setitem(pf._status, "fila", {"a.mp4"})
    assert api.post("/api/corte", json={"videos": ["a.mp4"], "corte": {}}).status_code == 409
    assert api.post("/api/gancho", json={"video": "a.mp4", "gancho": "x"}).status_code == 409
    assert api.post("/api/corte/padrao", json={"corte": {}}).status_code == 409


# ------------------------------------------------------------------ legenda

FALAS = [{"id": 0, "start": 0.2, "end": 1.5, "text": "Ninguém sabia o que havia ali."}]


def test_render_queima_legenda(pastas):
    v = _video(mf.ENTRADA / "a.mp4", 1280, 720, segundos=2)
    sem, com = mf.SAIDA / "sem.mp4", mf.SAIDA / "com.mp4"
    mf.renderizar(v, sem, mf._validar({}), "x")
    mf.renderizar(v, com, mf._validar({}), "x", legenda=FALAS)

    def quadro(arq):
        return subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", "1", "-i", str(arq),
                               "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                              check=True, capture_output=True).stdout
    assert quadro(sem) != quadro(com)  # a legenda mudou a imagem


def test_legenda_sem_audio_nao_transcreve(pastas, monkeypatch):
    from scripts import legenda_filmes as lf
    v = mf.ENTRADA / "mudo.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=320x240:rate=30", "-t", "1", str(v)], check=True)
    monkeypatch.setattr("app.pipeline.transcribe.transcribe",
                        lambda *_: pytest.fail("nao devia transcrever video mudo"))
    assert lf.segmentos(v) == []


def test_legenda_usa_cache_e_invalida_com_corte(pastas, monkeypatch):
    from scripts import legenda_filmes as lf
    v = _video(mf.ENTRADA / "a.mp4", segundos=2)
    chamadas = []
    monkeypatch.setattr("app.pipeline.transcribe.transcribe",
                        lambda wav: chamadas.append(wav) or {"language": "pt", "segments": FALAS})
    monkeypatch.setattr("app.pipeline.translate.translate_segments", lambda falas, meta: falas)
    assert lf.segmentos(v) == FALAS
    assert lf.segmentos(v) == FALAS and len(chamadas) == 1      # cache
    lf.segmentos(v, inicio=0.5)
    assert len(chamadas) == 2                                    # outro trecho


def test_api_legenda_lote_e_limpeza(api, monkeypatch):
    from scripts import legenda_filmes as lf
    for n in ("a.mp4", "b.mp4"):
        _video(mf.ENTRADA / n, segundos=1)
        mf.saida_de(mf.ENTRADA / n).write_bytes(b"render")
    r = api.post("/api/legenda", json={"videos": ["a.mp4"], "legendar": True}).json()
    assert r["renders_apagados"] == 1 and mf.saida_de(mf.ENTRADA / "b.mp4").exists()
    lista = {v["nome"]: v for v in api.get("/api/videos").json()}
    assert lista["a.mp4"]["legendar"] and not lista["b.mp4"]["legendar"]

    # padrao do molde: b segue o padrao (render invalida), a tem escolha propria
    mf.saida_de(mf.ENTRADA / "a.mp4").write_bytes(b"render")
    molde = api.get("/api/molde").json()["molde"]
    api.post("/api/molde", json={**molde, "legendar_padrao": True})
    assert mf.saida_de(mf.ENTRADA / "a.mp4").exists()
    assert not mf.saida_de(mf.ENTRADA / "b.mp4").exists()

    (mf.ENTRADA / "a.legenda.json").write_text("{}")
    api.post("/api/limpar", json={"alvo": "originais", "videos": ["a.mp4"]})
    assert not any(mf.ENTRADA.glob("a.*"))


def test_render_do_painel_passa_legenda(api, monkeypatch):
    from scripts import legenda_filmes as lf
    _video(mf.ENTRADA / "a.mp4", segundos=1)
    api.post("/api/legenda", json={"videos": ["a.mp4"], "legendar": True})
    monkeypatch.setattr(lf, "segmentos", lambda v, i, f: FALAS)
    recebido = {}
    original = mf.renderizar
    monkeypatch.setattr(mf, "renderizar",
                        lambda *a, **k: recebido.update(k) or original(*a, **k))
    api.post("/api/renderizar", json={})
    for _ in range(80):
        if not api.get("/api/status").json()["rodando"]:
            break
        time.sleep(0.25)
    assert recebido.get("legenda") == FALAS
    assert mf.saida_de(mf.ENTRADA / "a.mp4").exists()


# ------------------------------------------------------------------ corte em massa

def test_miniatura_e_quadro_reduzido(api):
    _video(mf.ENTRADA / "a.mp4", 1280, 720, segundos=2)
    r = api.get("/api/quadro/a.mp4?lado=360")
    from PIL import Image
    import io as _io
    assert max(Image.open(_io.BytesIO(r.content)).size) == 360
    r1 = api.get("/api/miniatura/a.mp4?topo=0&base=0")
    r2 = api.get("/api/miniatura/a.mp4?topo=30&base=30")
    assert r1.status_code == r2.status_code == 200
    assert Image.open(_io.BytesIO(r1.content)).size == (270, 480)
    assert r1.content != r2.content            # o corte muda o resultado
    assert not mf.tem_corte_proprio(mf.ENTRADA / "a.mp4")  # so mostra, nao grava
