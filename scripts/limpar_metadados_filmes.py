"""Tira os metadados dos renders que ja estao prontos em filmes/saida.

Renders novos ja saem limpos (molde_filmes.SEM_METADADOS); isto e para os
antigos. Sem recodificar: so copia os fluxos, entao e rapido e nao perde
qualidade. Pula o que ja esta limpo, entao pode rodar de novo sem custo.

    .venv/Scripts/python.exe -m scripts.limpar_metadados_filmes
"""
from __future__ import annotations

import json
import subprocess
import sys

from scripts import molde_filmes as mf


def sujo(arquivo) -> bool:
    d = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags",
         "-of", "json", str(arquivo)], capture_output=True, text=True, check=True).stdout)
    tags = set(d.get("format", {}).get("tags", {}))
    for s in d.get("streams", []):
        tags |= set(s.get("tags", {}))
    if tags - {"major_brand", "minor_version", "compatible_brands", "language", "handler_name"}:
        return True
    with open(arquivo, "rb") as f:  # SEI do x264 fica no comeco do video
        return b"x264 - core" in f.read(4 * 1024 * 1024)


def main() -> int:
    arquivos = [s for v in mf.listar_videos() for s in mf.saidas_de(v)]
    limpos = ja = falhas = 0
    for i, a in enumerate(arquivos, 1):
        try:
            if not sujo(a):
                ja += 1
                continue
            mf.limpar_metadados(a)
            limpos += 1
        except (subprocess.CalledProcessError, OSError) as e:
            falhas += 1
            print(f"FALHOU {a.name}: {e}", file=sys.stderr)
        if i % 20 == 0:
            print(f"{i}/{len(arquivos)}", flush=True)
    print(f"limpos {limpos}, ja estavam limpos {ja}, falhas {falhas}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
