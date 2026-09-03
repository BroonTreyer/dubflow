r"""Redistribui as publicacoes PENDENTES entre os canais elegiveis.

    .venv\Scripts\python.exe -m scripts.rebalancear_fila            # simulacao
    .venv\Scripts\python.exe -m scripts.rebalancear_fila --apply

Existe porque `distribute_episode` roteia UMA vez, no momento em que o episodio
termina: `assign_round_robin` reparte os cortes entre os canais ativos daquele
nicho e grava o `channel_id` no post. Um canal criado depois nasce sem nada —
o acervo ja agendado continua inteiro com quem existia antes, e o canal novo so
comeca a receber no proximo episodio processado.

Este script refaz o rodizio sobre o que ainda esta `pending`, episodio por
episodio, mantendo a regra que existe para nao ser penalizado pelo YouTube:
**um corte vai para um so canal**. Elegibilidade copia a do `distribute`: canal
ativo, mesmo nicho e mesmo idioma dos que ja seguravam aquele episodio.

Nao mexe no que ja foi publicado. Depois de aplicar, rode `reagendar_fila`:
os horarios antigos foram calculados para outra reparticao.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline.distribute import _channel_lang, _slug, assign_round_robin  # noqa: E402


def _pendentes_por_episodio(con: sqlite3.Connection) -> dict[int, list[tuple[int, int]]]:
    """{episode_id: [(post_id, channel_id), ...]} na ordem do corte."""
    out: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for ep, pid, canal in con.execute(
        "SELECT c.episode_id, p.id, p.channel_id FROM posts p"
        " JOIN clips c ON c.id = p.clip_id"
        " WHERE p.status = 'pending' ORDER BY c.episode_id, c.id, p.id"
    ):
        out[ep].append((pid, canal))
    return out


def rebalancear(aplicar: bool) -> int:
    ativos = {c["id"]: c for c in db.list_channels(only_active=True)}
    con = sqlite3.connect(settings.db_path)
    mudancas = 0
    try:
        for ep, posts in sorted(_pendentes_por_episodio(con).items()):
            donos = [ativos[c] for _, c in posts if c in ativos]
            if not donos:
                print(f"  ep {ep}: nenhum canal ativo segura estes posts — pulado")
                continue
            # Elegiveis: mesmo nicho e mesmo idioma de quem ja segurava o episodio.
            nichos = {_slug(d.get("niche") or "") for d in donos}
            idiomas = {_channel_lang(d) for d in donos}
            alvo = sorted(
                c["id"] for c in ativos.values()
                if _slug(c.get("niche") or "") in nichos and _channel_lang(c) in idiomas
            )
            novo = assign_round_robin([pid for pid, _ in posts], alvo)
            destino = {pid: canal for canal, pids in novo.items() for pid in pids}
            movidos = sum(1 for pid, antes in posts if destino[pid] != antes)
            print(f"  ep {ep}: {len(posts)} pendentes entre {alvo} — {movidos} mudam de canal")
            if aplicar:
                for pid, _ in posts:
                    con.execute("UPDATE posts SET channel_id = ? WHERE id = ?",
                                (destino[pid], pid))
            mudancas += movidos
        if aplicar:
            con.commit()
    finally:
        con.close()
    return mudancas


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="grava (sem isso, so simula)")
    args = ap.parse_args()
    n = rebalancear(args.apply)
    verbo = "mudaram" if args.apply else "mudariam"
    print(f"\n{n} publicacoes {verbo} de canal.")
    if args.apply:
        print(r"Agora rode: .venv\Scripts\python.exe -m scripts.reagendar_fila --apply")
