#!/usr/bin/python3
"""Send apcupsd event notifications to the camera-alert Telegram chat."""

import json
import os
import subprocess
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ENV_FILE = "/etc/camera-alert.env"
APCACCESS = "/usr/sbin/apcaccess"

EVENT_MESSAGES = {
    "onbattery": "Power failure: the UPS switched to battery.",
    "offbattery": "Mains power restored; the UPS is back online.",
    "commfailure": "Communication with the UPS was lost.",
    "commok": "Communication with the UPS was restored.",
    "changeme": "The UPS battery needs replacement.",
    "failing": "UPS battery is depleted; apcupsd is initiating shutdown.",
    "timeout": "The UPS runtime limit was reached; apcupsd is initiating shutdown.",
    "loadlimit": "The UPS battery charge limit was reached; apcupsd is initiating shutdown.",
    "runlimit": "The UPS runtime limit was reached; apcupsd is initiating shutdown.",
}


def _load_credentials():
    values = {}
    with open(ENV_FILE, encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in {"CAMERA_BOT_TOKEN", "CAMERA_CHAT_ID"}:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value

    token = values.get("CAMERA_BOT_TOKEN")
    chat_id = values.get("CAMERA_CHAT_ID")
    if not token or not chat_id or token == "changeme" or chat_id == "changeme":
        raise ValueError("Telegram credentials are missing from the shared env file")
    return token, chat_id


def _ups_status():
    try:
        result = subprocess.run(
            [APCACCESS, "status"],
            capture_output=True,
            check=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    status = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            status[key.strip()] = value.strip()
    return status


def _message(event, status):
    details = [EVENT_MESSAGES.get(event, f"UPS event: {event}.")]
    ups_name = status.get("UPSNAME")
    model = status.get("MODEL")
    if ups_name or model:
        details.append("UPS: " + " ".join(value for value in (ups_name, model) if value))

    metrics = []
    for key, label in (("STATUS", "Status"), ("BCHARGE", "Battery"), ("TIMELEFT", "Runtime")):
        value = status.get(key)
        if value:
            metrics.append(f"{label}: {value}")
    if metrics:
        details.append(" | ".join(metrics))

    return f"{os.uname().nodename}: " + "\n".join(details)


def _send(token, chat_id, text):
    body = urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=5) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the UPS alert")


def main(args):
    dry_run = len(args) == 2 and args[0] == "--dry-run"
    if dry_run:
        event = args[1]
    elif len(args) == 1:
        event = args[0]
    else:
        print("Usage: apcupsd_telegram.py [--dry-run] EVENT", file=sys.stderr)
        return 2

    text = _message(event, _ups_status())
    if dry_run:
        print(text)
        return 0

    try:
        token, chat_id = _load_credentials()
        _send(token, chat_id, text)
    except Exception:
        # Do not log request exceptions: Telegram URLs contain the bot token.
        print("Failed to send UPS alert to Telegram", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
