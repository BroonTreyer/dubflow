r"""Reespaca a fila de publicacoes pendentes a partir de agora.

    .venv\Scripts\python.exe -m scripts.reagendar_fila            # simulacao
    .venv\Scripts\python.exe -m scripts.reagendar_fila --apply
    .venv\Scripts\python.exe -m scripts.reagendar_fila --por-dia 3 --apply

Existe por causa de uma armadilha de cota: a YouTube Data API da 10000 unidades
por DIA e por PROJETO Cloud (nao por canal), e um upload custa ~1600 — cabem ~6
uploads/dia somando TODOS os canais. Com a maquina parada a fila acumula posts
vencidos; subir o worker assim publica os 6 primeiros e derruba o resto em 403
de cota. Como o backoff e curto (2/4/8 min), as 4 tentativas queimam em ~15 min
e os posts viram `failed` — perda definitiva, nao adiamento.

Reagendar preserva a ordem (`scheduled_at`, `id`) e usa os mesmos slots do
`distribute.plan_schedule`, entao a fila reespacada e indistinguivel de uma
recem-planejada. Nada e descartado: so anda para a frente.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.pipeline.distribute import _iter_slots  # noqa: E402


def _pendentes(con: sqlite3.Connection) -> dict[int | None, list[tuple[int, str]]]:
    """Posts pendentes por canal, na ordem em que devem sair."""
    por_canal: dict[int | None, list[tuple[int, str]]] = defaultdict(list)
    for pid, canal, agendado in con.execute(
        "SELECT id, channel_id, scheduled_at FROM posts WHERE status = 'pending'"
        " ORDER BY COALESCE(scheduled_at, ''), id"
    ):
        por_canal[canal].append((pid, agendado))
    return por_canal


def _ritmo(con: sqlite3.Connection, canal: int | None, forcado: int | None) -> int:
    if forcado:
        return forcado
    if canal is None:
        return settings.distribute_per_day
    linha = con.execute(
        "SELECT posts_per_day FROM channels WHERE id = ?", (canal,)
    ).fetchone()
    return (linha[0] if linha else None) or settings.distribute_per_day


def reagendar(por_dia: int | None, aplicar: bool) -> int:
    con = sqlite3.connect(settings.db_path)
    agora = datetime.now(timezone.utc)
    total = 0
    try:
        for canal, posts in sorted(_pendentes(con).items(), key=lambda kv: (kv[0] or 0)):
            ritmo = _ritmo(con, canal, por_dia)
            nome = con.execute(
                "SELECT name FROM channels WHERE id = ?", (canal,)
            ).fetchone() if canal else None
            slots = _iter_slots(agora, ritmo)
            print(f"\ncanal {canal} ({nome[0] if nome else 'sem canal'}): "
                  f"{len(posts)} pendentes, {ritmo}/dia")
            for pid, antes in posts:
                novo = next(slots).isoformat(timespec="seconds")
                if aplicar:
                    # `attempts` volta a zero: as tentativas gastas foram contra a
                    # cota, nao contra o video — mante-las condenaria o post.
                    con.execute(
                        "UPDATE posts SET scheduled_at = ?, attempts = 0, error = NULL"
                        " WHERE id = ?", (novo, pid))
                total += 1
                if total <= 6 or novo[:10] != antes[:10]:
                    print(f"  post {pid}: {antes} -> {novo}")
        if aplicar:
            con.commit()
    finally:
        con.close()
    return total


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--por-dia", type=int, default=None,
                    help="posts por dia por canal (padrao: o do proprio canal)")
    ap.add_argument("--apply", action="store_true", help="grava (sem isso, so simula)")
    args = ap.parse_args()
    n = reagendar(args.por_dia, args.apply)
    print(f"\n{n} posts {'reagendados' if args.apply else 'seriam reagendados'}.")
