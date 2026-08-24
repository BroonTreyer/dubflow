"""Subprocesso que executa uma transcricao e escreve o resultado em JSON.

Existe como processo separado por um motivo pratico: quando a VRAM acaba, o
CTranslate2 as vezes nao levanta erro — ele fica esperando memoria que nunca
chega. Uma thread travada em codigo nativo nao pode ser interrompida, entao a
unica forma confiavel de aplicar um timeout e poder matar o processo inteiro.
Como efeito colateral bem-vindo, matar o processo devolve toda a VRAM.

    python -m app.pipeline._transcribe_worker <audio> <device> <compute> <saida.json>
"""

from __future__ import annotations

import app.cuda_bootstrap  # noqa: F401  (registra as DLLs CUDA antes do ctranslate2)

import json
import sys
from pathlib import Path

from app.config import settings


def main() -> int:
    if len(sys.argv) != 5:
        print("uso: _transcribe_worker <audio> <device> <compute_type> <saida.json>",
              file=sys.stderr)
        return 2

    audio_path, device, compute_type, out_path = sys.argv[1:5]

    from faster_whisper import WhisperModel

    model = WhisperModel(settings.whisper_model, device=device, compute_type=compute_type)
    segments_iter, info = model.transcribe(
        audio_path,
        language=settings.source_lang or None,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        beam_size=5,
        condition_on_previous_text=False,  # evita alucinacao em cascata em videos longos
    )

    segments: list[dict] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append(
            {
                "id": len(segments),
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": text,
                # O whisper as vezes devolve palavra sem timestamp; sem o guarda,
                # round(None) derruba a transcricao inteira no fim do processo.
                "words": [
                    {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word}
                    for w in (seg.words or [])
                    if w.start is not None and w.end is not None
                ],
            }
        )
        # Sinal de vida para o processo pai: sem isto, "lento" e "travado" sao
        # indistinguiveis de fora.
        print(f"progress {len(segments)} {seg.end:.1f}", file=sys.stderr, flush=True)

    Path(out_path).write_text(
        json.dumps(
            {
                "language": info.language,
                "language_probability": round(info.language_probability or 0, 3),
                "duration": round(info.duration or 0, 2),
                "segments": segments,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
