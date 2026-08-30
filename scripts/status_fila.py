r"""Duas perguntas que os atalhos da area de trabalho fazem antes de agir.

    .venv\Scripts\python.exe scripts\status_fila.py vencidas
    .venv\Scripts\python.exe scripts\status_fila.py andamento

`vencidas`  numero de publicacoes pendentes cuja hora agendada ja passou. Subir o
            worker com esse numero alto joga tudo no ar de uma vez.
`andamento` episodios em estado nao-terminal ("12 (transcribing); 14 (clipping)").
            Matar o worker agora faz esses episodios recomecarem do zero.

Existe como arquivo, e nao como `python -c` dentro do .ps1, porque o PowerShell 5.1
quebra argumento de exe que contenha aspas duplas: o snippet chegava cortado.
Imprime 0 / linha vazia se o banco ainda nao existe.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

TERMINAIS = ("done", "failed", "canceled", "queued")


def _conectar() -> sqlite3.Connection | None:
    caminho = Path(settings.db_path)
    return sqlite3.connect(caminho) if caminho.exists() else None


def vencidas() -> str:
    con = _conectar()
    if con is None:
        return "0"
    agora = datetime.now(timezone.utc).isoformat()
    total = con.execute(
        "SELECT COUNT(*) FROM posts WHERE status = 'pending'"
        " AND (scheduled_at IS NULL OR scheduled_at <= ?)",
        (agora,),
    ).fetchone()[0]
    return str(total)


def andamento() -> str:
    con = _conectar()
    if con is None:
        return ""
    marcas = ", ".join("?" * len(TERMINAIS))
    linhas = con.execute(
        f"SELECT id, status FROM episodes WHERE status NOT IN ({marcas}) ORDER BY id",
        TERMINAIS,
    ).fetchall()
    return "; ".join(f"{i} ({s})" for i, s in linhas)


COMANDOS = {"vencidas": vencidas, "andamento": andamento}

if __name__ == "__main__":
    escolha = sys.argv[1] if len(sys.argv) > 1 else ""
    if escolha not in COMANDOS:
        print(f"uso: status_fila.py [{' | '.join(COMANDOS)}]", file=sys.stderr)
        raise SystemExit(2)
    print(COMANDOS[escolha]())
