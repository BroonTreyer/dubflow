"""Regra de negocio da venda no Telegram (pagamento manual).

O fluxo: o comprador cria um pedido pelo bot -> ele fica 'pending' -> voce confirma
o Pix no painel (confirm_payment) -> vira 'paid' e, sendo assinatura, estende o
acesso -> a entrega marca 'delivered'. O acesso a um episodio vale se a pessoa
pagou aquele episodio avulso OU tem assinatura ativa.

Aqui so mora a regra; o banco fica no app.db e a entrega no app.publishers.telegram.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app import db
from app.config import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso or "")
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- pedidos


def create_episode_order(buyer_tg_id: str, episode_id: int, buyer_name: str | None = None) -> int:
    return db.create_order(buyer_tg_id, "episode", buyer_name, episode_id, settings.price_episode)


def create_subscription_order(buyer_tg_id: str, buyer_name: str | None = None) -> int:
    return db.create_order(buyer_tg_id, "subscription", buyer_name, None, settings.price_subscription)


def grant_episode(buyer_tg_id: str, episode_id: int, buyer_name: str | None = None) -> int:
    """Entrega gratis para quem ja tem acesso (assinante, ou ja comprou o avulso).

    Cria um pedido de valor 0 ja marcado como 'paid', para cair direto na fila de
    entrega do worker — sem pedir Pix de novo.
    """
    oid = db.create_order(buyer_tg_id, "episode", buyer_name, episode_id, 0.0)
    db.update_order(oid, status="paid")
    return oid


def confirm_payment(order_id: int) -> dict[str, Any] | None:
    """Marca o pedido como pago. Sendo assinatura, estende o acesso. Idempotente.

    Devolve o pedido atualizado (para a entrega saber o que mandar), ou None se
    o pedido nao existe. Se ja nao estava 'pending', nao mexe (evita cobrar duas vezes).
    """
    order = db.get_order(order_id)
    if order is None or order["status"] != "pending":
        return order
    db.update_order(order_id, status="paid", paid_at=db.now())
    if order["kind"] == "subscription":
        _extend_subscription(str(order["buyer_tg_id"]))
    return db.get_order(order_id)


def mark_delivered(order_id: int) -> None:
    db.update_order(order_id, status="delivered")


# ----------------------------------------------------------------------- assinatura


def _extend_subscription(buyer_tg_id: str) -> None:
    """Soma a duracao: se ainda ativa, estende a partir do fim; senao, do agora."""
    now = _now()
    atual = _parse(db.get_subscription_expiry(buyer_tg_id))
    inicio = atual if (atual and atual > now) else now
    novo = (inicio + timedelta(days=settings.subscription_days)).isoformat(timespec="seconds")
    db.set_subscription_expiry(buyer_tg_id, novo)


def subscription_active(buyer_tg_id: str, at: datetime | None = None) -> bool:
    expira = _parse(db.get_subscription_expiry(buyer_tg_id))
    return bool(expira and expira > (at or _now()))


def has_access(buyer_tg_id: str, episode_id: int) -> bool:
    """A pessoa pode receber este episodio? (assinatura ativa ou pagou o avulso)."""
    return subscription_active(buyer_tg_id) or db.orders_delivered_episode(buyer_tg_id, episode_id)


# ----------------------------------------------------------------------- textos p/ o bot


def pix_instructions(amount: float) -> str:
    valor = f"R$ {amount:.2f}".replace(".", ",")
    linhas = [f"Valor: {valor}", f"Chave Pix: {settings.pix_key or '(defina PIX_KEY no .env)'}"]
    if settings.pix_name:
        linhas.append(f"Recebedor: {settings.pix_name}")
    linhas.append("Depois de pagar, mande o comprovante aqui e aguarde a confirmacao.")
    return "\n".join(linhas)
