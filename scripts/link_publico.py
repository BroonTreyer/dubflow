"""Link publico TEMPORARIO para a Meta baixar um video do PC (Reels > 60s).

Por que existe: no envio direto (rupload) a Meta processa o video durante a
requisicao e desiste por volta de 34s, entao nada acima de ~60s passa (medido em
07/10/2026). Pelo `video_url` ela baixa e processa com calma (ate 15 min / 300 MB),
mas precisa de um endereco publico.

Como fica seguro:
- servidor HTTP proprio, so em 127.0.0.1, que entrega UM arquivo por endereco
  aleatorio de 32 bytes (/v/<token>.mp4); qualquer outro caminho e 404;
- o tunel (cloudflared "quick tunnel", sem conta) so existe enquanto um video
  esta sendo entregue: abre, a Meta baixa e processa, fecha;
- o painel (porta 8035) nunca passa pelo tunel.

    with link_publico.compartilhar(video) as url:
        ...  # passa url como video_url e espera o container ficar FINISHED
"""

from __future__ import annotations

import contextlib
import logging
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

log = logging.getLogger("link_publico")

RAIZ = Path(__file__).resolve().parent.parent
CLOUDFLARED = RAIZ / "ferramentas" / "cloudflared.exe"
ESPERA_TUNEL = 90          # segundos para o endereco do tunel responder
_URL = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com")

_arquivos: dict[str, Path] = {}       # token -> arquivo liberado agora
_trava = threading.Lock()
_uso = 0                              # quantos compartilhamentos abertos
_servidor: ThreadingHTTPServer | None = None
_tunel: subprocess.Popen | None = None
_endereco: str | None = None


class ErroLink(Exception):
    pass


class _Entrega(BaseHTTPRequestHandler):
    """GET/HEAD de /v/<token>.mp4, com Range (a Meta pode baixar em pedacos)."""

    def log_message(self, *_):  # sem log por requisicao
        pass

    def _alvo(self) -> Path | None:
        m = re.fullmatch(r"/v/([A-Za-z0-9_-]{20,})\.mp4", self.path.split("?")[0])
        return _arquivos.get(m.group(1)) if m else None

    def do_HEAD(self):
        self._responder(corpo=False)

    def do_GET(self):
        if self.path == "/saude":
            self._simples(200, b"ok")
            return
        self._responder(corpo=True)

    def _simples(self, codigo: int, corpo: bytes = b"") -> None:
        self.send_response(codigo)
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        if corpo and self.command != "HEAD":
            self.wfile.write(corpo)

    def _responder(self, corpo: bool) -> None:
        arq = self._alvo()
        if arq is None or not arq.exists():
            self._simples(404)
            return
        tamanho = arq.stat().st_size
        ini, fim = 0, tamanho - 1
        faixa = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if faixa and (faixa.group(1) or faixa.group(2)):
            if faixa.group(1):
                ini = int(faixa.group(1))
                fim = min(int(faixa.group(2)), fim) if faixa.group(2) else fim
            else:  # ultimos N bytes
                ini = max(0, tamanho - int(faixa.group(2)))
            if ini > fim:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{tamanho}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {ini}-{fim}/{tamanho}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fim - ini + 1))
        self.end_headers()
        if not corpo:
            return
        with open(arq, "rb") as f:
            f.seek(ini)
            falta = fim - ini + 1
            while falta > 0:
                bloco = f.read(min(1 << 20, falta))
                if not bloco:
                    break
                try:
                    self.wfile.write(bloco)
                except (BrokenPipeError, ConnectionResetError):
                    return
                falta -= len(bloco)


def disponivel() -> bool:
    return CLOUDFLARED.exists()


def _subir_servidor() -> int:
    global _servidor
    if _servidor is None:
        _servidor = ThreadingHTTPServer(("127.0.0.1", 0), _Entrega)
        _servidor.daemon_threads = True
        threading.Thread(target=_servidor.serve_forever, daemon=True,
                         name="link-publico").start()
    return _servidor.server_address[1]


def _abrir_tunel(porta: int) -> str:
    global _tunel, _endereco
    if not disponivel():
        raise ErroLink("falta ferramentas/cloudflared.exe (necessario para videos acima de 60s)")
    cmd = [str(CLOUDFLARED), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{porta}"]
    _tunel = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              text=True, errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    achado: list[str] = []

    def ler():  # o endereco sai no stderr; continua lendo para o pipe nao encher
        for linha in _tunel.stderr:
            if not achado and (m := _URL.search(linha)):
                achado.append(m.group(0))
    threading.Thread(target=ler, daemon=True, name="cloudflared-log").start()

    limite = time.time() + ESPERA_TUNEL
    while time.time() < limite and not achado:
        if _tunel.poll() is not None:
            raise ErroLink("o cloudflared fechou ao abrir o tunel")
        time.sleep(0.5)
    if not achado:
        _fechar_tunel()
        raise ErroLink("o tunel nao devolveu endereco a tempo")
    endereco = achado[0]
    # o nome novo leva alguns segundos para existir na internet
    while time.time() < limite:
        try:
            if requests.get(f"{endereco}/saude", timeout=10).status_code == 200:
                _endereco = endereco
                log.info("tunel aberto: %s", endereco)
                return endereco
        except requests.RequestException:
            pass
        time.sleep(2)
    _fechar_tunel()
    raise ErroLink("o tunel abriu mas nao respondeu pela internet")


def _fechar_tunel() -> None:
    global _tunel, _endereco
    if _tunel is not None:
        _tunel.terminate()
        try:
            _tunel.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _tunel.kill()
        log.info("tunel fechado")
    _tunel, _endereco = None, None


@contextlib.contextmanager
def compartilhar(arquivo: Path):
    """URL publica do arquivo enquanto o bloco `with` durar."""
    global _uso
    token = secrets.token_urlsafe(32)
    with _trava:
        porta = _subir_servidor()
        if _tunel is None or _tunel.poll() is not None:
            _fechar_tunel()
            _abrir_tunel(porta)
        _arquivos[token] = arquivo
        _uso += 1
        endereco = _endereco
    try:
        yield f"{endereco}/v/{token}.mp4"
    finally:
        with _trava:
            _arquivos.pop(token, None)
            _uso -= 1
            if _uso == 0:
                _fechar_tunel()
