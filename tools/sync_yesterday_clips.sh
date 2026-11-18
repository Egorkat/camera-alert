#!/bin/bash
set -euo pipefail

CLIP_DIR="/opt/camera-alert/clips"
NAS_TARGET="master@10.10.0.50"
NAS_DIR="/home/video"
NAS_STATE_DIR="$NAS_DIR/.camera-alert-sync-state"
SSH_KEY="/root/.ssh/camera-alert-clips"

sync_date="$(date --date=yesterday +%F)"
source_day="$(date --date=yesterday +%d-%m)"
source_dir="$CLIP_DIR/$source_day"
destination="$NAS_TARGET:$NAS_DIR/$sync_date/"

if [[ ! -d "$source_dir" ]]; then
    echo "No clip directory for yesterday ($source_dir); nothing to sync"
    exit 0
fi

if [[ ! -r "$SSH_KEY" ]]; then
    echo "SSH identity key is missing or unreadable: $SSH_KEY" >&2
    exit 1
fi

# Limit the NAS's legacy cipher requirement to this SSH connection.
rsync --archive --partial --human-readable \
    -e "ssh -i $SSH_KEY -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o Ciphers=+aes128-cbc" \
    "$source_dir/" "$destination"

ssh -i "$SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes -o Ciphers=+aes128-cbc \
    "$NAS_TARGET" \
    "umask 077 && install -d -m 0700 '$NAS_STATE_DIR' && touch '$NAS_STATE_DIR/$sync_date'"

echo "Synced clips from $source_dir to $destination"
