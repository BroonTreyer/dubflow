"""Legenda do post pela IA (scripts/legenda_ia_filmes.py), sem chamar IA de verdade."""

from __future__ import annotations

import json
from types import SimpleNamespace

from scripts import legenda_ia_filmes as lia

CFG = {"hashtags": "#filmes #cinema", "cta": "Acesse o link da bio!"}


def test_compor_junta_filme_parte_chamada_e_hashtags_sem_repetir():
    t = lia.compor("Olha isso 😱", ["#Suspense", "filmes", "plot twist!", "#parte2"],
                   "#filmes #cinema", 2, 3, filme="Tropa de Elite (2007)", cta="Link na bio!")
    assert t == ("Olha isso 😱\n\n🎬 Filme: Tropa de Elite (2007)\n\n"
                 "▶️ Parte 2/3 — segue o perfil pra não perder a parte 3!\n\n"
                 "Link na bio!\n\n#filmes #cinema #suspense #plottwist")
    assert "(final)" in lia.compor("x", [], "", 3, 3)
    assert lia.compor("x", [], "", 1, 1) == "x"


def _cache(v, segs):
    v.with_suffix(".legenda.json").write_text(json.dumps({"chave": "x", "segments": segs}),
                                             encoding="utf-8")


def test_falas_vem_do_cache_por_trecho_e_nunca_transcrevem(tmp_path):
    v = tmp_path / "a.mp4"
    assert lia.falas_de(v) == ""
    _cache(v, [{"start": 0, "end": 2, "text": "Corre!"},
               {"start": 2, "end": 4, "text": " Ele voltou."},
               {"start": 31, "end": 33, "text": "Acabou."}])
    assert lia.falas_de(v) == "Corre! Ele voltou. Acabou."
    assert lia.falas_de(v, 0, 30) == "Corre! Ele voltou."
    assert lia.falas_de(v, 30, 60) == "Acabou."


def test_mensagem_tem_quadros_e_falas_de_cada_parte(tmp_path, monkeypatch):
    v = tmp_path / "a.mp4"
    _cache(v, [{"start": 1, "end": 2, "text": "Primeira"}, {"start": 50, "end": 51, "text": "Segunda"}])
    monkeypatch.setattr(lia, "trechos", lambda video, n: [(0.0, 30.0), (30.0, 60.0)][:n])
    monkeypatch.setattr(lia, "_quadro_jpeg", lambda video, t: "QUADRO")
    blocos = lia._mensagem(v, "Gancho", 2, [1, 2])
    imagens = [b for b in blocos if b["type"] == "image"]
    assert len(imagens) == 6 and imagens[0]["source"]["data"] == "QUADRO"
    textos = " ".join(b["text"] for b in blocos if b["type"] == "text")
    assert "Falas da parte 1: Primeira" in textos and "Falas da parte 2: Segunda" in textos


def _resposta(dados):
    return SimpleNamespace(refusal=False, json=lambda: dados)


def test_gerar_uma_chamada_por_video_e_filme_so_com_certeza(tmp_path, monkeypatch):
    from app.pipeline import llm
    monkeypatch.setattr(lia, "_mensagem", lambda video, g, n, pedir: [{"type": "text", "text": g}])
    chamadas = []

    def falso(role, tarefas, **k):
        chamadas.append((role, len(tarefas)))
        return [_resposta({"filme": "Filme A", "certeza": "alta", "hashtags": ["drama"],
                           "partes": [{"parte": 1, "texto": "A1"}, {"parte": 2, "texto": "A2"}]}),
                _resposta({"filme": "Chute", "certeza": "media", "hashtags": [],
                           "partes": [{"parte": 1, "texto": "B1"}]}),
                None]
    monkeypatch.setattr(llm, "map_json", falso)
    a, b, c = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "c.mp4"
    pedidos = [{"video": a, "gancho": "G", "parte": 1, "partes": 2},
               {"video": a, "gancho": "G", "parte": 2, "partes": 2},
               {"video": b, "gancho": "H"},
               {"video": c, "gancho": "I"}]
    r = lia.gerar(pedidos, CFG)
    assert chamadas == [(llm.ROLE_CLIP, 3)]                  # a (2 partes) numa chamada so
    assert r[0].startswith("A1\n\n🎬 Filme: Filme A\n\n▶️ Parte 1/2")
    assert r[1].startswith("A2") and "Acesse o link da bio!" in r[1] and "#drama" in r[1]
    assert "Chute" not in r[2] and r[2].startswith("B1")    # sem certeza: nao cita
    assert r[3] is None                                     # IA nao respondeu: modelo fixo


def test_gerar_sem_provedor_nao_quebra(tmp_path, monkeypatch):
    from app.pipeline import llm
    monkeypatch.setattr(lia, "_mensagem", lambda *a: [])

    def cai(*a, **k):
        raise llm.AllProvidersDown("sem credito")
    monkeypatch.setattr(llm, "map_json", cai)
    assert lia.gerar([{"video": tmp_path / "a.mp4", "gancho": "G"}], CFG) == [None]


def test_imagens_viram_data_url_na_openai():
    from app.pipeline import llm
    blocos = [{"type": "text", "text": "oi"},
              {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAA"}}]
    assert llm._blocos_openai(blocos) == [
        {"type": "text", "text": "oi"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}]
    assert llm._blocos_openai("texto") == "texto"
