r"""Gera os icones dos atalhos da area de trabalho (assets/*.ico).

    .venv\Scripts\python.exe scripts\gerar_icones.py

Os .ico ficam versionados; so rode isto se quiser mudar o desenho. A paleta e a
mesma do painel (app/web/templates/base.html).
"""
from pathlib import Path

from PIL import Image, ImageDraw

BG = (14, 17, 22)
LINE = (38, 44, 54)
ACCENT = (79, 140, 255)
TEXTO = (230, 233, 239)
ERR = (248, 81, 73)
WARN = (210, 153, 34)

S = 512
TAMANHOS = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def _base() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=96, fill=BG, outline=LINE, width=8)
    return img, d


def _legenda(d: ImageDraw.ImageDraw, cx: float, cor_baixo: tuple[int, int, int]) -> None:
    """As duas barras de legenda embaixo — a assinatura do produto."""
    for i, (larg, cor) in enumerate([(0.52, TEXTO), (0.32, cor_baixo)]):
        y = S * (0.68 + i * 0.115)
        meia = S * larg / 2
        d.rounded_rectangle([cx - meia, y, cx + meia, y + S * 0.062], radius=S * 0.031, fill=cor)


def abrir() -> Path:
    img, d = _base()
    cx, cy, r = S * 0.5, S * 0.42, S * 0.17
    d.polygon([(cx - r * 0.62, cy - r), (cx - r * 0.62, cy + r), (cx + r * 0.86, cy)], fill=ACCENT)
    _legenda(d, cx, ACCENT)
    destino = ASSETS / "dubflow.ico"
    img.save(destino, sizes=TAMANHOS)
    return destino


def parar() -> Path:
    img, d = _base()
    cx, cy, r = S * 0.5, S * 0.42, S * 0.15
    d.rounded_rectangle([cx - r, cy - r, cx + r, cy + r], radius=S * 0.035, fill=ERR)
    _legenda(d, cx, ERR)
    destino = ASSETS / "dubflow_parar.ico"
    img.save(destino, sizes=TAMANHOS)
    return destino


def reautorizar() -> Path:
    """Seta circular: reconectar as contas. Amarelo = mexe em credencial."""
    img, d = _base()
    cx, cy, r = S * 0.5, S * 0.42, S * 0.155
    grossura = int(S * 0.055)
    # Arco aberto no canto superior direito, onde entra a ponta da seta.
    d.arc([cx - r, cy - r, cx + r, cy + r], start=310, end=210, fill=WARN, width=grossura)
    ponta = S * 0.075
    topo = (cx + r * 0.72, cy - r * 0.72)
    d.polygon([(topo[0] - ponta, topo[1] - ponta * 0.15),
               (topo[0] + ponta * 0.35, topo[1] - ponta),
               (topo[0] + ponta * 0.2, topo[1] + ponta * 0.8)], fill=WARN)
    _legenda(d, cx, WARN)
    destino = ASSETS / "dubflow_contas.ico"
    img.save(destino, sizes=TAMANHOS)
    return destino


if __name__ == "__main__":
    for caminho in (abrir(), parar(), reautorizar()):
        print(f"{caminho} gerado")
