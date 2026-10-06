<#
    Abre o Painel de Filmes (porta 8030) sem depender de terminal nenhum.

    O painel roda escondido (pythonw) e independente: fechar o navegador, o
    terminal ou o Claude Code nao derruba ele. Se ja estiver de pe, so abre o
    navegador - rodar duas vezes e inofensivo.

    Log em filmes\painel.log (e la que se olha quando "fica off").

    Uso:
        .\scripts\abrir_painel_filmes.ps1
        .\scripts\abrir_painel_filmes.ps1 -CriarAtalho    # atalho na area de trabalho
        .\scripts\abrir_painel_filmes.ps1 -Reiniciar      # derruba e sobe de novo
        .\scripts\abrir_painel_filmes.ps1 -IniciarComWindows  # sobe sozinho no login

    Iniciar com o Windows importa por causa do Instagram: quem publica a fila
    agendada e o painel. Painel fechado = post que nao sai na hora.
    Para desfazer: apagar 'Painel de Filmes.lnk' da pasta shell:startup.
#>
[CmdletBinding()]
param([switch]$CriarAtalho, [switch]$Reiniciar, [switch]$SemNavegador, [switch]$IniciarComWindows)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$porta = 8035   # 8030 e do painel principal do dubflow
$url = "http://127.0.0.1:$porta"

function NovoAtalho([string]$pasta, [string]$extra) {
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $pasta 'Painel de Filmes.lnk'))
    $lnk.TargetPath = $powershell
    $lnk.Arguments = ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" " + $extra).Trim()
    $lnk.WorkingDirectory = $root
    $lnk.Description = 'Painel do molde de filmes'
    $icone = Join-Path $root 'assets\dubflow.ico'
    if (Test-Path $icone) { $lnk.IconLocation = "$icone,0" }
    $lnk.Save()
}

if ($CriarAtalho) {
    NovoAtalho ([Environment]::GetFolderPath('Desktop')) ''
    Write-Host "  atalho criado na area de trabalho: Painel de Filmes" -ForegroundColor Green
    return
}

if ($IniciarComWindows) {
    NovoAtalho ([Environment]::GetFolderPath('Startup')) '-SemNavegador'
    Write-Host "  o painel vai subir sozinho no login (pasta Inicializar)" -ForegroundColor Green
    return
}

function PortaAberta {
    $cliente = New-Object Net.Sockets.TcpClient
    try { $cliente.Connect('127.0.0.1', $porta); return $true }
    catch { return $false }
    finally { $cliente.Dispose() }
}

function Processos {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'painel_filmes' })
}

if ($Reiniciar) {
    Processos | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 800
}

if (-not (PortaAberta)) {
    if (-not (Test-Path $pythonw)) { throw "venv nao encontrado: $pythonw" }
    # instancia velha (outra porta, travada) nao pode ficar disputando a pasta filmes    Processos | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $env:PYTHONPATH = $root
    Start-Process -FilePath $pythonw -ArgumentList '-m', 'scripts.painel_filmes', '--sem-navegador' `
        -WorkingDirectory $root -WindowStyle Hidden
    foreach ($i in 1..30) {
        Start-Sleep -Milliseconds 300
        if (PortaAberta) { break }
    }
    if (-not (PortaAberta)) {
        throw "o painel nao subiu; veja $(Join-Path $root 'filmes\painel.log')"
    }
}

if (-not $SemNavegador) { Start-Process $url }
