# Sobe o painel e o worker em janelas separadas.
#   .\run.ps1          -> painel + worker
#   .\run.ps1 -Only web -> apenas o painel
#   .\run.ps1 -Only worker -> apenas o worker

param([ValidateSet('all', 'web', 'worker', 'bot')][string]$Only = 'all')

$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Error "venv nao encontrado em $python. Rode: py -m venv .venv"
    exit 1
}
if (-not (Test-Path (Join-Path $root '.env'))) {
    Write-Warning ".env ausente - copiando de .env.example (preencha ANTHROPIC_API_KEY)"
    Copy-Item (Join-Path $root '.env.example') (Join-Path $root '.env')
}

$env:PYTHONPATH = $root

if ($Only -in @('all', 'worker')) {
    Start-Process -FilePath $python -ArgumentList '-m', 'worker' -WorkingDirectory $root
    Write-Host "worker iniciado"
}

if ($Only -in @('all', 'bot')) {
    # Bot de vendas do Telegram (long-polling). So sobe se TELEGRAM_BOT_TOKEN estiver
    # configurado; senao ele registra o aviso e encerra sozinho.
    Start-Process -FilePath $python -ArgumentList '-m', 'bot' -WorkingDirectory $root
    Write-Host "bot de vendas iniciado"
}

if ($Only -in @('all', 'web')) {
    # Host e porta vem do .env (HOST/PORT). O padrao e 127.0.0.1: o painel
    # publica nas suas contas sociais, entao expor na rede e decisao explicita.
    $cfg = & $python -c "from app.config import settings; print(settings.host); print(settings.port)"
    $bindHost = $cfg[0]; $bindPort = $cfg[1]

    $envText = Get-Content (Join-Path $root '.env') -Raw
    if ($envText -notmatch '(?m)^DUBFLOW_PASSWORD=\S') {
        Write-Warning "DUBFLOW_PASSWORD vazia no .env - o painel vai recusar todos os logins."
    }

    Write-Host "painel em http://$bindHost`:$bindPort"
    & $python -m uvicorn app.web.main:app --host $bindHost --port $bindPort
}
