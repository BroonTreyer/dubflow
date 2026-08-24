"""Bot de vendas do Telegram: escuta os clientes e cria pedidos.

Roda em processo separado (long-polling), do mesmo jeito que o worker. O cliente
usa /catalogo, /comprar <id> ou /assinar; o bot cria um pedido pendente e mostra
sua chave Pix. Voce confirma o pagamento no painel (aba Vendas) e o worker entrega.

    py -m bot
"""

from __future__ import annotations

import logging
import sys
import time

import requests

from app import credentials, db, sales
from app.config import configure_logging, settings
from app.pipeline import archive
from app.publishers import telegram

sys.stderr.reconfigure(encoding="utf-8", errors="replace")
configure_logging("bot")
log = logging.getLogger("bot")

API = "https://api.telegram.org"

WELCOME = (
    "Ola! Aqui voce compra episodios legendados em pt-BR.\n\n"
    "/catalogo — ver os episodios disponiveis\n"
    "/comprar <numero> — comprar um episodio avulso\n"
    "/assinar — assinar e ter acesso a tudo\n"
    "/status — ver sua assinatura"
)


def _preco(v: float) -> str:
    return f"R$ {v:.2f}".replace(".", ",")


def _catalogo() -> str:
    itens = telegram.catalog(only_sellable=True)
    if not itens:
        return "O catalogo ainda esta vazio. Volte em breve."
    linhas = ["Catalogo — use /comprar <numero>:", ""]
    for it in itens:
        linhas.append(f"{it.get('id')} — {it.get('titulo') or 'sem titulo'} "
                      f"({it.get('canal') or '?'})")
    linhas += ["", f"Avulso: {_preco(settings.price_episode)}  |  "
                   f"Assinatura ({settings.subscription_days} dias): {_preco(settings.price_subscription)}"]
    return "\n".join(linhas)


def _handle_text(text: str, chat_id: int, name: str | None) -> str:
    """Trata uma mensagem e devolve a resposta (criando pedido quando for o caso)."""
    low = (text or "").strip().lower()

    if low.startswith("/start"):
        return WELCOME
    if low.startswith("/catalogo"):
        return _catalogo()
    if low.startswith("/assinar"):
        oid = sales.create_subscription_order(str(chat_id), name)
        return (f"Pedido de assinatura #{oid} criado.\n\n"
                f"{sales.pix_instructions(settings.price_subscription)}")
    if low.startswith("/comprar"):
        partes = (text or "").split()
        if len(partes) < 2 or not partes[1].isdigit():
            return "Use assim: /comprar <numero>. Veja os numeros no /catalogo."
        ep = int(partes[1])
        item = archive.find(ep)
        if item is None or item.get("licenca") not in telegram.SELLABLE_LICENSES:
            return "Nao achei esse episodio no catalogo. Veja o /catalogo."
        oid = sales.create_episode_order(str(chat_id), ep, name)
        return (f"Pedido #{oid} do episodio {ep} criado.\n\n"
                f"{sales.pix_instructions(settings.price_episode)}")
    if low.startswith("/status"):
        return ("Sua assinatura esta ativa." if sales.subscription_active(str(chat_id))
                else "Voce nao tem assinatura ativa. Use /assinar para assinar.")

    return "Nao entendi. " + WELCOME


def main() -> None:
    token = credentials.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN nao configurado (aba Conexoes). O bot nao vai subir.")
        return

    db.init_db()
    offset: int | None = None
    log.info("bot de vendas iniciado — aguardando mensagens")

    while True:
        try:
            resp = requests.get(
                f"{API}/bot{token}/getUpdates",
                params={"timeout": 25, "offset": offset},
                timeout=40,
            ).json()
        except requests.RequestException as exc:
            log.warning("getUpdates falhou: %s", exc)
            time.sleep(5)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            chat_id = (msg.get("chat") or {}).get("id")
            name = (msg.get("from") or {}).get("first_name")
            try:
                reply = _handle_text(msg.get("text", ""), chat_id, name)
                telegram.notify(str(chat_id), reply)
            except Exception as exc:  # noqa: BLE001 — nada derruba o bot
                log.exception("erro ao tratar mensagem de %s: %s", chat_id, exc)


if __name__ == "__main__":
    main()
