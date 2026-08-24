"""Autenticacao, CSRF e URLs assinadas.

O painel controla contas sociais reais e o acervo inteiro, entao nenhuma rota
pode ser anonima. Duas superficies com necessidades opostas:

- **Painel** — so voce. Sessao por cookie assinado + CSRF nos formularios.
- **/media** — precisa ser alcancavel pela Meta, que busca o video por URL e nao
  faz login. Em vez de deixar aberto, cada arquivo recebe uma assinatura HMAC:
  a URL funciona sem credencial, mas nao e adivinhavel nem enumeravel.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request

from app.config import settings

log = logging.getLogger(__name__)

SESSION_COOKIE = "dubflow_session"
SESSION_TTL = 60 * 60 * 12  # 12h


def _secret_file() -> Path:
    return settings.data_dir / ".secret_key"


def get_secret() -> bytes:
    """Chave de assinatura: do .env, ou gerada e persistida com permissao restrita."""
    from_env = os.getenv("SECRET_KEY", "").strip()
    if from_env:
        return from_env.encode()

    path = _secret_file()
    if path.exists():
        return path.read_bytes().strip()

    generated = secrets.token_hex(32).encode()
    path.write_bytes(generated)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    log.info("SECRET_KEY gerada em %s", path)
    return generated


def get_password() -> str:
    """Senha do painel. Sem senha configurada, o painel nao sobe."""
    return os.getenv("DUBFLOW_PASSWORD", "").strip()


# --------------------------------------------------------------------------- sessao


def _sign(payload: str) -> str:
    mac = hmac.new(get_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def _unsign(token: str) -> str | None:
    payload, _, mac = token.rpartition(".")
    if not payload or not mac:
        return None
    expected = hmac.new(get_secret(), payload.encode(), hashlib.sha256).hexdigest()
    # compare_digest evita vazar a posicao do primeiro byte divergente por timing.
    if not hmac.compare_digest(mac, expected):
        return None
    return payload


def issue_session() -> str:
    return _sign(f"{int(time.time()) + SESSION_TTL}:{secrets.token_hex(8)}")


def session_is_valid(token: str | None) -> bool:
    if not token:
        return False
    payload = _unsign(token)
    if payload is None:
        return False
    expiry, _, _ = payload.partition(":")
    try:
        return int(expiry) > time.time()
    except ValueError:
        return False


def require_session(request: Request) -> None:
    """Dependencia das rotas do painel."""
    if not session_is_valid(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(401, "sessao invalida ou expirada", headers={"Location": "/login"})


def check_password(candidate: str) -> bool:
    expected = get_password()
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


# ----------------------------------------------------------------- rate limit login
#
# O painel controla suas contas sociais e o acervo. Com uma senha unica, expor o
# /login sem limite abre a porta para forca bruta no minuto em que o painel sai do
# 127.0.0.1 (necessario para o Instagram buscar o video por URL). O contador em
# memoria basta: e um painel de um usuario so, e reiniciar o processo zera tudo.

LOGIN_WINDOW = 300     # janela de contagem, em segundos
LOGIN_MAX_FAILS = 5    # falhas na janela antes de travar
LOGIN_LOCKOUT = 300    # tempo de bloqueio apos estourar, em segundos

_login_fails: dict[str, list[float]] = {}


def _recent_fails(ip: str, now: float) -> list[float]:
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
    if fails:
        _login_fails[ip] = fails
    else:
        _login_fails.pop(ip, None)
    return fails


def login_retry_after(ip: str) -> int:
    """Segundos que faltam de bloqueio para este IP; 0 se pode tentar agora."""
    now = time.time()
    fails = _recent_fails(ip, now)
    if len(fails) < LOGIN_MAX_FAILS:
        return 0
    liberado_em = fails[-1] + LOGIN_LOCKOUT
    return max(0, int(liberado_em - now))


def record_login_failure(ip: str) -> None:
    now = time.time()
    fails = _recent_fails(ip, now)
    fails.append(now)
    _login_fails[ip] = fails


def clear_login_failures(ip: str) -> None:
    _login_fails.pop(ip, None)


# --------------------------------------------------------------------------- csrf


def csrf_token(request: Request) -> str:
    """Token derivado da sessao: valido so para quem ja tem o cookie."""
    session = request.cookies.get(SESSION_COOKIE) or ""
    return hmac.new(get_secret(), f"csrf:{session}".encode(), hashlib.sha256).hexdigest()[:32]


def require_csrf(request: Request, submitted: str) -> None:
    """Sem isto, um site aberto em outra aba conseguiria disparar POSTs autenticados."""
    if not hmac.compare_digest(submitted or "", csrf_token(request)):
        raise HTTPException(403, "token CSRF invalido — recarregue a pagina")


# --------------------------------------------------------------------------- media


def media_signature(filename: str) -> str:
    return hmac.new(get_secret(), f"media:{filename}".encode(), hashlib.sha256).hexdigest()[:32]


def verify_media_signature(filename: str, signature: str) -> bool:
    return hmac.compare_digest(signature or "", media_signature(filename))
