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

O refresh token nao expira sozinho (so se voce revogar o acesso ou trocar a
senha). Guarde como qualquer credencial — quem o tem publica no seu canal.
"""

from __future__ import annotations

import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from app.config import settings

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
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


def main() -> int:
    client_id = _prompt("YOUTUBE_CLIENT_ID", settings.youtube_client_id)
    client_secret = _prompt("YOUTUBE_CLIENT_SECRET", settings.youtube_client_secret)

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
    print("Sucesso! Cole a linha abaixo no seu .env:\n")
    print(f"YOUTUBE_REFRESH_TOKEN={refresh}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
