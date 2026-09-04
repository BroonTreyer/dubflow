"""Worker: consome a fila de episodios e a fila de publicacoes.

Roda em processo separado da UI porque transcricao e render de video seguram a
CPU/GPU por minutos — dentro do servidor web isso travaria o painel.

    py -m worker
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import attribution, db, pix, publishers, sales
from app.config import configure_logging, settings
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
    except runner.Paused:
        # Parada pedida, nao falha: o episodio ficou em 'paused' com os artefatos
        # gravados e volta do mesmo ponto quando alguem retomar.
        log.info("episodio %s pausado", episode["id"])
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

    # Credito da fonte no fim da descricao, em TODA plataforma: link do episodio
    # completo e @ do canal original. E o que apresenta o video como corte em vez
    # de reupload — a diferenca que pesa quando alguem denuncia.
    caption = attribution.apply(caption, {
        "source_url": post.get("ep_source_url"),
        "channel": post.get("ep_channel"),
        "meta": json.loads(post.get("ep_meta") or "{}"),
    })
    # A capa acompanha a orientacao do post: um Short publicado com a capa 16:9
    # aparece com barras. Cai na 16:9 se a vertical nao existir (corte antigo).
    if orientation == "horizontal":
        thumb = post.get("clip_thumb")
    else:
        thumb = post.get("clip_thumb_vertical") or post.get("clip_thumb")
    thumb_path = Path(thumb) if thumb else None
    channel_id = post.get("channel_id")
    log.info("publicando post %s em %s/%s%s (tentativa %d)",
             post["id"], platform, orientation,
             f" [canal {channel_id}]" if channel_id else "", attempts)

    result = publishers.publish(platform, Path(clip_path), caption, title, thumb_path, channel_id)
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

    if order["kind"] in ("subscription", "lifetime"):
        if order["kind"] == "lifetime":
            validade = "Seu acesso é vitalício — nunca expira."
        else:
            expira = db.get_subscription_expiry(order["buyer_tg_id"]) or "?"
            validade = f"Seu acesso vale até {expira[:10]}."
        invite, err = publishers.telegram.create_vip_invite()
        if not invite:
            # Ainda nao deu para gerar o convite (VIP nao configurado ou erro de
            # rede). Conta a tentativa e tenta de novo na proxima volta; passando do
            # teto, vira 'failed' para nao travar a fila para sempre.
            attempts = (order.get("attempts") or 0) + 1
            if attempts >= db.MAX_DELIVERY_ATTEMPTS:
                db.update_order(order["id"], status="failed", attempts=attempts)
                log.error("assinatura %s: sem link VIP apos %d tentativas: %s",
                          order["id"], attempts, err)
            else:
                db.update_order(order["id"], attempts=attempts)
                log.warning("assinatura %s: convite VIP falhou (tentativa %d): %s",
                            order["id"], attempts, err)
            return True
        publishers.telegram.notify(
            order["buyer_tg_id"],
            "Pagamento confirmado. Bem-vindo ao VIP.\n\n"
            f"{validade} Entre no canal com todos os episódios pelo seu link "
            "exclusivo (uso único, não compartilhe):\n\n"
            f"{invite}\n\n"
            "Bom dorama.",
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


_last_cleanup: float | None = None


def run_cleanup_published() -> bool:
    """Apaga o ARQUIVO do corte que ja foi publicado em todos os seus destinos.

    Numa fabrica de 1 ano de fila, corte publicado e peso morto: ele ja esta no
    YouTube. So o arquivo sai — a linha no banco fica, entao historico, permalink
    e metricas continuam no painel.

    O que NAO e tocado:

    - o ACERVO (`data/archive`), que e o produto vendido no Telegram;
    - a capa, que e pequena e ainda aparece no painel;
    - corte com qualquer publicacao pendente (pode ir para mais de um canal).

    A carencia (CLEANUP_AFTER_HOURS) da tempo de republicar se o upload der
    problema depois de marcado como publicado.
    """
    global _last_cleanup
    if not settings.cleanup_published:
        return False

    agora = time.monotonic()
    if _last_cleanup is not None and agora - _last_cleanup < 3600:
        return False
    _last_cleanup = agora

    candidatos = db.clips_fully_published(settings.cleanup_after_hours)
    if not candidatos:
        return False

    liberado = 0
    apagados = 0
    for clip in candidatos:
        # path (9:16) e path_wide (16:9) sao os pesados; a capa fica.
        for campo in ("path", "path_wide"):
            alvo = clip.get(campo)
            if not alvo:
                continue
            caminho = Path(alvo)
            try:
                if not caminho.is_file():
                    continue
                # Nunca apagar fora de data/: um path adulterado nao vira estrago.
                if not caminho.resolve().is_relative_to(settings.data_dir.resolve()):
                    log.warning("cleanup: %s esta fora de data/, ignorado", caminho)
                    continue
                liberado += caminho.stat().st_size
                caminho.unlink()
                apagados += 1
            except OSError as exc:
                log.warning("cleanup: nao consegui apagar %s (%s)", caminho, exc)

    if apagados:
        log.info("cleanup: %d arquivo(s) de corte publicado removidos, %.1f GB liberados",
                 apagados, liberado / 1e9)
    return bool(apagados)


_last_autofill: float | None = None


def run_autofill() -> bool:
    """Abastece a fila sozinho ate cada canal ter QUEUE_TARGET_DAYS agendados.

    Nao e cota diaria: e horizonte. Quando um canal alcanca o alvo, ele para de
    puxar; quando voce pluga uma conta nova (horizonte zero), o abastecimento
    volta a rodar sozinho para ela. E o que faz o fluxo continuar indefinidamente
    sem ninguem apertar botao.

    Duas travas de sobrevivencia, porque o alvo de 1 ano pede centenas de
    episodios: teto por rodada e piso de disco livre.
    """
    global _last_autofill
    if not settings.queue_autofill:
        return False

    agora = time.monotonic()
    intervalo = settings.queue_scan_interval_hours * 3600
    if _last_autofill is not None and agora - _last_autofill < intervalo:
        return False
    _last_autofill = agora

    livre_gb = shutil.disk_usage(str(settings.data_dir)).free / 1e9
    if livre_gb < settings.queue_min_free_gb:
        log.warning("autofill pausado: %.0f GB livres, piso e %d GB",
                    livre_gb, settings.queue_min_free_gb)
        return False

    # Nao empilha trabalho: se ainda ha episodio esperando, nao adianta puxar mais.
    with db.connect() as conn:
        na_fila = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE status = 'queued'").fetchone()[0]
    if na_fila >= settings.queue_max_per_run:
        return False

    try:
        from scripts import fill_queue
        faltam, _ = fill_queue.episodios_faltando(settings.queue_target_days)
        if faltam <= 0:
            return False
        fill_queue.rodar(settings.queue_target_days,
                         min(settings.queue_max_per_run - na_fila, faltam),
                         aplicar=True)
        return True
    except Exception as exc:  # noqa: BLE001 — abastecer e extra; nunca derruba o loop
        log.warning("autofill falhou: %s", exc)
        return False


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
            data = publishers.stats_for(post["platform"], post["remote_id"], post.get("channel_id"))
        except Exception as exc:  # noqa: BLE001 — metrica e extra; nunca derruba o loop
            log.warning("stats do post %s falharam: %s", post["id"], exc)
            continue
        if data:
            db.update_post(post["id"], views=data.get("views"), likes=data.get("likes"),
                           comments=data.get("comments"), stats_at=db.now())
    log.info("metricas atualizadas para %d publicacao(oes)", len(posts))
    return True


_vip_oversize_warned: set[int] = set()


def _max_upload_bytes() -> int:
    """Limite de upload conforme a base da Bot API: nuvem = 50 MB, local = 2 GB."""
    base = settings.telegram_api_base or ""
    return 50 * 1024 * 1024 if "api.telegram.org" in base else 2000 * 1024 * 1024


def run_vip_publish() -> bool:
    """Posta o VIDEO COMPLETO dos episodios vendaveis no canal VIP (um por volta).

    Separacao de conteudo: os cortes vao pro canal isca (distribuicao); aqui o
    episodio inteiro vai pro VIP, so para quem assina. Dedup por vip_posted_at.
    Um arquivo acima do limite da Bot API atual nao e enviado (evita torrar banda);
    o aviso pede o servidor Bot API local. Devolve True se agiu.
    """
    if not publishers.telegram.vip_configured():
        return False
    pendentes = db.episodes_pending_vip()
    if not pendentes:
        return False
    ep = pendentes[0]
    meta = archive.find(ep["id"])
    video = (meta or {}).get("arquivos", {}).get("episodio") if meta else None
    if not video or not Path(video).exists():
        log.warning("VIP: episodio %s sem video completo no acervo; marcando como visto", ep["id"])
        db.mark_vip_posted(ep["id"])  # sem arquivo nao ha o que postar; nao reprocessa
        return True

    size = Path(video).stat().st_size
    if size > _max_upload_bytes():
        if ep["id"] not in _vip_oversize_warned:
            log.warning("VIP: episodio %s tem %.0f MB, acima do limite da Bot API atual — "
                        "suba um servidor Bot API local (TELEGRAM_API_BASE) para ate 2 GB",
                        ep["id"], size / 1024 / 1024)
            _vip_oversize_warned.add(ep["id"])
        return False  # nao tenta subir; espera o Bot API local

    caption = meta.get("titulo") or f"Episodio {ep['id']}"
    if meta.get("canal"):
        caption = f"{caption}\n{meta['canal']}"
    log.info("VIP: publicando episodio %s (%.0f MB) no canal VIP", ep["id"], size / 1024 / 1024)
    result = publishers.telegram.publish_vip_episode(Path(video), caption)
    if result.ok:
        db.mark_vip_posted(ep["id"])
        log.info("VIP: episodio %s publicado no canal VIP", ep["id"])
        return True
    log.warning("VIP: falha ao publicar episodio %s (tentara de novo): %s",
                ep["id"], result.error)
    return False


def run_pix_poll() -> bool:
    """Confirma sozinho os pedidos pagos: consulta o gateway (PushinPay) sobre cada
    pedido pendente que tem cobranca e, quando 'paid', marca pago (o que estende a
    assinatura) — a fila de entrega entao manda o link do VIP. Devolve True se agiu.

    E o coracao do Pix automatico: substitui a confirmacao manual no painel.
    """
    if not pix.configured():
        return False
    pendentes = [o for o in db.list_orders(status="pending") if o.get("pix_txid")]
    if not pendentes:
        return False
    agiu = False
    for order in pendentes:
        status, err = pix.charge_status(order["pix_txid"])
        if err:
            log.warning("pedido %s: consulta Pix falhou: %s", order["id"], err)
            continue
        if status == pix.PAID:
            sales.confirm_payment(order["id"])  # vira 'paid' + estende assinatura
            log.info("pedido %s pago (Pix automatico) — indo para entrega", order["id"])
            agiu = True
        elif status in pix.DEAD:
            db.update_order(order["id"], status="canceled")
            log.info("pedido %s %s no gateway — cancelado", order["id"], status)
            agiu = True
        # 'created' = ainda nao pago; fica na fila para a proxima volta
    return agiu


def run_vip_expiry() -> bool:
    """Remove do canal VIP os assinantes cuja assinatura venceu. Devolve True se agiu.

    So mexe em quem venceu E ainda nao foi removido (list_expired_vip_members).
    Marca vip_removed_at apenas no sucesso: se a remocao falhar (ex.: bot sem
    permissao), tenta de novo na proxima volta em vez de deixar o assinante
    vencido dentro do VIP.
    """
    if not publishers.telegram.vip_configured():
        return False
    vencidos = db.list_expired_vip_members()
    if not vencidos:
        return False
    for buyer in vencidos:
        result = publishers.telegram.remove_from_vip(buyer)
        if result.ok:
            db.mark_vip_removed(buyer)
            log.info("assinante %s removido do VIP (assinatura vencida)", buyer)
            publishers.telegram.notify(
                buyer,
                "Sua assinatura venceu e o acesso ao VIP foi encerrado.\n\n"
                "Para voltar, é só renovar com Seja Prime (ou /assinar) que eu "
                "te coloco de volta na hora.",
            )
        else:
            log.warning("falha ao remover %s do VIP (tentara de novo): %s",
                        buyer, result.error)
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
            paid = run_pix_poll()
            expired = run_vip_expiry()
            vip_ep = run_vip_publish()
            # Por ultimo: so abastece quando o resto ja foi atendido, para nao
            # empilhar download novo sobre uma fila que ainda esta andando.
            abastecido = run_autofill()
            limpou = run_cleanup_published()
            did_work = (published or delivered or stats or acted or processed
                        or paid or expired or vip_ep or abastecido or limpou)
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
