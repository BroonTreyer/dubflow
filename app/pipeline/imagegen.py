"""Imagem tematica da capa, gerada por IA.

O video fonte e podcast: a camera nunca sai das pessoas, entao nao existe b-roll
dentro dele para virar o painel da capa (medido no ep 1: 20 de 20 frames tinham
rosto). As capas que dao referencia — vulcao, mapa da falha, enchente — tiram
essa imagem de fora. E o que este modulo faz.

Best-effort por definicao: sem chave, sem credito ou com a API fora, devolve None
e a capa cai no layout de frame inteiro. Capa nunca reprova um corte.

So a OpenAI gera imagem aqui (a Anthropic nao tem esse endpoint), entao nao ha
failover: e uma capacidade a mais, nao um elo da corrente.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

# Reforco de estilo aplicado a TODO prompt. O modelo tende a escrever palavras na
# imagem quando o assunto e noticioso — e texto gerado sai errado e briga com o
# texto real da capa, entao e proibido explicitamente.
STYLE = (
    "Cinematic dramatic photograph, high contrast, moody lighting, "
    "photorealistic, 4k, shallow depth of field. "
    "Absolutely no text, no letters, no words, no captions, no watermark, "
    "no logos, no borders. No people in frame — the presenter is composited "
    "separately over this image."
)


def _cache_path(prompt: str, out_dir: Path, size: str) -> Path:
    # O tamanho entra na chave: retrato e paisagem sao imagens diferentes.
    chave = hashlib.sha256(f"{size}|{prompt}".encode("utf-8")).hexdigest()[:16]
    return out_dir / f"art_{chave}.png"


def generate(prompt: str, out_dir: Path, size: str | None = None) -> Path | None:
    """Gera (ou reaproveita do cache) a imagem tematica. None se nao der."""
    prompt = (prompt or "").strip()
    size = size or settings.thumb_image_size
    if not prompt or not settings.thumb_generate_image:
        return None
    if not settings.openai_api_key:
        log.info("sem OPENAI_API_KEY: capa segue sem imagem gerada")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    destino = _cache_path(prompt, out_dir, size)
    # Cache por prompt: reprocessar um episodio nao paga a imagem de novo.
    if destino.exists() and destino.stat().st_size > 0:
        log.info("imagem da capa veio do cache (%s)", destino.name)
        return destino

    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        resposta = client.images.generate(
            model=settings.thumb_image_model,
            prompt=f"{prompt}. {STYLE}",
            size=size,
        )
        b64 = getattr(resposta.data[0], "b64_json", None)
        if not b64:
            log.warning("resposta de imagem sem conteudo")
            return None
        destino.write_bytes(base64.b64decode(b64))
        log.info("imagem da capa gerada (%s %s, %d KB)",
                 settings.thumb_image_model, size, destino.stat().st_size // 1024)
        return destino
    except Exception as exc:  # noqa: BLE001 — a capa e um extra
        log.warning("geracao da imagem da capa falhou (%s)", str(exc)[:180])
        return None
