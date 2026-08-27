"""Saude do token de cada canal do YouTube: renova o refresh token do cofre e diz
se esta VALIDO ou EXPIRADO/INVALIDO. Um token invalido e a causa mais comum de um
canal parar de publicar.

Identidade (nome/@handle) so aparece se o token tiver escopo de LEITURA
(youtube.readonly). Nosso escopo padrao e youtube.upload, que NAO le o canal nem o
e-mail — nesse caso o script mostra so a saude do token.

    .venv\\Scripts\\python.exe -m scripts.channel_identity
"""

from __future__ import annotations

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


def _identity(token: str) -> str | None:
    """Titulo/@handle do canal, se o escopo permitir ler. None quando nao permite."""
    try:
        data = requests.get(CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                            headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
    except requests.RequestException:
        return None
    items = data.get("items") or []
    if not items:
        return None
    sn = items[0]["snippet"]
    return f"'{sn.get('title')}' {sn.get('customUrl', '')}".strip()


def main() -> None:
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
        nome = _identity(token)
        print(prefix, f"token OK" + (f" — {nome}" if nome else " (identidade indisponivel: escopo so de upload)"))


if __name__ == "__main__":
    main()
