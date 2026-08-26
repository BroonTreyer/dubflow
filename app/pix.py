"""Gateway de pagamento Pix: cobranca dinamica + consulta de status.

Fluxo automatico: o bot cria uma cobranca por pedido (create_charge) e manda o
QR + copia-e-cola ao cliente; o worker consulta o status (charge_status) ate
'paid' e confirma a venda sozinho — sem chave estatica nem confirmacao manual.

Como o servidor pode ficar atras de rede privada (Tailscale), a confirmacao e
por POLLING (o worker consulta), nao por webhook publico.

Multi-gateway: `PIX_PROVIDER` escolhe o provedor (abacatepay | pushinpay). Trocar
de gateway e mudar essa variavel — a interface (create_charge/charge_status) e a
mesma. Valores viajam sempre em CENTAVOS. Os tokens vem do cofre (aba Conexoes)
ou do .env, como as demais credenciais.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from app import credentials
from app.config import settings

log = logging.getLogger(__name__)

# Status normalizados (minusculas). 'created'/'pending' = aguardando pagamento.
PAID = "paid"
DEAD = {"expired", "canceled", "cancelled", "refunded"}


def _provider() -> str:
    return (settings.pix_provider or "abacatepay").strip().lower()


def _token() -> str:
    key = "PUSHINPAY_TOKEN" if _provider() == "pushinpay" else "ABACATEPAY_TOKEN"
    return credentials.get(key)


def configured() -> bool:
    return bool(_token())


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _cents(amount_brl: float) -> int:
    return max(int(round(amount_brl * 100)), 50)  # ambos exigem no minimo 50 centavos


# --------------------------------------------------------------------- AbacatePay


def _abacate_create(amount_brl: float) -> tuple[dict[str, Any] | None, str | None]:
    try:
        resp = requests.post(
            "https://api.abacatepay.com/v1/pixQrCode/create",
            json={"amount": _cents(amount_brl),
                  "expiresIn": 3600,
                  "description": "Assinatura DubFlow"},
            headers=_headers(),
            timeout=30,
        )
        body = resp.json()
    except requests.RequestException as exc:
        return None, f"erro de rede: {exc}"
    except ValueError:
        return None, "resposta invalida do gateway ao criar cobranca"
    if body.get("error"):
        return None, str(body["error"])
    data = body.get("data") or body
    txid, br = data.get("id"), data.get("brCode")
    if not txid or not br:
        return None, str(body)
    return {"txid": str(txid), "copia_cola": br,
            "qr_base64": data.get("brCodeBase64") or "",
            "valor_centavos": data.get("amount")}, None


def _abacate_status(txid: str) -> tuple[str | None, str | None]:
    try:
        resp = requests.get(
            "https://api.abacatepay.com/v1/pixQrCode/check",
            params={"id": txid},
            headers=_headers(),
            timeout=30,
        )
        body = resp.json()
    except requests.RequestException as exc:
        return None, f"erro de rede: {exc}"
    except ValueError:
        return None, "resposta invalida do gateway ao consultar status"
    if body.get("error"):
        return None, str(body["error"])
    data = body.get("data") or body
    status = str(data.get("status") or "").lower()
    return (status, None) if status else (None, str(body))


def simulate_payment(txid: str) -> tuple[bool, str | None]:
    """Simula o pagamento (SO em devMode/sandbox da AbacatePay). Util para testar
    o fluxo automatico sem Pix real. Nao tem efeito em producao."""
    if _provider() != "abacatepay":
        return False, "simulacao so disponivel na AbacatePay"
    try:
        resp = requests.post(
            "https://api.abacatepay.com/v1/pixQrCode/simulate-payment",
            json={"id": txid},
            headers=_headers(),
            timeout=30,
        )
        body = resp.json()
    except requests.RequestException as exc:
        return False, f"erro de rede: {exc}"
    except ValueError:
        return False, "resposta invalida do gateway ao simular pagamento"
    if body.get("error"):
        return False, str(body["error"])
    return True, None


# ---------------------------------------------------------------------- PushinPay


def _pushin_create(amount_brl: float) -> tuple[dict[str, Any] | None, str | None]:
    try:
        resp = requests.post(
            "https://api.pushinpay.com.br/api/pix/cashIn",
            json={"value": _cents(amount_brl)},
            headers=_headers(),
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as exc:
        return None, f"erro de rede: {exc}"
    except ValueError:
        return None, "resposta invalida do gateway ao criar cobranca"
    txid, copia = data.get("id"), data.get("qr_code")
    if not txid or not copia:
        return None, str(data.get("message") or data.get("error") or data)
    return {"txid": str(txid), "copia_cola": copia,
            "qr_base64": data.get("qr_code_base64") or "",
            "valor_centavos": data.get("value")}, None


def _pushin_status(txid: str) -> tuple[str | None, str | None]:
    try:
        resp = requests.get(
            f"https://api.pushinpay.com.br/api/transactions/{txid}",
            headers=_headers(),
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as exc:
        return None, f"erro de rede: {exc}"
    except ValueError:
        return None, "resposta invalida do gateway ao consultar status"
    status = str(data.get("status") or "").lower()
    return (status, None) if status else (None, str(data.get("message") or data))


# ------------------------------------------------------------------- interface


def create_charge(amount_brl: float) -> tuple[dict[str, Any] | None, str | None]:
    """Cria uma cobranca Pix. Retorna ({txid, copia_cola, qr_base64, valor_centavos},
    None) ou (None, erro)."""
    if not configured():
        return None, f"Gateway Pix '{_provider()}' nao configurado (falta o token)"
    return (_pushin_create if _provider() == "pushinpay" else _abacate_create)(amount_brl)


def charge_status(txid: str) -> tuple[str | None, str | None]:
    """Consulta o status. Retorna (status_minusculo, None) ou (None, erro).
    status: 'pending'/'created' | 'paid' | 'expired' | 'canceled' | 'refunded'."""
    if not configured():
        return None, "Gateway Pix nao configurado"
    return (_pushin_status if _provider() == "pushinpay" else _abacate_status)(txid)
