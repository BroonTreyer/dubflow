"""Etapa 2: transcricao com faster-whisper, isolada em subprocesso.

**Por que subprocesso.** Esta placa tem 8 GB de VRAM compartilhados com o resto
do sistema. Quando falta memoria, o CTranslate2 se comporta de duas formas:

1. levanta `Library cublas64_12.dll is not found or cannot be loaded` — mensagem
   enganosa, que parece problema de instalacao e e falta de memoria; ou
2. **nao levanta nada e fica esperando** memoria que nunca chega.

O caso (2) e o perigoso: nenhum `try/except` o alcanca, e uma thread presa em
codigo nativo nao pode ser interrompida. Rodando em processo separado, o
travamento vira um timeout que o pai resolve matando o filho — o que ainda
devolve toda a VRAM de uma vez.

A cascata de modos degrada a qualidade em vez de desistir: float16 -> int8_float16
-> CPU. E a VRAM livre e consultada antes, para nem tentar um modo que
comprovadamente nao cabe.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

# VRAM necessaria por modo, em MB: pesos do modelo mais a area de trabalho das
# operacoes. Medido nesta placa; float16 pede bem mais do que o tamanho do modelo.
VRAM_NEEDED = {
    ("cuda", "float16"): 7000,
    ("cuda", "bfloat16"): 7000,
    ("cuda", "float32"): 8000,
    ("cuda", "int8_float16"): 3500,
    ("cuda", "int8"): 3000,
}

# Sem nenhuma saida por este tempo, consideramos travado. Precisa ser maior que a
# carga do modelo (~30-150s) para nao matar um processo que so esta comecando.
SILENCE_TIMEOUT = 360

# Multiplicadores sobre a duracao do audio para o teto total de cada modo.
TIME_BUDGET = {"cuda": 3.0, "cpu": 25.0}


def free_vram_mb() -> int | None:
    """VRAM livre segundo o driver. None quando nao ha GPU NVIDIA visivel."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0:
            return int(out.stdout.strip().splitlines()[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def audio_duration(audio_path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _modes(free_vram: int | None) -> list[tuple[str, str]]:
    """Modos viaveis, do melhor ao mais economico.

    Um modo que nao cabe na VRAM livre e descartado aqui em vez de falhar depois:
    tentar assim mesmo custa minutos ate travar, e o travamento e o pior caso.
    """
    preferido = (settings.whisper_device, settings.whisper_compute)
    cascata = [preferido, ("cuda", "int8_float16"), ("cpu", "int8")]

    saida: list[tuple[str, str]] = []
    for modo in cascata:
        if modo in saida:
            continue
        device, _ = modo
        if device == "cuda":
            if preferido[0] == "cpu":
                continue  # configuracao pede CPU explicitamente
            precisa = VRAM_NEEDED.get(modo, 4000)
            if free_vram is not None and free_vram < precisa:
                log.info("pulando %s/%s: precisa de ~%d MB, ha %d MB livres",
                         *modo, precisa, free_vram)
                continue
        saida.append(modo)

    if not saida:
        saida = [("cpu", "int8")]
    return saida


def _run_subprocess(audio_path: Path, device: str, compute_type: str,
                    total_timeout: float) -> dict[str, Any]:
    """Executa a transcricao isolada. Mata o filho se travar ou estourar o tempo."""
    out_file = Path(tempfile.mkdtemp(prefix="dubflow_tr_")) / "result.json"

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.pipeline._transcribe_worker",
         str(audio_path), device, compute_type, str(out_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(settings.root),
    )

    ultimo_sinal = time.monotonic()
    ultimo_progresso = {"segmentos": 0, "ate": 0.0}
    # Guardar o fim do stderr e o que torna uma falha diagnosticavel: sem isso,
    # "codigo 1" nao diz se foi VRAM, driver, modelo ausente ou audio corrompido.
    diagnostico: list[str] = []

    def ler_stderr() -> None:
        nonlocal ultimo_sinal
        for linha in proc.stderr:  # type: ignore[union-attr]
            ultimo_sinal = time.monotonic()
            if linha.startswith("progress "):
                try:
                    _, n, ate = linha.split()
                    ultimo_progresso["segmentos"] = int(n)
                    ultimo_progresso["ate"] = float(ate)
                except ValueError:
                    pass
            else:
                diagnostico.append(linha.rstrip())
                del diagnostico[:-40]  # so as ultimas linhas interessam

    leitor = threading.Thread(target=ler_stderr, daemon=True)
    leitor.start()

    inicio = time.monotonic()
    while proc.poll() is None:
        agora = time.monotonic()
        if agora - ultimo_sinal > SILENCE_TIMEOUT:
            proc.kill()
            raise TimeoutError(
                f"sem sinal de vida por {SILENCE_TIMEOUT}s em {device}/{compute_type} "
                f"(ultimo progresso: {ultimo_progresso['segmentos']} segmentos)"
            )
        if agora - inicio > total_timeout:
            proc.kill()
            raise TimeoutError(
                f"excedeu {total_timeout / 60:.0f} min em {device}/{compute_type} "
                f"(ultimo progresso: {ultimo_progresso['segmentos']} segmentos)"
            )
        time.sleep(2)

    if proc.returncode != 0 or not out_file.exists():
        leitor.join(timeout=5)
        causa = " | ".join(l for l in diagnostico[-6:] if l.strip()) or "sem saida de erro"
        raise RuntimeError(
            f"transcricao em {device}/{compute_type} terminou com codigo "
            f"{proc.returncode}: {causa}"
        )

    resultado = json.loads(out_file.read_text(encoding="utf-8"))
    try:
        out_file.unlink()
        out_file.parent.rmdir()
    except OSError:
        pass
    return resultado


def transcribe(audio_path: Path) -> dict[str, Any]:
    """Transcreve o audio, degradando o modo quando faltar memoria de GPU.

    Devolve segmentos com timestamps por palavra — o que permite cortar em
    fronteira de fala e montar legenda estilo social depois.
    """
    duracao = audio_duration(audio_path)
    livre = free_vram_mb()
    modos = _modes(livre)

    log.info(
        "transcrevendo %.0f min de audio | VRAM livre: %s | modos: %s",
        duracao / 60,
        f"{livre} MB" if livre is not None else "sem GPU",
        " -> ".join(f"{d}/{c}" for d, c in modos),
    )

    ultimo_erro: Exception | None = None
    for i, (device, compute_type) in enumerate(modos):
        # Teto generoso: serve para destravar, nao para apertar o processamento.
        timeout = max(900.0, duracao * TIME_BUDGET.get(device, 3.0))
        try:
            inicio = time.monotonic()
            resultado = _run_subprocess(audio_path, device, compute_type, timeout)
            resultado["mode"] = f"{device}/{compute_type}"
            log.info(
                "transcricao concluida em %s: %d segmentos em %.1f min",
                resultado["mode"], len(resultado["segments"]),
                (time.monotonic() - inicio) / 60,
            )
            if i:
                log.warning("modo reduzido foi usado (%s)", resultado["mode"])
            return resultado

        except (TimeoutError, RuntimeError) as exc:
            ultimo_erro = exc
            restante = modos[i + 1:]
            log.warning(
                "modo %s/%s falhou: %s.%s",
                device, compute_type, exc,
                f" Tentando {restante[0][0]}/{restante[0][1]}." if restante else "",
            )

    raise RuntimeError(
        f"transcricao falhou em todos os modos. Ultimo erro: {ultimo_erro}. "
        "Se a mensagem cita cublas/cudnn ou houve travamento, a causa costuma ser "
        "VRAM ocupada por outro programa (jogo, navegador), nao a instalacao do CUDA."
    ) from ultimo_erro


def unload() -> None:
    """Mantido por compatibilidade: o subprocesso ja libera tudo ao terminar."""
    return None
