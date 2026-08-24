"""Registra as DLLs CUDA do venv antes de qualquer import de ctranslate2.

No Windows, os wheels nvidia-*-cu12 colocam as DLLs em site-packages\\nvidia\\*\\bin,
que nao entra no search path do loader automaticamente. Importar este modulo
primeiro resolve o "Library cublas64_12.dll is not found".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_registered = False

# CRITICO: os cookies devolvidos por os.add_dll_directory precisam continuar
# vivos. A documentacao e explicita — "the added directory is removed when the
# returned object is closed or garbage collected". Descartar o retorno faz o
# diretorio sumir do search path assim que o GC passar, e a falha aparece muito
# depois, como `Library cublas64_12.dll is not found or cannot be loaded` na
# primeira operacao de GPU. Guardados aqui, duram o processo inteiro.
_dll_cookies: list[object] = []


def register_cuda_dlls() -> list[str]:
    """Adiciona os diretorios de DLL CUDA ao loader. Idempotente."""
    global _registered
    if _registered or sys.platform != "win32":
        return []

    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    added: list[str] = []
    if base.is_dir():
        for bin_dir in sorted(base.glob("*/bin")):
            try:
                _dll_cookies.append(os.add_dll_directory(str(bin_dir)))
                added.append(str(bin_dir))
            except OSError:
                pass

    # O PATH e o que realmente resolve o problema. O CTranslate2 carrega
    # cublas/cudnn sob demanda, na primeira operacao de GPU, e nesse caminho ele
    # NAO consulta os diretorios de os.add_dll_directory — so o PATH do processo.
    # Sem esta linha, o modelo carrega normalmente (alocando VRAM) e so entao a
    # inferencia falha com "Library cublas64_12.dll is not found or cannot be
    # loaded", o que parece falta de memoria e nao e.
    if added:
        atual = os.environ.get("PATH", "")
        faltando = [d for d in added if d not in atual]
        if faltando:
            os.environ["PATH"] = os.pathsep.join(faltando) + os.pathsep + atual

    _registered = True
    return added


register_cuda_dlls()
