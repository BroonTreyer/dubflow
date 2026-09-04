"""Gera o YOUTUBE_REFRESH_TOKEN uma vez, via fluxo OAuth de app Desktop.

Passo a passo:

  1. No Google Cloud, crie um projeto, ative a "YouTube Data API v3" e uma
     credencial OAuth do tipo "Desktop app". Anote o client id e o secret.
  2. Preencha YOUTUBE_CLIENT_ID e YOUTUBE_CLIENT_SECRET no .env (ou tenha eles
     em maos — o script pergunta se faltarem).
  3. Rode:

         .venv\\Scripts\\python.exe -m scripts.youtube_auth

  4. O navegador abre pedindo autorizacao da SUA conta do YouTube. Ao aceitar,
     o script captura o retorno num servidor local e imprime a linha pronta:

         YOUTUBE_REFRESH_TOKEN=1//0g...

     Cole no .env e o publisher do YouTube passa a funcionar.

Multi-conta: com `--channel <id>` (o id vem do painel /channels), o script usa o
client id/secret DAQUELE canal (do cofre, nao do .env) e grava o refresh token
direto no cofre do canal — nada a colar, e sem risco de trocar as contas:

         .venv\\Scripts\\python.exe -m scripts.youtube_auth --channel 3

     Autorize logado NA conta do YouTube daquele canal.

O refresh token nao expira sozinho (so se voce revogar o acesso ou trocar a
senha). Guarde como qualquer credencial — quem o tem publica no seu canal.
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from app import credentials, db
from app.config import settings

# upload = publicar; readonly = ler a identidade do canal (nome/@handle) para o
# painel saber qual conta e qual. Espaco separa multiplos escopos.
SCOPE = ("https://www.googleapis.com/auth/youtube.upload"
         " https://www.googleapis.com/auth/youtube.readonly")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# O Google exige que o redirect_uri de app Desktop seja loopback. A porta e
# livre; registramos http://localhost:<porta> como retorno so por esta execucao.
REDIRECT_PORT = 8731
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/"


class _CatchCode(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 — assinatura da stdlib
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _CatchCode.code = (params.get("code") or [None])[0]
        _CatchCode.error = (params.get("error") or [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Autorizado. Pode fechar esta aba e voltar ao terminal."
        if _CatchCode.error:
            msg = f"Falhou: {_CatchCode.error}. Volte ao terminal."
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode("utf-8"))

    def log_message(self, *_args: object) -> None:
        pass  # silencia o log de acesso da stdlib


def _prompt(label: str, current: str) -> str:
    if current:
        return current
    value = input(f"{label}: ").strip()
    if not value:
        print(f"  {label} e obrigatorio.")
        sys.exit(1)
    return value


def _resolve_creds(channel_id: int | None) -> tuple[str, str]:
    """Client id/secret da conta: do cofre do canal (multi-conta) ou do .env global."""
    if channel_id is None:
        return settings.youtube_client_id, settings.youtube_client_secret
    return (credentials.get("YOUTUBE_CLIENT_ID", channel_id),
            credentials.get("YOUTUBE_CLIENT_SECRET", channel_id))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gera o YOUTUBE_REFRESH_TOKEN. Com --channel, usa o client "
        "id/secret daquele canal e grava o token no cofre dele (nada a colar)."
    )
    parser.add_argument("--channel", type=int, default=None,
                        help="id do canal (ver painel /channels). Sem isso, usa o .env global.")
    args = parser.parse_args()

    channel = None
    if args.channel is not None:
        channel = db.get_channel(args.channel)
        if channel is None:
            print(f"Canal {args.channel} nao existe. Veja os ids em /channels.")
            return 1
        print(f"Gerando refresh token para o canal {args.channel}: '{channel['name']}'.")
        print("IMPORTANTE: autorize logado NA conta do YouTube deste canal.\n")

    cid_atual, secret_atual = _resolve_creds(args.channel)
    client_id = _prompt("YOUTUBE_CLIENT_ID", cid_atual)
    client_secret = _prompt("YOUTUBE_CLIENT_SECRET", secret_atual)

    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            # offline + consent forcam o Google a devolver um refresh token — sem
            # isso, uma segunda autorizacao da mesma conta so traz access token.
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    auth_url = f"{AUTH_URL}?{params}"

    print("Abrindo o navegador para autorizar o acesso ao seu canal do YouTube...")
    print(f"Se nao abrir, cole esta URL manualmente:\n\n  {auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", REDIRECT_PORT), _CatchCode)
    server.handle_request()  # bloqueia ate o Google redirecionar de volta
    server.server_close()

    if _CatchCode.error or not _CatchCode.code:
        print(f"Autorizacao nao concluida: {_CatchCode.error or 'sem code'}")
        return 1

    token = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": _CatchCode.code,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    ).json()

    refresh = token.get("refresh_token")
    if not refresh:
        print(f"O Google nao devolveu refresh token: {token.get('error_description') or token}")
        print("Dica: revogue o acesso antigo em https://myaccount.google.com/permissions e tente de novo.")
        return 1

    print("\n" + "=" * 60)
    if args.channel is not None:
        # Grava client id/secret JUNTO com o refresh token. Um refresh token so
        # funciona com o cliente OAuth que o emitiu, entao guardar so ele deixava
        # o canal num estado impossivel: autorizado e incapaz de publicar, com a
        # mensagem "Could not determine client ID from request" — que nao aponta
        # para a causa. Pior quando o cofre global tinha um client id qualquer:
        # como sao SHARED_KEYS, o canal herdava o cliente ERRADO em silencio.
        credentials.save({
            "YOUTUBE_REFRESH_TOKEN": refresh,
            "YOUTUBE_CLIENT_ID": client_id,
            "YOUTUBE_CLIENT_SECRET": client_secret,
        }, args.channel)
        print(f"Sucesso! Refresh token e credencial do cliente gravados no cofre "
              f"do canal {args.channel} ('{channel['name']}').")
        print("Nada a colar — o publisher do YouTube ja pode postar por este canal.")
    else:
        print("Sucesso! Cole a linha abaixo no seu .env:\n")
        print(f"YOUTUBE_REFRESH_TOKEN={refresh}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
