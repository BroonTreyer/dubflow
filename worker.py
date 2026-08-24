"""Worker: consome a fila de episodios e a fila de publicacoes.

Roda em processo separado da UI porque transcricao e render de video seguram a
CPU/GPU por minutos — dentro do servidor web isso travaria o painel.

    py -m worker
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db, publishers
from app.config import configure_logging
from app.pipeline import runner

# Titulos de video vem em qualquer alfabeto. Com o log redirecionado para arquivo,
# o Windows usa cp1252 e um titulo em turco/japones derruba o worker no meio do
# processamento — falha no logging, nao no pipeline.
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

configure_logging("worker")
log = logging.getLogger("worker")

IDLE_SECONDS = 5


def run_episode_queue() -> bool:
    """Processa um episodio, se houver. Devolve True se fez trabalho."""
    episode = db.claim_next_queued()
    if episode is None:
        return False

    log.info("processando episodio %s: %s", episode["id"], episode["source_url"])
    try:
        runner.process_episode(episode["id"])
        log.info("episodio %s concluido", episode["id"])
    except Exception as exc:  # noqa: BLE001 — o runner ja registrou; o worker segue vivo
        log.error("episodio %s falhou: %s", episode["id"], exc)
    return True


def run_action_queue() -> bool:
    """Executa acoes pedidas pelo painel (queimar legenda, refazer cortes)."""
    pendente = db.claim_next_action()
    if pendente is None:
        return False

    episode, action = pendente
    log.info("executando '%s' no episodio %s", action, episode["id"])
    runner.run_action(episode["id"], action)
    return True


def run_publish_queue() -> bool:
    """Publica um post pendente por vez, para nao estourar rate limit."""
    posts = db.pending_posts()
    if not posts:
        return False

    post = posts[0]
    orientation = post.get("orientation") or "vertical"
    # Horizontal usa a versao 16:9 do corte; vertical usa o 9:16 padrao.
    clip_path = post.get("clip_path_wide") if orientation == "horizontal" else post.get("clip_path")
    attempts = (post.get("attempts") or 0) + 1

    if not clip_path or not Path(clip_path).exists():
        faltando = "corte horizontal (16:9)" if orientation == "horizontal" else "corte"
        db.update_post(post["id"], status="failed", attempts=attempts,
                       error=f"arquivo do {faltando} nao encontrado")
        return True

    db.update_post(post["id"], status="publishing", attempts=attempts)
    caption = post.get("clip_caption") or ""
    title = post.get("clip_title")
    thumb = post.get("clip_thumb")
    thumb_path = Path(thumb) if thumb else None
    log.info("publicando post %s em %s/%s (tentativa %d)",
             post["id"], post["platform"], orientation, attempts)

    result = publishers.publish(post["platform"], Path(clip_path), caption, title, thumb_path)
    if result.ok:
        db.update_post(
            post["id"],
            status="published",
            remote_id=result.remote_id,
            permalink=result.permalink,
            posted_at=db.now(),
            error=None,
        )
        log.info("post %s publicado (%s)", post["id"], result.remote_id)
        return True

    if attempts < db.MAX_PUBLISH_ATTEMPTS:
        # Backoff exponencial via `scheduled_at`: a falha volta para a fila em vez
        # de morrer, e uma instabilidade de rede deixa de queimar o post.
        delay = 2 ** attempts * 60
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(
            timespec="seconds"
        )
        db.update_post(post["id"], status="pending", scheduled_at=retry_at,
                       error=result.error)
        log.warning("post %s falhou (%s); nova tentativa em %ds", post["id"], result.error, delay)
    else:
        db.update_post(post["id"], status="failed", error=result.error)
        log.error("post %s esgotou as tentativas: %s", post["id"], result.error)
    return True


def main() -> None:
    db.init_db()

    # No boot nada esta em execucao por definicao: episodios em estado
    # intermediario e posts em 'publishing' sao restos de um worker interrompido.
    # Sem isto, um reinicio no meio de um job deixa o episodio orfao para sempre.
    stuck = db.recover_stuck_episodes()
    if stuck:
        log.info("episodio(s) %s devolvido(s) para a fila apos reinicio", stuck)

    recovered = db.recover_stuck_posts()
    if recovered:
        log.info("%d publicacao(oes) presa(s) devolvida(s) para a fila", recovered)

    log.info("worker iniciado — aguardando fila")
    while True:
        try:
            # As duas filas rodam a cada volta. Com `A or B`, uma fila de episodios
            # sempre cheia faria com que nenhum corte fosse publicado nunca.
            published = run_publish_queue()
            acted = run_action_queue()
            processed = run_episode_queue()
            did_work = published or acted or processed
        except KeyboardInterrupt:
            log.info("encerrando")
            return
        except Exception as exc:  # noqa: BLE001 — nada derruba o loop
            log.exception("erro inesperado no loop: %s", exc)
            did_work = False

        if not did_work:
            time.sleep(IDLE_SECONDS)


if __name__ == "__main__":
    main()
