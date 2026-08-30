<#
    Abre o dubflow sem depender de ninguem: sobe o que ainda NAO esta de pe
    (worker, bot de vendas e painel) e abre o painel no navegador.

    E o alvo do atalho "Dubflow" na area de trabalho (scripts\criar_atalhos.ps1).

    O ponto importante e o guarda de processo duplicado. Dois workers na mesma
    fila brigam pelo mesmo episodio, e subir um worker novo devolve para a fila
    todo episodio em estado nao-terminal - por isso cada parte so sobe se ainda
    nao estiver rodando. Rodar o atalho duas vezes e inofensivo: a segunda vez
    so abre o navegador.

    Uso:
        .\scripts\abrir_dubflow.ps1              # sobe tudo e abre o navegador
        .\scripts\abrir_dubflow.ps1 -SemNavegador
        .\scripts\abrir_dubflow.ps1 -SemBot      # sem o bot do Telegram
        .\scripts\abrir_dubflow.ps1 -SemWorker   # so o painel: nao processa nem publica
#>
[CmdletBinding()]
param([switch]$SemNavegador, [switch]$SemBot, [switch]$SemWorker)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

function Falhar([string]$msg) {
    Write-Host ""
    Write-Host "  $msg" -ForegroundColor Red
    Write-Host ""
    # Segura a janela para o erro dar tempo de ser lido - a menos que nao haja
    # console (agendador), onde Read-Host lanca.
    try { Read-Host "  (enter para fechar)" | Out-Null } catch { Start-Sleep -Seconds 5 }
    exit 1
}

if (-not (Test-Path $python)) { Falhar "venv nao encontrado em $python. Rode: py -m venv .venv" }
if (-not (Test-Path (Join-Path $root '.env'))) { Falhar ".env ausente em $root. Copie de .env.example e preencha." }

$env:PYTHONPATH = $root

# Processos do dubflow ja de pe. O python.exe do venv e um shim: ele aparece com
# a mesma linha de comando do processo real, entao casar por '-m worker' pega os
# dois - o que serve, aqui so interessa saber se JA existe algum.
function Processos([string]$padrao) {
    @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $padrao })
}

function PortaAberta([int]$porta) {
    $cliente = New-Object Net.Sockets.TcpClient
    try { $cliente.Connect('127.0.0.1', $porta); return $true }
    catch { return $false }
    finally { $cliente.Dispose() }
}

Write-Host ""
Write-Host "  dubflow" -ForegroundColor Cyan
Write-Host "  $root" -ForegroundColor DarkGray
Write-Host ""

# HOST/PORT vem do .env, pela mesma leitura que o app faz.
try {
    $cfg = & $python -c "from app.config import settings; print(settings.host); print(settings.port)"
} catch { Falhar "nao consegui ler a configuracao (app/config.py): $_" }
if ($LASTEXITCODE -ne 0 -or $cfg.Count -lt 2) { Falhar "nao consegui ler HOST/PORT do .env" }
$bindHost = $cfg[0].Trim()
$porta = [int]$cfg[1].Trim()
$url = "http://127.0.0.1:$porta"

# --- worker -----------------------------------------------------------------
# O worker publica sozinho toda publicacao agendada cuja hora ja passou. Se a
# maquina ficou dias desligada, subir o worker dispara a fila atrasada INTEIRA
# de uma vez - varios videos no ar no mesmo minuto, que e o oposto do gotejamento.
# Por isso: so avisa e pergunta quando existe atraso; no dia a dia nao aparece.
function PublicacoesVencidas {
    try {
        $saida = & $python (Join-Path $root 'scripts\status_fila.py') 'vencidas'
        return [int]("$saida".Trim())
    } catch { return 0 }
}

if ($SemWorker) {
    Write-Host "  worker      pulado (-SemWorker): nada e processado nem publicado" -ForegroundColor DarkGray
} elseif ((Processos '-m\s+worker').Count -gt 0) {
    Write-Host "  worker      ja estava de pe" -ForegroundColor DarkGray
} else {
    $vencidas = PublicacoesVencidas
    $subirWorker = $true
    if ($vencidas -gt 0) {
        Write-Host ""
        Write-Host "  ATENCAO: $vencidas publicacao(oes) agendada(s) com a hora ja vencida." -ForegroundColor Yellow
        Write-Host "  Ao subir, o worker publica todas de uma vez nos canais conectados." -ForegroundColor Yellow
        # Sem console para perguntar (agendador, -NonInteractive), o silencio NAO
        # autoriza: publicar e irreversivel, o worker fica de fora.
        $resposta = ''
        try { $resposta = Read-Host "  Subir o worker mesmo assim? (s/N)" } catch { $resposta = '' }
        $subirWorker = $resposta -match '^[sSyY]'
        Write-Host ""
    }
    if ($subirWorker) {
        Start-Process -FilePath $python -ArgumentList '-m', 'worker' -WorkingDirectory $root -WindowStyle Minimized
        Write-Host "  worker      iniciado" -ForegroundColor Green
    } else {
        Write-Host "  worker      NAO subiu - reagende ou apague as publicacoes atrasadas no painel" -ForegroundColor Yellow
    }
}

# --- bot de vendas ----------------------------------------------------------
if ($SemBot) {
    Write-Host "  bot         pulado (-SemBot)" -ForegroundColor DarkGray
} elseif ((Processos '-m\s+bot').Count -gt 0) {
    Write-Host "  bot         ja estava de pe" -ForegroundColor DarkGray
} else {
    # Sem TELEGRAM_BOT_TOKEN ele avisa e encerra sozinho - nao custa tentar.
    Start-Process -FilePath $python -ArgumentList '-m', 'bot' -WorkingDirectory $root -WindowStyle Minimized
    Write-Host "  bot         iniciado" -ForegroundColor Green
}

# --- painel -----------------------------------------------------------------
if (PortaAberta $porta) {
    Write-Host "  painel      ja estava de pe em $url" -ForegroundColor DarkGray
} else {
    Start-Process -FilePath $python -WorkingDirectory $root -WindowStyle Minimized `
        -ArgumentList '-m', 'uvicorn', 'app.web.main:app', '--host', $bindHost, '--port', "$porta"
    Write-Host "  painel      subindo em $url ..." -ForegroundColor Green

    $limite = (Get-Date).AddSeconds(60)
    while (-not (PortaAberta $porta)) {
        if ((Get-Date) -gt $limite) {
            Falhar "o painel nao respondeu em 60s. Rode '.\run.ps1 -Only web' numa janela para ver o erro."
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "  painel      no ar" -ForegroundColor Green
}

$envText = Get-Content (Join-Path $root '.env') -Raw
if ($envText -notmatch '(?m)^DUBFLOW_PASSWORD=\S') {
    Write-Host ""
    Write-Host "  aviso: DUBFLOW_PASSWORD vazia no .env - o painel vai recusar todos os logins." -ForegroundColor Yellow
}

if (-not $SemNavegador) { Start-Process $url }

Write-Host ""
Write-Host "  pronto: $url" -ForegroundColor Cyan
Write-Host "  para desligar tudo, use o atalho 'Parar Dubflow'." -ForegroundColor DarkGray
Start-Sleep -Seconds 3
