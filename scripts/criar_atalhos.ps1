<#
    Cria (ou refaz) os atalhos do dubflow na area de trabalho:

        Dubflow          -> sobe worker + bot + painel e abre o navegador
        Parar Dubflow    -> desliga os tres

    Rode uma vez, por maquina:
        .\scripts\criar_atalhos.ps1

    A area de trabalho vem do proprio Windows ([Environment]::GetFolderPath),
    entao funciona igual com a pasta redirecionada para o OneDrive.
#>
[CmdletBinding()]
param([string]$Destino)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

if (-not $Destino) { $Destino = [Environment]::GetFolderPath('Desktop') }
if (-not (Test-Path $Destino)) { throw "pasta de destino nao existe: $Destino" }

$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shell = New-Object -ComObject WScript.Shell

function Novo-Atalho([string]$nome, [string]$script, [string]$icone, [string]$descricao) {
    $caminho = Join-Path $Destino "$nome.lnk"
    $lnk = $shell.CreateShortcut($caminho)
    $lnk.TargetPath = $powershell
    # -ExecutionPolicy Bypass: a politica da maquina costuma barrar .ps1 clicado.
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $root $script)`""
    $lnk.WorkingDirectory = $root
    $lnk.Description = $descricao
    $lnk.WindowStyle = 1
    $arquivoIcone = Join-Path $root $icone
    if (Test-Path $arquivoIcone) { $lnk.IconLocation = "$arquivoIcone,0" }
    $lnk.Save()
    Write-Host "  criado: $caminho" -ForegroundColor Green
}

Write-Host ""
Novo-Atalho 'Dubflow' 'scripts\abrir_dubflow.ps1' 'assets\dubflow.ico' `
    'Sobe o worker, o bot e o painel do dubflow e abre no navegador'
Novo-Atalho 'Parar Dubflow' 'scripts\parar_dubflow.ps1' 'assets\dubflow_parar.ico' `
    'Desliga o worker, o bot e o painel do dubflow'
Write-Host ""
Write-Host "  pronto. Se o icone nao aparecer na hora, e o cache do Windows - ele atualiza sozinho." -ForegroundColor DarkGray
Write-Host ""
