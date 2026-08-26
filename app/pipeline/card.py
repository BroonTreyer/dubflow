"""Molde opcional do corte vertical: so o CTA.

Uma pilula discreta com "SEGUE O PERFIL" no rodape, SOBREPOSTA ao video — que
continua em tela cheia (o face-crop de clips.py). O miolo fica transparente.

Ja existiu aqui uma faixa com o gancho no topo. Foi removida em 25/08/2026: o
gancho ja aparece na legenda queimada e na capa, e repetido numa tarja preta em
cima do video ele so roubava area da imagem sem acrescentar nada.

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

def render_overlay(cta: str, output_path: Path,
                   size: tuple[int, int] = (1080, 1920)) -> Path | None:
    """PNG RGBA com a pilula do CTA no rodape; todo o resto transparente.

    Devolve None (corte sai sem molde) se faltar fonte/Pillow, se nao houver texto,
    ou se qualquer passo falhar — o molde e um extra, nunca reprova o corte.
    """
    font_file = thumbnail._font_path()
    if font_file is None:
        log.warning("sem fonte pesada; corte sai sem molde")
        return None

    cta = (cta or "").strip()
    if not cta:
        return None

    try:
        from PIL import Image, ImageDraw, ImageFont

        largura, altura = size
        img = Image.new("RGBA", (largura, altura), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        _draw_cta(draw, cta, font_file, largura, altura, ImageFont)

        img.save(output_path)
        return output_path
    except Exception as exc:  # noqa: BLE001 — molde e opcional; nunca quebra o corte
        log.warning("molde do corte falhou (%s); segue sem molde", exc)
        return None


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
