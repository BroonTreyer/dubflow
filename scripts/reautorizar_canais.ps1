<#
    Reautoriza os canais do YouTube, um a um, no escopo completo
    (youtube.upload + youtube.readonly).

        .\scripts\reautorizar_canais.ps1              # todos os canais YouTube
        .\scripts\reautorizar_canais.ps1 -Canais 6,10 # so estes
        .\scripts\reautorizar_canais.ps1 -SoConferir  # nao reautoriza, so audita

    Por que reautorizar: o token antigo so tem `youtube.upload`. Ele publica, mas
    nao le a identidade do canal - por isso o painel mostra "Canal YT 1" em vez do
    nome real, e por isso o diagnostico de falha de publicacao fica cego.

    Cada canal e uma conta diferente do Google. O navegador guarda a sessao da
    conta anterior, entao ANTES de cada canal o script para e espera voce
    confirmar - use o seletor de contas do Google, ou uma janela anonima, e
    confira o nome do canal na tela de autorizacao. Autorizar o canal 7 logado na
    conta do 6 grava o token errado no cofre, e o corte vai para o canal errado.

    Precisa de console interativo: o Google pede login no navegador.
#>
[CmdletBinding()]
param([int[]]$Canais, [switch]$SoConferir)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$env:PYTHONPATH = $root
$env:PYTHONIOENCODING = 'utf-8'

if (-not (Test-Path $python)) { throw "venv nao encontrado em $python" }

function Auditar {
    Write-Host ""
    Write-Host "  --- saude e identidade de cada canal ---" -ForegroundColor Cyan
    & $python (Join-Path $root 'scripts\channel_identity.py')
    Write-Host ""
}

if (-not $Canais) {
    $lista = & $python -c "from app import db; print(' '.join(str(c['id']) for c in db.list_channels(platform='youtube')))"
    $Canais = @("$lista".Trim() -split '\s+' | Where-Object { $_ } | ForEach-Object { [int]$_ })
}

Write-Host ""
Write-Host "  Reautorizacao de canais do YouTube" -ForegroundColor Cyan
Write-Host "  canais: $($Canais -join ', ')" -ForegroundColor DarkGray

Auditar

if ($SoConferir) { Write-Host "  (-SoConferir: nada foi reautorizado)" -ForegroundColor DarkGray; return }

$feitos = @(); $pulados = @()
foreach ($id in $Canais) {
    $nome = & $python -c "from app import db; c=db.get_channel($id); print(c['name'] if c else '')"
    Write-Host ""
    Write-Host "  ============================================================" -ForegroundColor Cyan
    Write-Host "  Canal $id - $nome" -ForegroundColor Cyan
    Write-Host "  ============================================================" -ForegroundColor Cyan
    Write-Host "  Entre na conta do Google DESTE canal. Se o navegador entrar"
    Write-Host "  direto na conta anterior, troque no seletor de contas ou abra"
    Write-Host "  uma janela anonima e cole a URL que o script imprime."
    $r = Read-Host "  [Enter] para autorizar, 'p' para pular, 'x' para parar aqui"
    if ($r -match '^[xX]') { Write-Host "  parando por aqui." -ForegroundColor Yellow; break }
    if ($r -match '^[pP]') { $pulados += $id; continue }

    & $python -m scripts.youtube_auth --channel $id
    if ($LASTEXITCODE -eq 0) {
        $feitos += $id
        Write-Host "  canal $id reautorizado" -ForegroundColor Green
    } else {
        $pulados += $id
        Write-Host "  canal $id NAO concluiu (codigo $LASTEXITCODE)" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "  reautorizados: $($feitos -join ', ')" -ForegroundColor Green
if ($pulados) { Write-Host "  pendentes    : $($pulados -join ', ')" -ForegroundColor Yellow }

Auditar

Write-Host "  Se a identidade ja aparece, grave os nomes reais no painel com:" -ForegroundColor DarkGray
Write-Host "      .venv\Scripts\python.exe scripts\channel_identity.py --apply" -ForegroundColor DarkGray
Write-Host ""
