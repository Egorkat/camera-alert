#!/bin/sh

EVENT=${0##*/}
ORIGINAL="${0}.camera-alert-original"

if [ -x "$ORIGINAL" ]; then
    "$ORIGINAL" "$@"
fi

/usr/bin/python3 /opt/camera-alert/tools/apcupsd_telegram.py "$EVENT" || \
    /usr/bin/logger -t camera-alert-ups "Telegram notification failed for $EVENT"

exit 0
