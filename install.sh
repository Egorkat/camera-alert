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
apt-get install -y python3 python3-requests python3-urllib3 ffmpeg openssl vsftpd

echo "==> Creating data directories"
mkdir -p "$REPO_DIR/snapshots" "$REPO_DIR/clips" "$REPO_DIR/ftp"
install -d -m 0755 /var/log/camera-alert

echo "==> Writing environment file template"
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" << 'EOF'
# Camera Alert secrets - fill these in, then: systemctl restart camera-listener camera-worker camera-bot
CAMERA_BOT_TOKEN=changeme
CAMERA_CHAT_ID=changeme
CAMERA_USER=admin
CAMERA_PASS=changeme
CAMERA_FTP_USER=cameraftp
CAMERA_FTP_PASS=changeme
EOF
    chmod 600 "$ENV_FILE"
    echo "    Created $ENV_FILE - edit it with real values before starting the services"
else
    echo "    $ENV_FILE already exists, leaving it untouched"
fi

echo "==> Configuring FTP upload server"
if ! grep -q '^CAMERA_FTP_USER=' "$ENV_FILE"; then
    printf '\nCAMERA_FTP_USER=cameraftp\n' >> "$ENV_FILE"
fi
if ! grep -q '^CAMERA_FTP_PASS=' "$ENV_FILE"; then
    ftp_password="$(openssl rand -hex 16)"
    printf 'CAMERA_FTP_PASS=%s\n' "$ftp_password" >> "$ENV_FILE"
    echo "    Generated FTP password in $ENV_FILE"
elif grep -q '^CAMERA_FTP_PASS=changeme$' "$ENV_FILE"; then
    ftp_password="$(openssl rand -hex 16)"
    sed -i "s/^CAMERA_FTP_PASS=.*/CAMERA_FTP_PASS=$ftp_password/" "$ENV_FILE"
    echo "    Generated FTP password in $ENV_FILE"
fi
ftp_user="$(sed -n 's/^CAMERA_FTP_USER=//p' "$ENV_FILE")"
ftp_password="$(sed -n 's/^CAMERA_FTP_PASS=//p' "$ENV_FILE")"
if [[ -z "$ftp_user" || -z "$ftp_password" || "$ftp_password" == "changeme" ]]; then
    echo "CAMERA_FTP_USER and CAMERA_FTP_PASS must be set in $ENV_FILE" >&2
    exit 1
fi

if id "$ftp_user" >/dev/null 2>&1; then
    usermod --home "$REPO_DIR/ftp" --shell /usr/sbin/nologin "$ftp_user"
else
    useradd --system --home-dir "$REPO_DIR/ftp" --shell /usr/sbin/nologin \
        --no-create-home "$ftp_user"
fi
    grep -qxF /usr/sbin/nologin /etc/shells || echo /usr/sbin/nologin >> /etc/shells
printf '%s:%s\n' "$ftp_user" "$ftp_password" | chpasswd
chown -R "$ftp_user":"$ftp_user" "$REPO_DIR/ftp"
chmod 0777 "$REPO_DIR/ftp"
printf '%s\n' "$ftp_user" > /etc/vsftpd-camera-alert.users
chmod 0644 /etc/vsftpd-camera-alert.users

cat > /etc/vsftpd-camera-alert.conf << EOF
listen=YES
listen_ipv6=NO
listen_port=21
anonymous_enable=YES
local_enable=YES
write_enable=YES
local_umask=022
check_shell=NO
userlist_enable=NO
userlist_deny=NO
userlist_file=/etc/vsftpd-camera-alert.users
chroot_local_user=YES
allow_writeable_chroot=YES
local_root=$REPO_DIR/ftp
pasv_enable=YES
pasv_min_port=30000
pasv_max_port=30009
xferlog_enable=YES
xferlog_file=/var/log/camera-alert/ftp.log
EOF

cat > /etc/systemd/system/camera-ftp.service << EOF
[Unit]
Description=Camera Alert FTP upload server
After=network.target

[Service]
Type=simple
ExecStart=/usr/sbin/vsftpd /etc/vsftpd-camera-alert.conf
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "==> Installing systemd units"
for svc in listener worker bot converter; do
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
systemctl enable --now camera-ftp.service

echo "==> Done"
echo "1. Edit $ENV_FILE with your Telegram bot token, chat id, and camera credentials"
echo "2. Edit config.py CAMERAS list with your camera IPs"
echo "3. FTP user: $ftp_user (password is in $ENV_FILE)"
echo "4. Start everything: systemctl enable --now camera-listener camera-worker camera-bot"
