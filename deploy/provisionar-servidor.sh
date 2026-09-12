#!/usr/bin/env bash
# Prepara um servidor Ubuntu 24.04 para rodar o OfertaFlow inteiro e a metade
# PUBLICADORA do dubflow. Sem GPU: o PC transcreve e renderiza, o servidor posta.
#
# Roda uma vez, como root, numa maquina limpa. E idempotente: repetir nao quebra.
#
#   sudo bash provisionar-servidor.sh
#
# O que ele NAO faz, de proposito: copiar os dados (44 GB), os .env com as chaves,
# e os tokens do YouTube. Segredo e dado vao em passo separado, manual — ver o
# README deste diretorio.
set -euo pipefail

msg() { echo; echo "=== $* ==="; }

[ "$(id -u)" = "0" ] || { echo "rode como root (sudo)"; exit 1; }

msg "Pacotes base"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  curl wget git unzip ca-certificates gnupg \
  python3 python3-venv python3-pip \
  ffmpeg \
  ufw

# SEM GPU AQUI, de proposito.
#
# A arquitetura e hibrida: o PC transcreve (faster-whisper na GPU) e renderiza;
# o servidor so publica. O unico consumidor de GPU do dubflow e a transcricao —
# o render ja e CPU pura (libx264 em clips.py e subtitles.py), entao nada aqui
# precisa de placa.
#
# Se um dia a transcricao vier para ca, e aqui que entra driver + CUDA.

msg "Node 22 (OfertaFlow)"
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -c2-3)" -lt 22 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -qq nodejs
fi
node -v

msg "deno (runtime JS que o yt-dlp exige para o YouTube)"
# Sem ele o yt-dlp degrada ate "Sign in to confirm you're not a bot" — foi o que
# reprovou 16 episodios em agosto. Instalado em /opt para o systemd enxergar: o
# instalador padrao escreve o PATH no ~/.bashrc, que servico nao le.
if [ ! -x /opt/deno/bin/deno ]; then
  DENO_INSTALL=/opt/deno bash -c "curl -fsSL https://deno.land/install.sh | sh -s -- -y"
fi
ln -sf /opt/deno/bin/deno /usr/local/bin/deno
deno --version | head -1

# Chrome, desktop e VNC entram SO com --com-navegador.
#
# Medido em 12/09/2026: o Mercado Livre bloqueia as paginas de loja de marca
# vindas deste datacenter (41 KB, zero produto) e libera as mesmas paginas do IP
# residencial do dono (1,59 MB, 96 produtos). Mesmo Chrome, mesma versao, perfil
# limpo dos dois lados — a unica variavel era o IP.
#
# Por isso a metade "navegador" do OfertaFlow (colheita e gerador de link) fica
# no PC, e o servidor so publica. Sem navegador aqui: menos 1 GB de pacotes,
# nenhuma sessao grafica exposta e nada de VNC para proteger.
if [ "${1:-}" = "--com-navegador" ]; then
  msg "Google Chrome + desktop minimo (opcional)"
  if ! command -v google-chrome >/dev/null 2>&1; then
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub       | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main"       > /etc/apt/sources.list.d/google-chrome.list
    apt-get update -qq
    apt-get install -y -qq google-chrome-stable
  fi
  apt-get install -y -qq xfce4 xfce4-goodies tigervnc-standalone-server dbus-x11
  google-chrome --version
else
  msg "Sem navegador (padrao)"
  echo "  A colheita e o gerador de link ficam no PC — ver o comentario acima."
  echo "  Para instalar mesmo assim: bash provisionar-servidor.sh --com-navegador"
fi

msg "Usuarios de servico"
id dubflow    >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/dubflow    --shell /bin/bash dubflow
id ofertaflow >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/ofertaflow --shell /bin/bash ofertaflow
# Sem grupo de GPU: o worker daqui so faz upload.

msg "Firewall"
# Nada de painel exposto: 8030 e 3000 escutam em 127.0.0.1 e o acesso e por
# tunel SSH. Eles publicam nas suas contas — porta aberta e a operacao inteira
# na mao de quem varrer o IP.
ufw allow OpenSSH
ufw --force enable
ufw status verbose

msg "Pronto"
cat <<'FIM'
Falta o que este script NAO faz de proposito:

  1. Copiar o codigo para /opt/dubflow e /opt/ofertaflow
  2. Copiar os .env (chaves da Anthropic, Z-API, Telegram)
  3. Copiar data/ — 44 GB no dubflow
  4. Reautorizar os canais do YouTube (o token nao viaja entre maquinas)
  5. Logar no painel de afiliados do Mercado Livre pelo VNC, uma vez

Os passos estao no README deste diretorio, em ordem.
FIM
