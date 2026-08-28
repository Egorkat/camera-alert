#!/bin/bash
# Camera Alert - installer
#
# Run as root (or via sudo) from the cloned repo directory, which must be
# /opt/camera-alert (paths are hardcoded in config.py).
#
#   cd /opt/camera-alert && sudo ./install.sh
set -euo pipefail

REPO_DIR="/opt/camera-alert"
ENV_FILE="/etc/camera-alert.env"

if [[ "$EUID" -ne 0 ]]; then
    echo "Run this script as root (sudo ./install.sh)" >&2
    exit 1
fi

if [[ "$(pwd)" != "$REPO_DIR" ]]; then
    echo "Must be run from $REPO_DIR (config.py hardcodes this path)" >&2
    exit 1
fi

echo "==> Installing system dependencies"
apt-get update -qq
apt-get install -y python3 python3-requests python3-urllib3 ffmpeg

echo "==> Creating data directories"
mkdir -p "$REPO_DIR/snapshots" "$REPO_DIR/clips"

echo "==> Writing environment file template"
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" << 'EOF'
# Camera Alert secrets - fill these in, then: systemctl restart camera-listener camera-worker camera-bot
CAMERA_BOT_TOKEN=changeme
CAMERA_CHAT_ID=changeme
CAMERA_USER=admin
CAMERA_PASS=changeme
EOF
    chmod 600 "$ENV_FILE"
    echo "    Created $ENV_FILE - edit it with real values before starting the services"
else
    echo "    $ENV_FILE already exists, leaving it untouched"
fi

echo "==> Installing systemd units"
for svc in listener worker bot; do
    cat > "/etc/systemd/system/camera-$svc.service" << EOF
[Unit]
Description=Camera Alert $svc
After=network.target
$( [[ "$svc" != "listener" ]] && echo "Wants=camera-listener.service" )

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $REPO_DIR/$svc.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
done

systemctl daemon-reload

echo "==> Done"
echo "1. Edit $ENV_FILE with your Telegram bot token, chat id, and camera credentials"
echo "2. Edit config.py CAMERAS list with your camera IPs"
echo "3. Start everything: systemctl enable --now camera-listener camera-worker camera-bot"
