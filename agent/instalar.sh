#!/usr/bin/env bash
# Instala o agente em um mini PC com Linux (Ubuntu ou Debian).
# Uso: sudo VIGIA_SERVER=https://... AGENT_TOKEN=... bash instalar.sh
set -euo pipefail

: "${VIGIA_SERVER:?Defina VIGIA_SERVER com o endereço do servidor}"
: "${AGENT_TOKEN:?Defina AGENT_TOKEN com o token do servidor}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Instalando Docker..."
  curl -fsSL https://get.docker.com | sh
fi

cd "$(dirname "$0")"
umask 077
cat > .env <<EOF
VIGIA_SERVER=${VIGIA_SERVER}
AGENT_TOKEN=${AGENT_TOKEN}
EOF

docker compose up -d --build
echo "Agente no ar. Ele volta sozinho se o equipamento reiniciar."
echo "Para acompanhar: docker compose logs -f"
