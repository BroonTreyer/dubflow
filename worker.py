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

from app import db, publishers, sales
from app.config import configure_logging
from app.pipeline import archive, runner

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
    platform = post["platform"]
    # Titulo otimizado para SEO (cai no titulo interno se faltar).
    title = post.get("clip_yt_title") or post.get("clip_title")
    # YouTube usa a descricao otimizada para busca; as outras redes usam a legenda
    # social (gancho + hashtags), que e o formato certo para elas.
    if platform == "youtube" and post.get("clip_yt_description"):
        caption = post["clip_yt_description"]
    else:
        caption = post.get("clip_caption") or ""
    thumb = post.get("clip_thumb")
    thumb_path = Path(thumb) if thumb else None
    log.info("publicando post %s em %s/%s (tentativa %d)",
             post["id"], platform, orientation, attempts)

    result = publishers.publish(platform, Path(clip_path), caption, title, thumb_path)
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


def run_delivery_queue() -> bool:
    """Entrega os pedidos ja pagos (confirmados no painel). Devolve True se agiu.

    Roda fora da requisicao web porque enviar o video ao comprador pode demorar.
    Avulso: manda o episodio. Assinatura: avisa que o acesso esta ativo. Uma falha
    de rede deixa o pedido em 'paid' para a proxima volta tentar de novo.
    """
    pagos = [o for o in db.list_orders(status="paid")]
    if not pagos:
        return False
    order = pagos[-1]  # o mais antigo (list_orders vem do mais novo para o mais velho)

    if order["kind"] == "subscription":
        expira = db.get_subscription_expiry(order["buyer_tg_id"]) or "?"
        publishers.telegram.notify(
            order["buyer_tg_id"],
            f"Pagamento confirmado! Sua assinatura esta ativa ate {expira[:10]}. "
            "Use /catalogo para pedir os episodios.",
        )
        db.update_order(order["id"], status="delivered")
        return True

    meta = archive.find(order["episode_id"])
    if meta is None:
        log.error("pedido %s: episodio %s nao encontrado no acervo", order["id"], order["episode_id"])
        db.update_order(order["id"], status="canceled")
        return True

    log.info("entregando pedido %s (episodio %s) para %s",
             order["id"], order["episode_id"], order["buyer_tg_id"])
    result = publishers.telegram.deliver_episode(meta, order["buyer_tg_id"])
    if result.ok:
        sales.mark_delivered(order["id"])
        return True

    # Falhou: conta a tentativa. Ate o teto, continua 'paid' e a proxima volta tenta
    # de novo; passando do teto, vira 'failed' para nao travar a fila para sempre.
    attempts = (order.get("attempts") or 0) + 1
    if attempts >= db.MAX_DELIVERY_ATTEMPTS:
        db.update_order(order["id"], status="failed", attempts=attempts)
        log.error("pedido %s falhou %d vezes; marcado 'failed': %s",
                  order["id"], attempts, result.error)
    else:
        db.update_order(order["id"], attempts=attempts)
        log.warning("entrega do pedido %s falhou (tentativa %d): %s",
                    order["id"], attempts, result.error)
    return True


_last_stats_refresh: float | None = None
STATS_INTERVAL = 1800  # atualiza as metricas no maximo a cada 30 min


def run_stats_refresh() -> bool:
    """De tempos em tempos, puxa views/curtidas das publicacoes para o painel."""
    global _last_stats_refresh
    agora = time.monotonic()
    if _last_stats_refresh is not None and agora - _last_stats_refresh < STATS_INTERVAL:
        return False
    _last_stats_refresh = agora

    posts = db.posts_needing_stats(limit=15)
    if not posts:
        return False
    for post in posts:
        try:
            data = publishers.stats_for(post["platform"], post["remote_id"])
        except Exception as exc:  # noqa: BLE001 — metrica e extra; nunca derruba o loop
            log.warning("stats do post %s falharam: %s", post["id"], exc)
            continue
        if data:
            db.update_post(post["id"], views=data.get("views"), likes=data.get("likes"),
                           comments=data.get("comments"), stats_at=db.now())
    log.info("metricas atualizadas para %d publicacao(oes)", len(posts))
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
            delivered = run_delivery_queue()
            stats = run_stats_refresh()
            acted = run_action_queue()
            processed = run_episode_queue()
            did_work = published or delivered or stats or acted or processed
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
