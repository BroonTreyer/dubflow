"""Saude do token de cada canal do YouTube: renova o refresh token do cofre e diz
se esta VALIDO ou EXPIRADO/INVALIDO. Um token invalido e a causa mais comum de um
canal parar de publicar.

Identidade (nome/@handle) so aparece se o token tiver escopo de LEITURA
(youtube.readonly). Nosso escopo padrao e youtube.upload, que NAO le o canal nem o
e-mail — nesse caso o script mostra so a saude do token.

    .venv\\Scripts\\python.exe -m scripts.channel_identity
"""

from __future__ import annotations

import argparse

import requests

from app import credentials, db

TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


def _access_token(cid: str, secret: str, refresh: str) -> tuple[str | None, str | None]:
    try:
        r = requests.post(TOKEN_URL, data={
            "client_id": cid, "client_secret": secret,
            "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=30).json()
    except requests.RequestException as exc:
        return None, f"erro de rede: {exc}"
    return r.get("access_token"), (r.get("error_description") or r.get("error"))


def _identity(token: str) -> tuple[str | None, str]:
    """(titulo, detalhe). titulo=None quando nao deu; detalhe explica o porque:
    rotulo do canal no sucesso, ou o motivo real (API desativada, sem escopo, sem
    canal) — nao mais um chute de 'falta escopo'."""
    try:
        data = requests.get(CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                            headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
    except requests.RequestException as exc:
        return None, f"erro de rede: {exc}"
    err = data.get("error")
    if err:
        msg = str(err.get("message", err))
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        if "has not been used in project" in msg or reason in ("accessNotConfigured", "SERVICE_DISABLED"):
            return None, "YouTube Data API v3 DESATIVADA no projeto Cloud deste canal — ative-a (isso tambem impede o UPLOAD)"
        if reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
            return None, "falta o escopo youtube.readonly — reautorize com o youtube_auth novo"
        return None, msg
    items = data.get("items") or []
    if not items:
        return None, "token e API ok, mas a conta nao tem um canal do YouTube criado"
    sn = items[0]["snippet"]
    titulo = sn.get("title")
    handle = sn.get("customUrl", "")
    # Inclui o @handle: varios canais podem ter o MESMO titulo (contas novas), e o
    # handle e o que os distingue no painel.
    return titulo, handle


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Saude do token e identidade dos canais do YouTube.")
    parser.add_argument("--apply", action="store_true",
                        help="renomeia cada canal no painel com o nome real do YouTube "
                             "(exige token com escopo youtube.readonly).")
    args = parser.parse_args()

    db.init_db()
    canais = [c for c in db.list_channels() if c["platform"] == "youtube"]
    if not canais:
        print("Nenhum canal do YouTube cadastrado.")
        return
    for ch in canais:
        prefix = f"canal {ch['id']:>2} ({ch['name']}):"
        refresh = credentials.get("YOUTUBE_REFRESH_TOKEN", ch["id"])
        if not refresh:
            print(prefix, "SEM refresh token — nao autorizado")
            continue
        token, err = _access_token(
            credentials.get("YOUTUBE_CLIENT_ID", ch["id"]),
            credentials.get("YOUTUBE_CLIENT_SECRET", ch["id"]),
            refresh)
        if not token:
            print(prefix, f"TOKEN INVALIDO/EXPIRADO ({err}) -> reautorize: "
                          f"youtube_auth --channel {ch['id']}")
            continue
        titulo, extra = _identity(token)
        if not titulo:
            print(prefix, f"token OK, mas {extra}")
            continue
        # extra = @handle. Nome distinguivel mesmo quando varios canais tem o mesmo titulo.
        nome_real = f"{titulo} ({extra})" if extra else titulo
        rotulo = f"'{titulo}' {extra}".strip()
        if args.apply and nome_real != ch["name"]:
            db.update_channel(ch["id"], name=nome_real)
            print(prefix, f"renomeado para {rotulo}")
        else:
            print(prefix, f"token OK — {rotulo}")


if __name__ == "__main__":
    main()
