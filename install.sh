#!/bin/bash
# Instala/reinstala o sensor de presenca mmWave (C4001 + Home Assistant via MQTT).
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
sudo apt install -y python3-venv python3-pip python3-lgpio python3-serial python3-smbus

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
fi

MQTT_CONFIG="$HOME/.config/mqtt-ha.json"
if [ ! -f "$MQTT_CONFIG" ]; then
    echo
    echo "==> MQTT do Home Assistant (add-on Mosquitto; usuario definido nas opcoes do add-on)"
    read -rp "Host do HA [192.168.1.211]: " MQTT_HOST
    read -rp "Usuario MQTT: " MQTT_USUARIO
    read -rsp "Senha MQTT: " MQTT_SENHA
    echo
    mkdir -p "$HOME/.config"
    ( umask 077; printf '{"host": "%s", "port": 1883, "usuario": "%s", "senha": "%s"}\n' \
        "${MQTT_HOST:-192.168.1.211}" "$MQTT_USUARIO" "$MQTT_SENHA" > "$MQTT_CONFIG" )
fi

if [ ! -d venv ]; then
    python3 -m venv --system-site-packages venv
fi
./venv/bin/pip install -r requirements.txt

sed -e "s|__USER__|$USER|g" -e "s|__HOME__|$HOME|g" presenca-quarto.service | sudo tee /etc/systemd/system/presenca-quarto.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now presenca-quarto
systemctl status presenca-quarto --no-pager

echo
read -rp "Configurar HTTPS com Nginx + Let's Encrypt agora? Requer dominio ja apontando pro IP publico e portas 80/443 liberadas no roteador [s/N]: " CONFIGURAR_HTTPS
if [[ "$CONFIGURAR_HTTPS" =~ ^[sS] ]]; then
    read -rp "Dominio (ex: presenca-quarto.seudominio.com): " DOMINIO
    read -rp "E-mail para avisos do Let's Encrypt [gtavares.r@icloud.com]: " EMAIL_CERTBOT
    EMAIL_CERTBOT="${EMAIL_CERTBOT:-gtavares.r@icloud.com}"

    sudo apt install -y nginx certbot python3-certbot-nginx

    NGINX_CONF="/etc/nginx/sites-available/$DOMINIO"
    sudo tee "$NGINX_CONF" > /dev/null <<EOF
server {
    listen 80;
    server_name $DOMINIO;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # obrigatorio para os WebSockets (/logs, /presenca-ws, /sensor-ws)
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
EOF

    sudo ln -sf "$NGINX_CONF" "/etc/nginx/sites-enabled/$DOMINIO"
    sudo nginx -t
    sudo systemctl reload nginx

    sudo certbot --nginx -d "$DOMINIO" --non-interactive --agree-tos -m "$EMAIL_CERTBOT" --redirect

    echo "==> HTTPS configurado. Teste com: curl -I https://$DOMINIO/"
fi
