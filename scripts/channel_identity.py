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
    """(titulo, rotulo) do canal se o escopo permitir ler. titulo=None quando nao."""
    try:
        data = requests.get(CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                            headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
    except requests.RequestException:
        return None, ""
    items = data.get("items") or []
    if not items:
        return None, ""
    sn = items[0]["snippet"]
    titulo = sn.get("title")
    return titulo, f"'{titulo}' {sn.get('customUrl', '')}".strip()


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
        titulo, rotulo = _identity(token)
        if not titulo:
            print(prefix, "token OK (identidade indisponivel: falta o escopo "
                          "youtube.readonly — reautorize com o youtube_auth novo)")
            continue
        if args.apply and titulo != ch["name"]:
            db.update_channel(ch["id"], name=titulo)
            print(prefix, f"renomeado para {rotulo}")
        else:
            print(prefix, f"token OK — {rotulo}")


if __name__ == "__main__":
    main()
