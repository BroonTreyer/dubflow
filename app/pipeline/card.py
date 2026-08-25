"""Molde opcional do corte vertical.

Uma faixa com o gancho (hook) no topo e um CTA discreto embaixo, SOBREPOSTOS ao
video — que continua em tela cheia (o face-crop de clips.py). Minimalista de
proposito: so as duas faixas ocupam pixel, o miolo fica transparente.

Diferente da capa (thumbnail.py, uma imagem estatica que substitui o frame),
aqui o resultado e um PNG RGBA transparente no meio, para o ffmpeg sobrepor em
TODOS os frames do corte via `overlay`, sem encolher o video.

Reusa o motor de texto do thumbnail.py (fonte pesada, quebra de linha, destaque).
Nunca derruba o render: qualquer falha vira None e o corte sai sem molde.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.pipeline import thumbnail

log = logging.getLogger(__name__)

# Fracao da tela reservada ao texto do gancho — teto para o molde ficar enxuto.
HOOK_BODY_FRAC = 0.050   # corpo inicial da fonte, relativo a altura
HOOK_MAX_LINES = 3
HIGHLIGHT = (255, 216, 0)   # amarelo do realce (mesma cor da capa)


def render_overlay(hook: str, cta: str, output_path: Path,
                   size: tuple[int, int] = (1080, 1920)) -> Path | None:
    """PNG RGBA: faixa do gancho no topo + pilula de CTA embaixo, miolo transparente.

    Devolve None (corte sai sem molde) se faltar fonte/Pillow, se nao houver texto,
    ou se qualquer passo falhar — o molde e um extra, nunca reprova o corte.
    """
    font_file = thumbnail._font_path()
    if font_file is None:
        log.warning("sem fonte pesada; corte sai sem molde")
        return None

    hook = (hook or "").strip()
    cta = (cta or "").strip()
    if not hook and not cta:
        return None

    try:
        from PIL import Image, ImageDraw, ImageFont

        largura, altura = size
        img = Image.new("RGBA", (largura, altura), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        margem = int(largura * 0.06)
        largura_util = largura - 2 * margem

        if hook:
            _draw_hook(draw, hook, font_file, largura, altura, largura_util)
        if cta:
            _draw_cta(draw, cta, font_file, largura, altura, ImageFont)

        img.save(output_path)
        return output_path
    except Exception as exc:  # noqa: BLE001 — molde e opcional; nunca quebra o corte
        log.warning("molde do corte falhou (%s); segue sem molde", exc)
        return None


def _draw_hook(draw, hook: str, font_file: str, largura: int, altura: int,
               largura_util: int) -> None:
    """Faixa escura semitransparente no topo com o gancho, legivel sobre qualquer cena."""
    palavras = thumbnail.parse_highlight(hook)
    corpo = int(altura * HOOK_BODY_FRAC)
    fonte, linhas = thumbnail._fit_lines(draw, palavras, largura_util, corpo,
                                         font_file, max_linhas=HOOK_MAX_LINES)
    alturas = [draw.textbbox((0, 0), "Ay", font=fonte)[3] for _ in linhas]
    entrelinha = int(fonte.size * 0.18)
    bloco = sum(alturas) + entrelinha * (len(linhas) - 1)

    topo = int(altura * 0.02)
    pad = int(altura * 0.028)
    # A faixa nao depende da imagem: como e sobreposta no video (que muda a cada
    # frame), um veu fixo e o unico jeito de garantir contraste sempre.
    draw.rectangle([0, 0, largura, topo + bloco + 2 * pad], fill=(0, 0, 0, 175))

    contorno = max(3, int(fonte.size * 0.10))
    y = topo + pad
    for linha, alt in zip(linhas, alturas):
        largura_linha = sum(draw.textlength(p, font=fonte) for p, _ in linha)
        largura_linha += draw.textlength(" ", font=fonte) * (len(linha) - 1)
        x = (largura - largura_linha) / 2
        for palavra, destaque in linha:
            draw.text((x, y), palavra, font=fonte,
                      fill=HIGHLIGHT if destaque else thumbnail.WHITE,
                      stroke_width=contorno, stroke_fill=thumbnail.BLACK)
            x += draw.textlength(palavra + " ", font=fonte)
        y += alt + entrelinha


def _draw_cta(draw, cta: str, font_file: str, largura: int, altura: int,
              ImageFont) -> None:
    """Pilula compacta com o CTA no rodape (texto puro — a fonte nao faz emoji)."""
    fonte = ImageFont.truetype(font_file, int(altura * 0.026))
    tw = draw.textlength(cta, font=fonte)
    th = draw.textbbox((0, 0), "Ay", font=fonte)[3]
    px, py = int(largura * 0.035), int(th * 0.55)
    bw, bh = tw + 2 * px, th + 2 * py
    bx = (largura - bw) / 2
    by = altura - bh - int(altura * 0.035)
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=int(bh / 2),
                           fill=(0, 0, 0, 195))
    draw.text((bx + px, by + py - int(th * 0.1)), cta, font=fonte,
              fill=thumbnail.WHITE, stroke_width=2, stroke_fill=thumbnail.BLACK)
