"""Servidor de entrega do link temporario (scripts/link_publico.py), so local: sem tunel."""

from __future__ import annotations

import requests

from scripts import link_publico as lp

TOKEN = "T" * 43


def _servir(tmp_path, monkeypatch):
    arq = tmp_path / "v.mp4"
    arq.write_bytes(bytes(range(256)) * 40)          # 10240 bytes
    porta = lp._subir_servidor()
    monkeypatch.setitem(lp._arquivos, TOKEN, arq)
    return f"http://127.0.0.1:{porta}", arq


def test_entrega_o_arquivo_inteiro_e_por_faixa(tmp_path, monkeypatch):
    base, arq = _servir(tmp_path, monkeypatch)
    url = f"{base}/v/{TOKEN}.mp4"
    r = requests.get(url, timeout=5)
    assert r.status_code == 200 and r.content == arq.read_bytes()
    assert r.headers["Content-Type"] == "video/mp4"
    r = requests.get(url, headers={"Range": "bytes=100-199"}, timeout=5)
    assert r.status_code == 206 and r.content == arq.read_bytes()[100:200]
    assert r.headers["Content-Range"] == "bytes 100-199/10240"
    assert requests.get(url, headers={"Range": "bytes=-10"}, timeout=5).content == \
        arq.read_bytes()[-10:]
    assert requests.head(url, timeout=5).headers["Content-Length"] == "10240"


def test_so_entrega_o_que_foi_liberado(tmp_path, monkeypatch):
    base, _ = _servir(tmp_path, monkeypatch)
    for caminho in (f"/v/{'X' * 43}.mp4", "/", "/v/../painel.log", "/api/ig",
                    f"/v/{TOKEN}.txt"):
        assert requests.get(base + caminho, timeout=5).status_code == 404, caminho
