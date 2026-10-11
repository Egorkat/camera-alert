#!/bin/bash
set -euo pipefail

HOOK_DIR="/etc/apcupsd"
HOOK="/opt/camera-alert/tools/apcupsd_hook.sh"
ENV_FILE="/etc/camera-alert.env"
EVENTS=(onbattery offbattery commfailure commok changeme failing timeout loadlimit runlimit)

if [[ "$EUID" -ne 0 ]]; then
    echo "Run as root: sudo /opt/camera-alert/tools/install_apcupsd_telegram.sh" >&2
    exit 1
fi

if [[ ! -x "$HOOK" || ! -f /opt/camera-alert/tools/apcupsd_telegram.py ]]; then
    echo "Camera Alert UPS notifier files are missing from /opt/camera-alert/tools" >&2
    exit 1
fi
if [[ ! -x "$HOOK_DIR/apccontrol" || ! -r "$ENV_FILE" ]]; then
    echo "apcupsd or $ENV_FILE is not configured on this host" >&2
    exit 1
fi
if ! systemctl is-active --quiet apcupsd; then
    echo "apcupsd must be active before installing its event hooks" >&2
    exit 1
fi

for event in "${EVENTS[@]}"; do
    current="$HOOK_DIR/$event"
    backup="$current.camera-alert-original"
    if [[ -L "$current" && "$(readlink "$current")" == "$HOOK" ]]; then
        continue
    fi
    if [[ ( -e "$current" || -L "$current" ) && ( -e "$backup" || -L "$backup" ) ]]; then
        echo "Refusing to overwrite $current; backup already exists at $backup" >&2
        exit 1
    fi
done

for event in "${EVENTS[@]}"; do
    current="$HOOK_DIR/$event"
    backup="$current.camera-alert-original"
    if [[ -L "$current" && "$(readlink "$current")" == "$HOOK" ]]; then
        continue
    fi
    if [[ -e "$current" || -L "$current" ]]; then
        mv "$current" "$backup"
    fi
    ln -s "$HOOK" "$current"
done

printf 'UPS Telegram hooks installed for: %s\n' "${EVENTS[*]}"
printf 'Existing hook scripts, where present, were preserved as *.camera-alert-original.\n'
