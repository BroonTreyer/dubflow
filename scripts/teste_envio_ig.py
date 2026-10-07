"""Mede ate que duracao o envio direto do Instagram aceita, SEM publicar nada.

Para cada duracao: renderiza um trecho de um video real com o molde e o render
atual, sobe pelo mesmo caminho da publicacao (rupload) e espera a Meta processar.
Para no container: nunca chama media_publish, entao nada aparece no perfil (a
Meta descarta containers nao publicados em 24h).

Uso (na pasta dubflow, com o painel conectado a uma conta):
    .venv\\Scripts\\python.exe -m scripts.teste_envio_ig
    .venv\\Scripts\\python.exe -m scripts.teste_envio_ig --duracoes 60,120,300 --video nome.mp4
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from scripts import instagram_filmes as ig
from scripts import molde_filmes as mf


def medir(conta: dict, arquivo: Path) -> tuple[str, float]:
    inicio = time.time()
    try:
        container = ig._subir(conta, arquivo, "teste de envio (nao publicado)")
        ig._esperar(container, conta["token"], 1800)
        return "ACEITO", time.time() - inicio
    except ig.ErroInstagram as exc:
        return f"RECUSADO: {exc}", time.time() - inicio


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--duracoes", default="60,77,120,180,300")
    p.add_argument("--video", help="nome do video em filmes/entrada (padrao: o mais longo)")
    args = p.parse_args()

    conta = ig._conta(None)
    molde = mf.carregar_molde()
    videos = mf.listar_videos()
    video = mf.ENTRADA / args.video if args.video else max(videos, key=mf.duracao)
    total = mf.duracao(video)
    print(f"conta @{conta['username']} | video {video.name} ({total:.0f}s)\n")
    with tempfile.TemporaryDirectory() as tmp:
        for seg in (float(s) for s in args.duracoes.split(",")):
            if seg > total:
                print(f"{seg:>5.0f}s  pulado (o video tem {total:.0f}s)")
                continue
            arq = Path(tmp) / f"teste_{seg:.0f}.mp4"
            corte = {**mf.corte_de(video, molde), "inicio": 0.0, "fim": seg}
            mf.renderizar(video, arq, molde, mf.gancho_de(video, None, molde), corte)
            mb = arq.stat().st_size / 1e6
            resultado, gasto = medir(conta, arq)
            print(f"{seg:>5.0f}s  {mb:6.1f} MB  {gasto:5.0f}s  {resultado}", flush=True)


if __name__ == "__main__":
    main()
