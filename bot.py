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
from pathlib import Path

import requests

from app import credentials, db, pix, sales
from app.config import configure_logging, settings
from app.pipeline import archive
from app.publishers import telegram

sys.stderr.reconfigure(encoding="utf-8", errors="replace")
configure_logging("bot")
log = logging.getLogger("bot")

API = (settings.telegram_api_base or "https://api.telegram.org").rstrip("/")

# Botoes fixos do teclado (o cliente toca em vez de digitar comando). Sem emoji.
BTN_MENSAL = "Assinar mensal"
BTN_VITALICIO = "Plano vitalício"
BTN_CATALOGO = "Catálogo"
BTN_ASSINANTE = "Minha assinatura"
BTN_CANAL = "Canal geral"


def main_keyboard() -> dict:
    """Teclado persistente de botoes — a cara profissional do bot."""
    return {
        "keyboard": [
            [{"text": BTN_MENSAL}, {"text": BTN_VITALICIO}],
            [{"text": BTN_CATALOGO}, {"text": BTN_ASSINANTE}],
            [{"text": BTN_CANAL}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def _preco(v: float) -> str:
    return f"R$ {v:.2f}".replace(".", ",")


def _welcome() -> str:
    """Boas-vindas vendedora (vai como legenda do banner no /start)."""
    return (
        "Bem-vindo ao DubFlow — seus doramas e séries favoritos legendados com "
        "capricho em português.\n\n"
        "No canal geral você acompanha cortes e novidades todos os dias, de graça. "
        "Para assistir aos episódios completos, entre no VIP:\n\n"
        f"• Mensal: {_preco(settings.price_subscription)} — acesso por "
        f"{settings.subscription_days} dias\n"
        f"• Vitalício: {_preco(settings.price_lifetime)} — acesso permanente, sem "
        "mensalidade\n"
        f"• Episódio avulso: {_preco(settings.price_episode)} — leva só o que quiser\n\n"
        "O pagamento é por Pix, com QR na hora e liberação automática, sem "
        "comprovante. Toque em um dos planos aqui embaixo para gerar seu QR Code."
    )


def _welcome_image() -> Path | None:
    """Banner do /start: usa o configurado (TELEGRAM_WELCOME_IMAGE) ou o padrao
    embutido em assets/, se existir. Sem imagem, o /start vai so em texto."""
    if settings.telegram_welcome_image:
        p = Path(settings.telegram_welcome_image)
        if p.exists():
            return p
    default = Path(__file__).resolve().parent / "assets" / "welcome_banner.png"
    return default if default.exists() else None


def _canal_geral() -> str:
    link = settings.telegram_channel_link or "(canal em breve)"
    return (
        f"Nosso canal geral, com cortes e novidades:\n{link}\n\n"
        "Siga por lá para não perder nada. Quando quiser assistir aos episódios "
        "completos, toque em Seja Prime."
    )


def _catalogo() -> str:
    itens = telegram.catalog(only_sellable=True)
    if not itens:
        return (
            "Estamos preparando novidades para você. O catálogo abre em breve — "
            "garanta seu acesso com Seja Prime e seja o primeiro a assistir."
        )
    linhas = ["Catálogo de episódios — escolha o seu:", ""]
    for it in itens:
        linhas.append(f"{it.get('id')} — {it.get('titulo') or 'sem título'} "
                      f"({it.get('canal') or '?'})")
    linhas += [
        "",
        "Para levar um episódio avulso: /comprar <número>.",
        "",
        f"Dica: por {_preco(settings.price_subscription)} você assina e assiste a "
        f"tudo por {settings.subscription_days} dias — bem mais em conta do que "
        f"{_preco(settings.price_episode)} por episódio. Toque em Seja Prime.",
    ]
    return "\n".join(linhas)


def _send_pix_charge(chat_id: int, amount: float, order_id: int, header: str) -> str:
    """Gera a cobranca no gateway, manda o QR + copia-e-cola e guarda o txid.

    Retorna o texto final (o copia-e-cola vai por ultimo, sozinho, para o cliente
    copiar com um toque). Se o gateway nao estiver configurado, cai no Pix manual.
    """
    charge, err = pix.create_charge(amount)
    if not charge:
        # Sem gateway (ou erro): mantem o fluxo manual — chave estatica + comprovante.
        if err:
            log.warning("pedido %s: cobranca Pix automatica falhou (%s)", order_id, err)
        return f"{header}\n\n{sales.pix_instructions(amount)}"

    db.update_order(order_id, pix_txid=charge["txid"])
    valor = f"R$ {amount:.2f}".replace(".", ",")
    caption = (
        f"{header}\n\n"
        f"Valor: {valor}\n\n"
        "Pague escaneando o QR acima ou copiando o código Pix na próxima mensagem. "
        "A confirmação é automática: assim que o pagamento cair, seu acesso é "
        "liberado na hora, sem precisar enviar comprovante."
    )
    if charge.get("qr_base64"):
        telegram.send_photo_b64(str(chat_id), charge["qr_base64"], caption)
    else:
        telegram.notify(str(chat_id), caption)
    # O copia-e-cola vai isolado como ultima mensagem: fica facil de tocar e copiar.
    return charge["copia_cola"]


def _handle_text(text: str, chat_id: int, name: str | None) -> str:
    """Trata uma mensagem e devolve a resposta (criando pedido quando for o caso)."""
    raw = (text or "").strip()
    # Os botoes do teclado chegam como texto: mapeia para o comando equivalente.
    if raw == BTN_MENSAL:
        raw = "/assinar"
    elif raw == BTN_VITALICIO:
        raw = "/vitalicio"
    elif raw == BTN_CATALOGO:
        raw = "/catalogo"
    elif raw == BTN_ASSINANTE:
        raw = "/status"
    elif raw == BTN_CANAL:
        return _canal_geral()
    low = raw.lower()

    if low.startswith("/start"):
        return _welcome()
    if low.startswith("/catalogo"):
        return _catalogo()
    if low.startswith("/assinar"):
        oid = sales.create_subscription_order(str(chat_id), name)
        header = (f"Ótima escolha. Plano mensal reservado (pedido #{oid}) — "
                  f"acesso a todos os episódios por {settings.subscription_days} dias.")
        return _send_pix_charge(chat_id, settings.price_subscription, oid, header)
    if low.startswith("/vitalicio") or low.startswith("/vitalício"):
        oid = sales.create_lifetime_order(str(chat_id), name)
        header = (f"Excelente. Plano vitalício reservado (pedido #{oid}) — acesso "
                  "permanente ao VIP, sem mensalidade, para sempre.")
        return _send_pix_charge(chat_id, settings.price_lifetime, oid, header)
    if low.startswith("/comprar"):
        partes = (text or "").split()
        if len(partes) < 2 or not partes[1].isdigit():
            return ("Me diga o número do episódio assim: /comprar 12. "
                    "Não sabe qual? Veja no /catalogo.")
        ep = int(partes[1])
        item = archive.find(ep)
        if item is None or item.get("licenca") not in telegram.SELLABLE_LICENSES:
            return ("Não encontrei esse episódio no catálogo. "
                    "Confira os números disponíveis no /catalogo.")
        # Assinante (ou quem ja comprou este avulso) recebe na hora, sem pagar de novo.
        if sales.has_access(str(chat_id), ep):
            sales.grant_episode(str(chat_id), ep, name)
            return "Você já tem acesso a esse episódio. Estou enviando agora, um instante."
        oid = sales.create_episode_order(str(chat_id), ep, name)
        header = f"Ótima escolha. Episódio {ep} reservado (pedido #{oid})."
        return _send_pix_charge(chat_id, settings.price_episode, oid, header)
    if low.startswith("/status"):
        if sales.subscription_active(str(chat_id)):
            if sales.is_lifetime(str(chat_id)):
                return "Você tem acesso vitalício ao VIP. Aproveite sem limites."
            return ("Sua assinatura está ativa. Aproveite todos os episódios à "
                    "vontade. Bom dorama.")
        return (f"Você ainda não tem acesso ao VIP. Planos: mensal "
                f"{_preco(settings.price_subscription)} ou vitalício "
                f"{_preco(settings.price_lifetime)}. Toque em Assinar mensal ou "
                "Plano vitalício para começar.")

    return ("Não entendi bem. Toque em um dos botões aqui embaixo para "
            "começar.\n\n" + _welcome())


def main() -> None:
    token = credentials.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN nao configurado (aba Conexoes). O bot nao vai subir.")
        return

    db.init_db()
    offset: int | None = None
    banner = _welcome_image()
    log.info("bot de vendas iniciado — aguardando mensagens%s",
             f" (banner: {banner.name})" if banner else " (sem banner)")

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
            text_in = msg.get("text", "")
            try:
                reply = _handle_text(text_in, chat_id, name)
                # No /start manda o banner com a legenda; senao, texto. Sempre com
                # o teclado de botoes fixo.
                if text_in.strip().lower().startswith("/start") and banner:
                    telegram.send_photo_path(str(chat_id), banner, reply, main_keyboard())
                else:
                    telegram.notify(str(chat_id), reply, main_keyboard())
            except Exception as exc:  # noqa: BLE001 — nada derruba o bot
                log.exception("erro ao tratar mensagem de %s: %s", chat_id, exc)


if __name__ == "__main__":
    main()
