"""Diagnostico rapido: posts que falharam ou estao presos, com o motivo e o canal.

Roda na maquina onde o worker publica (o render), lendo o banco local dela.

    .venv\\Scripts\\python.exe -m scripts.diag_posts
"""

from __future__ import annotations

from app import db


def main() -> None:
    db.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT p.id, p.platform, p.status, p.attempts, p.error,"
            "       COALESCE(ch.name, 'global') AS canal"
            "  FROM posts p LEFT JOIN channels ch ON ch.id = p.channel_id"
            " WHERE p.status IN ('failed', 'pending')"
            " ORDER BY p.status DESC, p.id DESC LIMIT 40"
        ).fetchall()

    if not rows:
        print("Nenhum post falho ou pendente. Tudo publicado ou fila vazia.")
        return

    print(f"{'canal':<24}{'plataforma':<12}{'status':<9}{'tent':<5}motivo")
    print("-" * 90)
    for r in rows:
        print(f"{(r['canal'] or '')[:24]:<24}{r['platform']:<12}{r['status']:<9}"
              f"{r['attempts']:<5}{r['error'] or '(sem mensagem)'}")


if __name__ == "__main__":
    main()
