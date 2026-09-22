#!/bin/bash
# Instala/reinstala o sensor de presenca mmWave (C4001 + Sinric Pro).
# Uso: ./install.sh   (rodar de dentro da pasta clonada do repositorio)

set -e

REPO_URL="https://github.com/rtavares-g/presenca-quarto.git"
INSTALL_DIR="$HOME/presenca-quarto"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    if [ ! -d "$INSTALL_DIR" ]; then
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    cd "$INSTALL_DIR"
else
    cd "$SCRIPT_DIR"
fi

sudo apt update
sudo apt install -y python3-venv python3-pip python3-lgpio

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env

    echo
    echo "==> Configuracao do Sinric Pro (deixe em branco para pular e editar depois)"
    read -rp "Device ID: " SINRIC_DEVICE_ID
    read -rp "App Key: " SINRIC_APP_KEY
    read -rsp "App Secret: " SINRIC_APP_SECRET
    echo

    [ -n "$SINRIC_DEVICE_ID" ] && sed -i "s|^SINRIC_DEVICE_ID=.*|SINRIC_DEVICE_ID=$SINRIC_DEVICE_ID|" .env
    [ -n "$SINRIC_APP_KEY" ] && sed -i "s|^SINRIC_APP_KEY=.*|SINRIC_APP_KEY=$SINRIC_APP_KEY|" .env
    [ -n "$SINRIC_APP_SECRET" ] && sed -i "s|^SINRIC_APP_SECRET=.*|SINRIC_APP_SECRET=$SINRIC_APP_SECRET|" .env

    if [ -z "$SINRIC_DEVICE_ID" ] || [ -z "$SINRIC_APP_KEY" ] || [ -z "$SINRIC_APP_SECRET" ]; then
        echo "==> Algum campo do Sinric ficou em branco - edite depois com: nano $(pwd)/.env"
    fi
fi

if [ ! -d venv ]; then
    python3 -m venv --system-site-packages venv
fi
./venv/bin/pip install -r requirements.txt

sudo cp presenca-quarto.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now presenca-quarto
systemctl status presenca-quarto --no-pager
