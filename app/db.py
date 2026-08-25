"""Camada SQLite: episodios, cortes e publicacoes.

Cada episodio carrega `license_status`, que controla o que pode ir para o
catalogo pago do Telegram (ver publishers/telegram.py).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.config import settings

# Estados do pipeline, em ordem.
STATES = [
    "queued",
    "downloading",
    "transcribing",
    "translating",
    "subtitling",
    "clipping",
    "archiving",
    "done",
]

LICENSE_STATES = ("unknown", "licensed", "owned", "public_domain")

# Colunas que os helpers de update podem tocar. As instrucoes UPDATE interpolam o
# nome da coluna (bind parameters so valem para valores), entao a allowlist e o
# que impede uma chave inesperada de virar SQL.
EPISODE_COLUMNS = {
    "source_url", "video_id", "title", "channel", "duration", "lang_src", "lang_dst",
    "license_status", "status", "progress", "error", "paths", "meta", "updated_at",
    "pending_action",
}

# Acoes pedidas pelo painel sobre um episodio ja concluido, executadas pelo
# worker: queimar a legenda no video inteiro, ou refazer os cortes (util depois
# de mudar o estilo da legenda).
ACTIONS = ("burn", "rerender_clips")
CLIP_COLUMNS = {"idx", "start", "end", "title", "hook", "caption", "yt_title",
                "yt_description", "score", "path", "path_wide", "thumb_path",
                "thumb_vertical_path", "status"}
POST_COLUMNS = {"platform", "orientation", "status", "remote_id", "permalink", "error",
                "scheduled_at", "posted_at", "attempts",
                "views", "likes", "comments", "stats_at"}


def _guard(fields: dict[str, Any], allowed: set[str], table: str) -> None:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"coluna(s) invalida(s) para {table}: {sorted(unknown)}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url      TEXT NOT NULL,
    video_id        TEXT,
    title           TEXT,
    channel         TEXT,
    duration        REAL,
    lang_src        TEXT,
    lang_dst        TEXT,
    license_status  TEXT NOT NULL DEFAULT 'unknown',
    status          TEXT NOT NULL DEFAULT 'queued',
    progress        REAL NOT NULL DEFAULT 0,
    error           TEXT,
    paths           TEXT NOT NULL DEFAULT '{}',
    meta            TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id   INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    start        REAL NOT NULL,
    end          REAL NOT NULL,
    title          TEXT,
    hook           TEXT,
    caption        TEXT,
    yt_title       TEXT,
    yt_description TEXT,
    score          REAL,
    path           TEXT,
    path_wide      TEXT,
    thumb_path     TEXT,
    thumb_vertical_path TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id      INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    platform     TEXT NOT NULL,
    orientation  TEXT NOT NULL DEFAULT 'vertical',
    status       TEXT NOT NULL DEFAULT 'pending',
    remote_id    TEXT,
    permalink    TEXT,
    error        TEXT,
    scheduled_at TEXT,
    posted_at    TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    views        INTEGER,
    likes        INTEGER,
    comments     INTEGER,
    stats_at     TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_tg_id   TEXT NOT NULL,        -- id do chat/usuario no Telegram
    buyer_name    TEXT,
    kind          TEXT NOT NULL,        -- 'episode' | 'subscription'
    episode_id    INTEGER,              -- nulo para assinatura
    amount        REAL,                 -- valor cobrado em BRL (informativo)
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|paid|delivered|canceled|failed
    attempts      INTEGER NOT NULL DEFAULT 0,       -- tentativas de entrega
    created_at    TEXT NOT NULL,
    paid_at       TEXT
);

CREATE TABLE IF NOT EXISTS subscriptions (
    buyer_tg_id   TEXT PRIMARY KEY,
    expires_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clips_episode ON clips(episode_id);
CREATE INDEX IF NOT EXISTS idx_posts_clip ON posts(clip_id);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Migracao para bancos criados antes da coluna de tentativas.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
        if "attempts" not in columns:
            conn.execute("ALTER TABLE posts ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        for col in ("views", "likes", "comments"):
            if col not in columns:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {col} INTEGER")
        if "stats_at" not in columns:
            conn.execute("ALTER TABLE posts ADD COLUMN stats_at TEXT")

        ep_columns = {r["name"] for r in conn.execute("PRAGMA table_info(episodes)")}
        if "pending_action" not in ep_columns:
            conn.execute("ALTER TABLE episodes ADD COLUMN pending_action TEXT")

        # Migracoes das colunas de corte horizontal, thumbnail e orientacao do post.
        clip_columns = {r["name"] for r in conn.execute("PRAGMA table_info(clips)")}
        if "path_wide" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN path_wide TEXT")
        if "thumb_path" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN thumb_path TEXT")
        if "thumb_vertical_path" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN thumb_vertical_path TEXT")
        if "yt_title" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN yt_title TEXT")
        if "yt_description" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN yt_description TEXT")

        post_columns = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
        if "orientation" not in post_columns:
            conn.execute(
                "ALTER TABLE posts ADD COLUMN orientation TEXT NOT NULL DEFAULT 'vertical'"
            )

        order_columns = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        if "attempts" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")


def request_action(episode_id: int, action: str) -> None:
    if action not in ACTIONS:
        raise ValueError(f"acao invalida: {action}")
    update_episode(episode_id, pending_action=action)


def claim_next_action() -> tuple[dict[str, Any], str] | None:
    """Pega a proxima acao pendente, liberando a coluna na mesma transacao."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM episodes WHERE pending_action IS NOT NULL ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE episodes SET pending_action = NULL, updated_at = ? WHERE id = ?",
            (now(), row["id"]),
        )
    return _episode_row(row), row["pending_action"]


def recover_stuck_episodes() -> list[int]:
    """Devolve a fila os episodios que ficaram parados num estado de execucao.

    Se o worker cai (ou e reiniciado) no meio de um episodio, ele fica em
    'downloading'/'transcribing'/... e nunca mais e pego, porque o claim so
    busca 'queued'. Como este projeto roda um worker por vez, no boot dele nada
    esta em execucao por definicao — entao todo estado intermediario e resto de
    uma execucao interrompida.

    Se um dia forem varios workers, isto precisa virar lease com heartbeat: um
    worker novo nao poderia mais assumir que os outros estao parados.
    """
    in_flight = [s for s in STATES if s not in ("queued", "done")]
    placeholders = ", ".join("?" for _ in in_flight)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM episodes WHERE status IN ({placeholders})", in_flight
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if ids:
            conn.execute(
                f"UPDATE episodes SET status = 'queued', progress = 0, updated_at = ?"
                f" WHERE status IN ({placeholders})",
                (now(), *in_flight),
            )
    return ids


def recover_stuck_posts() -> int:
    """Devolve a pendente os posts que ficaram presos em 'publishing'.

    Se o worker morre no meio de um upload, o post fica travado nesse estado para
    sempre. Chamado no boot do worker, quando por definicao nada esta publicando.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE posts SET status = 'pending' WHERE status = 'publishing'"
        )
        return cur.rowcount


# --------------------------------------------------------------------------- episodes


def find_active_by_url(source_url: str) -> dict[str, Any] | None:
    """Episodio ja processado ou na fila para a mesma URL.

    Evita pagar a traducao duas vezes pelo mesmo video — o erro mais caro que um
    duplo clique consegue causar aqui.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM episodes WHERE source_url = ? AND status != 'failed'"
            " ORDER BY id DESC LIMIT 1",
            (source_url,),
        ).fetchone()
    return _episode_row(row) if row else None


def create_episode(source_url: str, license_status: str = "unknown") -> int:
    if license_status not in LICENSE_STATES:
        raise ValueError(f"license_status invalido: {license_status}")
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO episodes (source_url, license_status, lang_dst, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (source_url, license_status, settings.target_lang, ts, ts),
        )
        return int(cur.lastrowid)


def update_episode(episode_id: int, **fields: Any) -> None:
    if not fields:
        return
    _guard(fields, EPISODE_COLUMNS, "episodes")
    for key in ("paths", "meta"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    fields["updated_at"] = now()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE episodes SET {assignments} WHERE id = ?",
            (*fields.values(), episode_id),
        )


def get_episode(episode_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return _episode_row(row) if row else None


def list_episodes(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_episode_row(r) for r in rows]


def claim_next_queued() -> dict[str, Any] | None:
    """Marca o episodio mais antigo da fila como `downloading` e o devolve.

    O UPDATE condicionado a status='queued' garante que dois workers nunca
    peguem o mesmo episodio.
    """
    with connect() as conn:
        # BEGIN IMMEDIATE pega o lock de escrita antes do SELECT. Sem isso, dois
        # workers leem o mesmo id e o segundo recebe SQLITE_BUSY_SNAPSHOT no
        # UPDATE — que o SQLite nao repete, entao viraria excecao em vez de fila.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM episodes WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE episodes SET status = 'downloading', updated_at = ?"
            " WHERE id = ? AND status = 'queued'",
            (now(), row["id"]),
        )
        if cur.rowcount == 0:
            return None
        claimed = conn.execute("SELECT * FROM episodes WHERE id = ?", (row["id"],)).fetchone()
    return _episode_row(claimed)


def _episode_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("paths", "meta"):
        try:
            data[key] = json.loads(data.get(key) or "{}")
        except json.JSONDecodeError:
            data[key] = {}
    return data


# --------------------------------------------------------------------------- clips


def replace_clips(episode_id: int, clips: list[dict[str, Any]]) -> list[int]:
    ts = now()
    with connect() as conn:
        conn.execute("DELETE FROM clips WHERE episode_id = ?", (episode_id,))
        ids = []
        for idx, clip in enumerate(clips):
            cur = conn.execute(
                "INSERT INTO clips (episode_id, idx, start, end, title, hook, caption,"
                " yt_title, yt_description, score, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    idx,
                    clip["start"],
                    clip["end"],
                    clip.get("title"),
                    clip.get("hook"),
                    clip.get("caption"),
                    clip.get("yt_title"),
                    clip.get("yt_description"),
                    clip.get("score"),
                    ts,
                ),
            )
            ids.append(int(cur.lastrowid))
    return ids


def update_clip(clip_id: int, **fields: Any) -> None:
    if not fields:
        return
    _guard(fields, CLIP_COLUMNS, "clips")
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE clips SET {assignments} WHERE id = ?", (*fields.values(), clip_id))


def list_clips(episode_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM clips WHERE episode_id = ? ORDER BY idx", (episode_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_clip(clip_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- posts


def create_post(clip_id: int, platform: str, scheduled_at: str | None = None,
                orientation: str = "vertical") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO posts (clip_id, platform, orientation, scheduled_at, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (clip_id, platform, orientation, scheduled_at, now()),
        )
        return int(cur.lastrowid)


def update_post(post_id: int, **fields: Any) -> None:
    if not fields:
        return
    _guard(fields, POST_COLUMNS, "posts")
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE posts SET {assignments} WHERE id = ?", (*fields.values(), post_id))


MAX_PUBLISH_ATTEMPTS = 4


def pending_posts() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.*, c.path AS clip_path, c.path_wide AS clip_path_wide,"
            " c.thumb_path AS clip_thumb,"
            " c.thumb_vertical_path AS clip_thumb_vertical, c.caption AS clip_caption,"
            " c.title AS clip_title, c.yt_title AS clip_yt_title,"
            " c.yt_description AS clip_yt_description, c.episode_id"
            " FROM posts p JOIN clips c ON c.id = p.clip_id"
            " WHERE p.status = 'pending'"
            "   AND p.attempts < ?"
            "   AND (p.scheduled_at IS NULL OR p.scheduled_at <= ?)"
            " ORDER BY p.id",
            (MAX_PUBLISH_ATTEMPTS, now()),
        ).fetchall()
    return [dict(r) for r in rows]


def list_posts(episode_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.*, c.idx AS clip_idx FROM posts p JOIN clips c ON c.id = p.clip_id"
            " WHERE c.episode_id = ? ORDER BY p.id",
            (episode_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def posts_needing_stats(limit: int = 10) -> list[dict[str, Any]]:
    """Publicacoes com id remoto, priorizando as nunca atualizadas / mais antigas."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, platform, remote_id FROM posts"
            " WHERE status = 'published' AND remote_id IS NOT NULL"
            " ORDER BY (stats_at IS NULL) DESC, stats_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def analytics_posts(limit: int = 200) -> list[dict[str, Any]]:
    """Publicacoes com metricas, do maior numero de views para o menor."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.*, c.title AS clip_title, c.yt_title AS clip_yt_title,"
            " c.episode_id, e.title AS episode_title"
            " FROM posts p JOIN clips c ON c.id = p.clip_id"
            " JOIN episodes e ON e.id = c.episode_id"
            " WHERE p.status = 'published'"
            " ORDER BY COALESCE(p.views, -1) DESC, p.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- orders (vendas)

ORDER_STATES = ("pending", "paid", "delivered", "canceled", "failed")
ORDER_COLUMNS = {"buyer_name", "kind", "episode_id", "amount", "status", "attempts", "paid_at"}
MAX_DELIVERY_ATTEMPTS = 4


def create_order(buyer_tg_id: str, kind: str, buyer_name: str | None = None,
                 episode_id: int | None = None, amount: float | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO orders (buyer_tg_id, buyer_name, kind, episode_id, amount, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (str(buyer_tg_id), buyer_name, kind, episode_id, amount, now()),
        )
        return int(cur.lastrowid)


def get_order(order_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return dict(row) if row else None


def update_order(order_id: int, **fields: Any) -> None:
    if not fields:
        return
    _guard(fields, ORDER_COLUMNS, "orders")
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE orders SET {assignments} WHERE id = ?", (*fields.values(), order_id))


def list_orders(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def orders_delivered_episode(buyer_tg_id: str, episode_id: int) -> bool:
    """Ja pagou (ou recebeu) este episodio avulso?"""
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM orders WHERE buyer_tg_id = ? AND kind = 'episode'"
            "   AND episode_id = ? AND status IN ('paid', 'delivered') LIMIT 1",
            (str(buyer_tg_id), episode_id),
        ).fetchone()
    return row is not None


# ----------------------------------------------------------------- subscriptions (assinaturas)


def get_subscription_expiry(buyer_tg_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE buyer_tg_id = ?", (str(buyer_tg_id),)
        ).fetchone()
    return row["expires_at"] if row else None


def set_subscription_expiry(buyer_tg_id: str, expires_at: str) -> None:
    ts = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO subscriptions (buyer_tg_id, expires_at, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(buyer_tg_id) DO UPDATE SET expires_at = excluded.expires_at,"
            "   updated_at = excluded.updated_at",
            (str(buyer_tg_id), expires_at, ts),
        )
