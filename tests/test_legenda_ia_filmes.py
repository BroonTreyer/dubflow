"""Legenda do post pela IA (scripts/legenda_ia_filmes.py), sem chamar IA de verdade."""

from __future__ import annotations

import json
from types import SimpleNamespace

from scripts import legenda_ia_filmes as lia


def test_compor_junta_parte_e_hashtags_sem_repetir():
    t = lia.compor("Olha isso 😱", ["#Suspense", "filmes", "plot twist!"], "#filmes #cinema", 2, 3)
    assert t == "Olha isso 😱\n\nParte 2/3\n\n#filmes #cinema #suspense #plottwist"


def test_falas_vem_do_cache_e_nunca_transcrevem(tmp_path):
    v = tmp_path / "a.mp4"
    assert lia.falas_de(v) == ""
    v.with_suffix(".legenda.json").write_text(json.dumps(
        {"chave": "x", "segments": [{"text": "Corre!"}, {"text": " Ele voltou."}]}),
        encoding="utf-8")
    assert lia.falas_de(v) == "Corre! Ele voltou."


def test_gerar_usa_o_modelo_e_cai_para_none(tmp_path, monkeypatch):
    from app.pipeline import llm
    respostas = [SimpleNamespace(refusal=False,
                                 json=lambda: {"legenda": "Boa", "hashtags": ["terror"]}),
                 None]
    monkeypatch.setattr(llm, "map_json", lambda role, tarefas, **k: respostas[:len(tarefas)])
    pedidos = [{"video": tmp_path / "a.mp4", "gancho": "G"},
               {"video": tmp_path / "b.mp4", "gancho": "H"}]
    assert lia.gerar(pedidos, "#filmes") == ["Boa\n\n#filmes #terror", None]


def test_gerar_sem_provedor_nao_quebra(tmp_path, monkeypatch):
    from app.pipeline import llm

    def cai(*a, **k):
        raise llm.AllProvidersDown("sem credito")
    monkeypatch.setattr(llm, "map_json", cai)
    assert lia.gerar([{"video": tmp_path / "a.mp4", "gancho": "G"}], "") == [None]
