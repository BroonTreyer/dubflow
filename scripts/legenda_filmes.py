"""Legenda dos cortes de filme: transcreve, traduz pra pt-BR se preciso, gera o ASS.

Reaproveita o pipeline do dubflow:
    audio -> faster-whisper na GPU (app.pipeline.transcribe)
          -> traducao so se o audio nao for portugues (app.pipeline.translate)
          -> ASS posicionado no rodape do VIDEO, nao da tela (app.pipeline.subtitles)

A transcricao (ja traduzida) fica em <video>.legenda.json, com a chave do trecho
cortado: refazer o render, mudar cor ou gancho nao transcreve de novo. Mudar o
inicio/fim do corte invalida, porque o audio e outro.

Uma transcricao por vez (_trava_gpu): a placa tem 8 GB e o render roda ate 6
videos em paralelo.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_trava_gpu = threading.Lock()
IDIOMA = "pt-BR"


def _sidecar(video: Path) -> Path:
    return video.with_suffix(".legenda.json")


def _sidecar_opcao(video: Path) -> Path:
    return video.with_suffix(".legendar.json")


def quer_legenda(video: Path, molde: dict) -> bool:
    """Escolha do video (.legendar.json) ou, sem ela, o padrao do molde."""
    arq = _sidecar_opcao(video)
    if arq.exists():
        try:
            return bool(json.loads(arq.read_text(encoding="utf-8")).get("legendar"))
        except (json.JSONDecodeError, AttributeError):
            pass
    return bool(molde.get("legendar_padrao"))


def tem_escolha_propria(video: Path) -> bool:
    return _sidecar_opcao(video).exists()


def escolher(video: Path, legendar: bool | None) -> None:
    """True/False grava a escolha do video; None volta pro padrao do molde."""
    arq = _sidecar_opcao(video)
    if legendar is None:
        arq.unlink(missing_ok=True)
    else:
        arq.write_text(json.dumps({"legendar": bool(legendar)}), encoding="utf-8")


def arquivos_de(video: Path) -> list[Path]:
    """Tudo que este modulo grava ao lado do video (para a limpeza apagar junto)."""
    return [_sidecar(video), _sidecar_opcao(video)]


def _chave(video: Path, inicio: float, fim: float) -> str:
    return f"{video.stat().st_size}:{video.stat().st_mtime_ns}:{inicio:.2f}:{fim:.2f}"


def _tem_audio(video: Path) -> bool:
    saida = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True).stdout
    return bool(saida.strip())


def segmentos(video: Path, inicio: float = 0.0, fim: float = 0.0) -> list[dict]:
    """Falas do trecho [inicio, fim] ja em pt-BR, com tempo relativo ao trecho.

    Lista vazia = nada a legendar (sem audio ou sem fala). Erro de transcricao ou
    de traducao sobe: legenda errada/em ingles e pior que avisar.
    """
    chave = _chave(video, inicio, fim)
    cache = _sidecar(video)
    if cache.exists():
        try:
            dados = json.loads(cache.read_text(encoding="utf-8"))
            if dados.get("chave") == chave:
                return dados["segments"]
        except (json.JSONDecodeError, KeyError):
            pass

    if not _tem_audio(video):
        resultado = {"chave": chave, "language": None, "segments": []}
        cache.write_text(json.dumps(resultado), encoding="utf-8")
        return []

    # importados aqui: carregam config/.env do dubflow, so quando ha legenda
    from app.pipeline import transcribe, translate

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if inicio:
            cmd += ["-ss", f"{inicio:.2f}"]
        if fim > inicio:
            cmd += ["-t", f"{fim - inicio:.2f}"]
        cmd += ["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                str(wav)]
        subprocess.run(cmd, check=True, capture_output=True)
        with _trava_gpu:
            log.info("transcrevendo %s", video.name)
            bruto = transcribe.transcribe(wav)

    falas = bruto.get("segments") or []
    if falas:
        falas = translate.translate_segments(
            falas, {"title": video.stem, "lang_src": bruto.get("language"),
                    "lang_dst": IDIOMA})
    resultado = {"chave": chave, "language": bruto.get("language"), "segments": falas}
    cache.write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    return falas


def escrever_ass(falas: list[dict], destino: Path, caixa: tuple[int, int, int, int],
                 tela: tuple[int, int]) -> Path:
    """ASS com a legenda no rodape da AREA DO VIDEO (o molde ocupa o resto da tela)."""
    from app.pipeline import subtitles

    x, y, w, h = caixa
    largura_tela, altura_tela = tela
    tamanho = max(30, min(72, round(w * 0.058)))
    margem_v = altura_tela - (y + h) + max(16, round(h * 0.06))
    margem_lat = x + 24
    estilo = (f"Style: Default,Arial Black,{tamanho},&H00FFFFFF,&H000000FF,&H00000000,"
              f"&HC0000000,-1,0,0,0,100,100,0,0,1,{max(2, tamanho // 14)},2,2,"
              f"{margem_lat},{margem_lat},{margem_v},1")
    por_linha = max(14, min(42, int((w - 48) / (tamanho * 0.62))))
    return subtitles.write_ass(falas, destino, width=largura_tela, height=altura_tela,
                               style=estilo, max_chars=por_linha, max_lines=2)


def filtro_subtitles(ass: Path) -> str:
    from app.pipeline import subtitles
    return f"subtitles='{subtitles._escape_for_filter(ass)}'"
