"""Notificacao do Windows (o balao no canto da tela), sem modulo extra.

Usa a API de toast do proprio Windows pelo PowerShell 5.1. O texto vai por
variavel de ambiente, nunca colado no script: aspas ou $ no texto nao viram
codigo. Falhar aqui nunca derruba quem chamou.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("notificar")

_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $xml.GetElementsByTagName('text')
$t.Item(0).AppendChild($xml.CreateTextNode($env:AVISO_TITULO)) > $null
$t.Item(1).AppendChild($xml.CreateTextNode($env:AVISO_TEXTO)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def windows(titulo: str, texto: str) -> bool:
    if sys.platform != "win32":
        return False
    env = {**os.environ, "AVISO_TITULO": titulo[:120], "AVISO_TEXTO": texto[:400]}
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", _SCRIPT],
            env=env, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            log.warning("notificacao falhou: %s", r.stderr.decode(errors="replace")[:200])
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("notificacao falhou: %s", exc)
        return False
