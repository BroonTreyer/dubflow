#!/usr/bin/env bash
# Recusa subir o worker quando ha publicacao agendada com a hora vencida.
#
# No Windows isto era uma PERGUNTA no atalho da area de trabalho. Num servico do
# systemd nao ha ninguem para perguntar, e o silencio nao pode autorizar: subir o
# worker com a fila atrasada joga tudo no ar no mesmo minuto, e publicar e
# irreversivel. Entao aqui a falta de resposta vira recusa, com o motivo no journal.
#
# Em servidor sempre ligado isso quase nunca dispara. Ele existe para o depois de
# um reboot longo ou de uma queda do provedor, que e exatamente quando o estrago
# aconteceria sem ninguem olhando.
#
# Para liberar de proposito (voce conferiu e quer publicar as atrasadas):
#     sudo systemctl set-environment DUBFLOW_IGNORAR_VENCIDAS=1
#     sudo systemctl start dubflow-worker
#     sudo systemctl unset-environment DUBFLOW_IGNORAR_VENCIDAS
set -euo pipefail

RAIZ="${DUBFLOW_HOME:-/opt/dubflow}"
LIMITE="${DUBFLOW_LIMITE_VENCIDAS:-10}"

if [ "${DUBFLOW_IGNORAR_VENCIDAS:-0}" = "1" ]; then
  echo "guarda-vencidas: ignorada por DUBFLOW_IGNORAR_VENCIDAS=1"
  exit 0
fi

cd "$RAIZ"
# O status_fila.py imprime 0 quando o banco ainda nao existe, entao instalacao
# nova passa reto em vez de travar na primeira subida.
VENCIDAS="$(PYTHONPATH="$RAIZ" "$RAIZ/.venv/bin/python" "$RAIZ/scripts/status_fila.py" vencidas 2>/dev/null || echo 0)"
VENCIDAS="${VENCIDAS//[^0-9]/}"
VENCIDAS="${VENCIDAS:-0}"

if [ "$VENCIDAS" -gt "$LIMITE" ]; then
  echo "guarda-vencidas: $VENCIDAS publicacoes com a hora vencida (limite $LIMITE)."
  echo "  Subir agora publicaria todas de uma vez, e a cota da YouTube Data API"
  echo "  (~6 uploads/dia somando TODOS os canais) transformaria o excedente em"
  echo "  'failed' definitivo em ~15 minutos."
  echo "  Reespace a fila antes:  .venv/bin/python -m scripts.reagendar_fila --apply"
  exit 1
fi

echo "guarda-vencidas: $VENCIDAS vencidas, dentro do limite ($LIMITE). Liberado."
