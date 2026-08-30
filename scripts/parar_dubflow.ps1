<#
    Desliga o dubflow: worker, bot de vendas e painel.

    Alvo do atalho "Parar Dubflow" na area de trabalho.

    Antes de matar, avisa se algum episodio esta a meio caminho. Matar o worker
    no meio de um episodio nao corrompe nada, mas ao subir de novo ele devolve
    para a fila todo episodio em estado nao-terminal - ou seja, aquele episodio
    volta do zero (download e transcricao inclusos).
#>
[CmdletBinding()]
param([switch]$Forcar)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$env:PYTHONPATH = $root

function Processos([string]$padrao) {
    @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $padrao })
}

Write-Host ""
Write-Host "  dubflow - desligando" -ForegroundColor Cyan
Write-Host ""

# Episodio a meio caminho? So avisa; a decisao e do usuario.
if (-not $Forcar -and (Test-Path $python)) {
    # try/catch porque no PS 5.1 qualquer linha em stderr de um .exe vira
    # ErrorRecord e, com ErrorActionPreference=Stop, derrubaria o script inteiro
    # so por causa de um aviso do sqlite.
    $emCurso = ''
    try { $emCurso = & $python (Join-Path $root 'scripts\status_fila.py') 'andamento' }
    catch { $emCurso = '' }
    if ($emCurso -and "$emCurso".Trim()) {
        Write-Host "  ATENCAO: episodio(s) em andamento: $emCurso" -ForegroundColor Yellow
        Write-Host "  Ao religar, o worker recomeca esse(s) episodio(s) do zero." -ForegroundColor Yellow
        # Sem console para perguntar, desliga: parar nao destroi nada, so custa
        # reprocessar o episodio que estava no meio.
        $resposta = 's'
        try { $resposta = Read-Host "  Desligar mesmo assim? (s/N)" } catch { $resposta = 's' }
        if ($resposta -notmatch '^[sSyY]') { Write-Host "  cancelado."; Start-Sleep -Seconds 2; exit 0 }
    }
}

$alvos = @{
    'painel' = 'uvicorn\s+app\.web\.main'
    'worker' = '-m\s+worker'
    'bot'    = '-m\s+bot'
}

$mortos = 0
foreach ($nome in @('painel', 'worker', 'bot')) {
    $procs = Processos $alvos[$nome]
    if (-not $procs.Count) {
        Write-Host "  $nome`t nao estava rodando" -ForegroundColor DarkGray
        continue
    }
    foreach ($p in $procs) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $mortos++ }
        catch { Write-Host "  $nome`t nao consegui parar o PID $($p.ProcessId): $_" -ForegroundColor Red }
    }
    Write-Host "  $nome`t parado ($($procs.Count) processo(s))" -ForegroundColor Green
}

Write-Host ""
Write-Host "  $mortos processo(s) encerrado(s)." -ForegroundColor Cyan
Start-Sleep -Seconds 3
