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
    "segment", "license_status", "status", "progress", "error", "paths", "meta",
    "updated_at", "pending_action", "started_at",
}

# Acoes pedidas pelo painel sobre um episodio ja concluido, executadas pelo
# worker: queimar a legenda no video inteiro, ou refazer os cortes (util depois
# de mudar o estilo da legenda).
ACTIONS = ("burn", "rerender_clips", "distribute")
CLIP_COLUMNS = {"idx", "start", "end", "title", "hook", "caption", "yt_title",
                "yt_description", "score", "path", "path_wide", "thumb_path",
                "thumb_vertical_path", "thumb_text", "status"}
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
    -- Segmento (nicho) do conteudo, para rotear os cortes ao canal certo.
    -- Classificado automaticamente; editavel no painel. NULL = ainda nao definido.
    segment         TEXT,
    license_status  TEXT NOT NULL DEFAULT 'unknown',
    status          TEXT NOT NULL DEFAULT 'queued',
    progress        REAL NOT NULL DEFAULT 0,
    error           TEXT,
    paths           TEXT NOT NULL DEFAULT '{}',
    meta            TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    -- Quando o worker PEGOU o episodio (created_at e quando voce colou o link).
    -- E a base do tempo estimado: sem isso, um video que esperou 3h na fila
    -- pareceria estar processando ha 3h.
    started_at      TEXT
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
    thumb_text     TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,          -- rotulo humano ("Financas BR #1")
    platform    TEXT NOT NULL,          -- youtube|instagram|tiktok|telegram
    market      TEXT NOT NULL DEFAULT 'BR',   -- BR|US|... (afeta idioma/RPM alvo)
    niche       TEXT,                   -- segmento que o canal atende (roteia os cortes)
    -- Rotulo do projeto do Google Cloud (YouTube). Canais com o MESMO project
    -- dividem a cota diaria de upload da API. Vazio = projeto proprio (recomendado).
    project     TEXT,
    -- Ritmo do gotejamento: quantos cortes por dia este canal recebe no agendamento
    -- automatico. Conta nova posta pouco; conta aquecida pode subir.
    posts_per_day INTEGER NOT NULL DEFAULT 3,
    status      TEXT NOT NULL DEFAULT 'active',  -- active|paused
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id      INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    -- Conta de destino. NULL = credenciais globais (comportamento anterior a
    -- multi-conta). ON DELETE SET NULL preserva o historico do post se o canal
    -- for removido.
    channel_id   INTEGER REFERENCES channels(id) ON DELETE SET NULL,
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
CREATE INDEX IF NOT EXISTS idx_channels_status ON channels(status);
"""
# idx_posts_channel NAO fica no SCHEMA: em banco antigo a coluna posts.channel_id
# so existe depois do ALTER de migracao, que roda apos o executescript(SCHEMA).
# Criado em init_db logo apos garantir a coluna.

CHANNEL_STATES = ("active", "paused")
CHANNEL_COLUMNS = {"name", "platform", "market", "niche", "status", "posts_per_day", "project"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Estados em que nao ha mais nada a esperar.
TERMINAL = ("done", "failed", "canceled")


def eta_seconds(episode: dict[str, Any]) -> int | None:
    """Quanto falta, em segundos, pelo ritmo observado ate agora.

    Regra de tres simples sobre o progresso: se 30% levou 3 min, os 70% que
    faltam levam ~7 min. E uma estimativa grosseira — as etapas nao custam o
    mesmo (o render dos cortes pesa mais que o download) — entao a UI mostra
    como aproximacao, nunca como promessa.

    None quando nao da para estimar: episodio terminal, ainda na fila, ou
    progresso baixo demais para o ritmo significar alguma coisa.
    """
    if episode.get("status") in TERMINAL:
        return None
    progresso = float(episode.get("progress") or 0)
    if progresso < 0.05 or progresso >= 1:
        return None

    inicio = episode.get("started_at")
    if not inicio:
        return None
    try:
        decorrido = (datetime.now(timezone.utc) - datetime.fromisoformat(inicio)).total_seconds()
    except (TypeError, ValueError):
        return None
    if decorrido <= 0:
        return None

    return int(decorrido / progresso * (1 - progresso))


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
        if "started_at" not in ep_columns:
            conn.execute("ALTER TABLE episodes ADD COLUMN started_at TEXT")
        if "segment" not in ep_columns:
            conn.execute("ALTER TABLE episodes ADD COLUMN segment TEXT")

        # Multi-conta: cadencia de gotejamento por canal (bancos que ja tinham a
        # tabela channels sem a coluna).
        ch_columns = {r["name"] for r in conn.execute("PRAGMA table_info(channels)")}
        if "posts_per_day" not in ch_columns:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN posts_per_day INTEGER NOT NULL DEFAULT 3"
            )
        if "project" not in ch_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN project TEXT")

        # Migracoes das colunas de corte horizontal, thumbnail e orientacao do post.
        clip_columns = {r["name"] for r in conn.execute("PRAGMA table_info(clips)")}
        if "path_wide" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN path_wide TEXT")
        if "thumb_path" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN thumb_path TEXT")
        if "thumb_vertical_path" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN thumb_vertical_path TEXT")
        if "thumb_text" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN thumb_text TEXT")
        if "yt_title" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN yt_title TEXT")
        if "yt_description" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN yt_description TEXT")

        post_columns = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
        if "orientation" not in post_columns:
            conn.execute(
                "ALTER TABLE posts ADD COLUMN orientation TEXT NOT NULL DEFAULT 'vertical'"
            )
        # Multi-conta: bancos antigos ganham channel_id nulavel (NULL = cofre global).
        if "channel_id" not in post_columns:
            conn.execute("ALTER TABLE posts ADD COLUMN channel_id INTEGER REFERENCES channels(id)")
        # Indice criado aqui (nao no SCHEMA) porque a coluna pode ter acabado de nascer.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_channel ON posts(channel_id)")

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


def create_episode(source_url: str, license_status: str = "unknown",
                   lang_dst: str | None = None) -> int:
    if license_status not in LICENSE_STATES:
        raise ValueError(f"license_status invalido: {license_status}")
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO episodes (source_url, license_status, lang_dst, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (source_url, license_status, (lang_dst or settings.target_lang), ts, ts),
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
                orientation: str = "vertical", channel_id: int | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO posts (clip_id, platform, orientation, scheduled_at, channel_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (clip_id, platform, orientation, scheduled_at, channel_id, now()),
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
            " LEFT JOIN channels ch ON ch.id = p.channel_id"
            " WHERE p.status = 'pending'"
            "   AND (p.channel_id IS NULL OR ch.status = 'active')"
            "   AND p.attempts < ?"
            "   AND (p.scheduled_at IS NULL OR p.scheduled_at <= ?)"
            " ORDER BY p.id",
            (MAX_PUBLISH_ATTEMPTS, now()),
        ).fetchall()
    return [dict(r) for r in rows]


def list_posts(episode_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.*, c.idx AS clip_idx, ch.name AS channel_name"
            " FROM posts p JOIN clips c ON c.id = p.clip_id"
            " LEFT JOIN channels ch ON ch.id = p.channel_id"
            " WHERE c.episode_id = ? ORDER BY p.id",
            (episode_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def posts_needing_stats(limit: int = 10) -> list[dict[str, Any]]:
    """Publicacoes com id remoto, priorizando as nunca atualizadas / mais antigas."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, platform, remote_id, channel_id FROM posts"
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


# ------------------------------------------------------------------- dashboard (visao geral)


def dashboard_stats() -> dict[str, int]:
    """Contadores agregados para o painel de visao geral (poucas passadas no banco)."""
    with connect() as conn:
        ep = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM episodes GROUP BY status")}
        po = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM posts GROUP BY status")}
        m = conn.execute(
            "SELECT COALESCE(SUM(views),0) v, COALESCE(SUM(likes),0) l,"
            " COALESCE(SUM(comments),0) c FROM posts WHERE status='published'").fetchone()

        def scalar(sql: str, *params: Any) -> int:
            row = conn.execute(sql, params).fetchone()
            return int(row[0] or 0) if row else 0

        return {
            "episodes_total": sum(ep.values()),
            "episodes_done": ep.get("done", 0),
            "episodes_failed": ep.get("failed", 0),
            "episodes_queued": ep.get("queued", 0),
            "episodes_processing": sum(
                n for s, n in ep.items() if s not in ("done", "failed", "canceled", "queued")),
            "clips_ready": scalar("SELECT COUNT(*) FROM clips WHERE status='ready'"),
            "clips_total": scalar("SELECT COUNT(*) FROM clips"),
            "posts_published": po.get("published", 0),
            "posts_pending": po.get("pending", 0),
            "posts_failed": po.get("failed", 0),
            "posts_scheduled": scalar(
                "SELECT COUNT(*) FROM posts WHERE status='pending' AND scheduled_at IS NOT NULL"),
            "views": int(m["v"]), "likes": int(m["l"]), "comments": int(m["c"]),
            "channels_total": scalar("SELECT COUNT(*) FROM channels"),
            "channels_active": scalar("SELECT COUNT(*) FROM channels WHERE status='active'"),
        }


def scheduled_posts(limit: int = 300) -> list[dict[str, Any]]:
    """Posts pendentes COM horario agendado, do mais proximo ao mais distante.

    Base do calendario do painel. Inclui a thumb e o canal para renderizar o card.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.id, p.platform, p.orientation, p.scheduled_at, p.channel_id,"
            " c.idx AS clip_idx, c.title AS clip_title, c.yt_title AS clip_yt_title,"
            " c.thumb_vertical_path, c.thumb_path, c.episode_id,"
            " ch.name AS channel_name"
            " FROM posts p JOIN clips c ON c.id = p.clip_id"
            " LEFT JOIN channels ch ON ch.id = p.channel_id"
            " WHERE p.status = 'pending' AND p.scheduled_at IS NOT NULL"
            " ORDER BY p.scheduled_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_clips(limit: int = 12) -> list[dict[str, Any]]:
    """Cortes prontos mais recentes, com thumbnail e o episodio de origem."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.idx, c.title, c.yt_title, c.score, c.start, c.end,"
            " c.thumb_vertical_path, c.thumb_path, c.path, c.episode_id,"
            " e.title AS episode_title"
            " FROM clips c JOIN episodes e ON e.id = c.episode_id"
            " WHERE c.status = 'ready' ORDER BY c.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def channel_totals() -> dict[int | None, dict[str, int]]:
    """Por canal (None = cofre global): posts publicados/pendentes e soma de metricas."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT channel_id,"
            " SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) AS published,"
            " SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,"
            " COALESCE(SUM(views),0) AS views, COALESCE(SUM(likes),0) AS likes"
            " FROM posts GROUP BY channel_id"
        ).fetchall()
    return {r["channel_id"]: {
        "published": int(r["published"] or 0), "pending": int(r["pending"] or 0),
        "views": int(r["views"] or 0), "likes": int(r["likes"] or 0),
    } for r in rows}


# ------------------------------------------------------------------- distribuicao automatica


def clips_ready_without_posts(episode_id: int) -> list[dict[str, Any]]:
    """Cortes prontos deste episodio que ainda nao tem NENHUMA publicacao.

    Base do agendamento automatico: garante que rodar a distribuicao de novo nao
    duplica posts — so agenda o que ainda nao foi agendado.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.* FROM clips c LEFT JOIN posts p ON p.clip_id = c.id"
            " WHERE c.episode_id = ? AND c.status = 'ready' AND p.id IS NULL"
            " ORDER BY c.idx",
            (episode_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def channel_scheduling_horizon(channel_id: int) -> str | None:
    """Ultimo horario ja agendado/publicado para este canal (ISO), ou None.

    O gotejamento novo continua DEPOIS deste horario, para nao empilhar posts no
    mesmo instante do que ja estava na fila do canal.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(scheduled_at) AS h FROM posts"
            " WHERE channel_id = ? AND scheduled_at IS NOT NULL"
            "   AND status IN ('pending', 'publishing', 'published')",
            (channel_id,),
        ).fetchone()
    return row["h"] if row and row["h"] else None


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


# --------------------------------------------------------------------------- channels (contas)


def create_channel(name: str, platform: str, market: str = "BR",
                   niche: str | None = None, posts_per_day: int = 3,
                   project: str | None = None) -> int:
    """Registra uma conta de destino (um canal do YouTube, um perfil do TikTok...).

    Cada canal tem seu proprio cofre de credenciais (ver app/credentials.py); e a
    unidade que permite publicar em varias contas a partir do mesmo acervo.
    """
    if platform not in ("youtube", "instagram", "tiktok", "telegram"):
        raise ValueError(f"plataforma invalida: {platform}")
    name = (name or "").strip()
    if not name:
        raise ValueError("o canal precisa de um nome")
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO channels (name, platform, market, niche, posts_per_day,"
            " project, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, platform, (market or "BR").strip() or "BR",
             (niche or "").strip() or None, max(1, int(posts_per_day or 3)),
             (project or "").strip() or None, ts, ts),
        )
        return int(cur.lastrowid)


def get_channel(channel_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return dict(row) if row else None


def list_channels(platform: str | None = None, only_active: bool = False) -> list[dict[str, Any]]:
    clauses, params = [], []
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    if only_active:
        clauses.append("status = 'active'")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM channels{where} ORDER BY platform, name", params
        ).fetchall()
    return [dict(r) for r in rows]


def update_channel(channel_id: int, **fields: Any) -> None:
    if not fields:
        return
    _guard(fields, CHANNEL_COLUMNS, "channels")
    if "status" in fields and fields["status"] not in CHANNEL_STATES:
        raise ValueError(f"status de canal invalido: {fields['status']}")
    fields["updated_at"] = now()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE channels SET {assignments} WHERE id = ?", (*fields.values(), channel_id)
        )


def delete_channel(channel_id: int) -> None:
    """Remove o canal. Os posts ja publicados por ele mantêm o historico
    (channel_id vira NULL via ON DELETE SET NULL)."""
    with connect() as conn:
        conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
