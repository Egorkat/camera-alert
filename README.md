# Camera Alert

Motion-alert pipeline for Dahua (and Dahua-OEM) IP cameras: listens for camera
events over HTTP long-poll, stores snapshots/clips, and delivers Telegram alerts
with an interactive bot menu for status, mute, and filtering.

## How it works

Three independent services, communicating through a shared SQLite database:

```
listener.py  →  events.db  →  worker.py  →  Telegram
     │                                         ▲
     └── snapshots/, clips/            bot.py ─┘ (commands, menu, mute, filter)
```

- **`listener.py`** — opens a persistent HTTP connection to each camera's
  `eventManager.cgi` endpoint and streams motion/alarm events in real time.
  On a supported event it grabs a live snapshot, stores the event in SQLite,
  and captures a short video clip (preferring the camera's own recorded file
  via the `NewFile` event + `RPC_Loadfile`, falling back to a live RTSP
  capture if that doesn't arrive in time). Also runs a disk-space watchdog.
- **`worker.py`** — polls the database for unsent events and delivers them to
  Telegram, applying the active event-type filter and mute state.
- **`bot.py`** — Telegram bot with both text commands and an inline-button
  menu: live snapshots, recent event history, mute/unmute with reminders,
  event-type filtering, and disk usage.

## Requirements

- Python 3 with `requests`
- `ffmpeg` (for clip remuxing/fallback capture)
- One or more Dahua/Dahua-OEM cameras with HTTP API access enabled
- A Telegram bot token ([@BotFather](https://t.me/BotFather)) and your chat ID

## Configuration

All secrets are read from environment variables — nothing sensitive lives in
`config.py`:

| Variable | Description |
|---|---|
| `CAMERA_BOT_TOKEN` | Telegram bot token |
| `CAMERA_CHAT_ID` | Telegram chat ID to send alerts to |
| `CAMERA_USER` | Default camera username (optional, defaults to `admin`) |
| `CAMERA_PASS` | Default camera password |

Edit `config.py` to set your camera list (`CAMERAS`), storage paths, and
alert thresholds. Per-camera credentials can override the defaults:

```python
CAMERAS = [
    {"name": "front", "ip": "10.0.0.101"},
    {"name": "gate",  "ip": "10.0.0.102", "user": "viewer", "pass": "secret"},
]
```

## Running

Each service is meant to run continuously (e.g. as a systemd unit):

```bash
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... CAMERA_PASS=... python3 listener.py
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... python3 worker.py
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... python3 bot.py
```

Example systemd unit (repeat for `listener`, `worker`, `bot`):

```ini
[Unit]
Description=Camera Alert Listener
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/camera-alert
Environment="CAMERA_BOT_TOKEN=..."
Environment="CAMERA_CHAT_ID=..."
Environment="CAMERA_PASS=..."
ExecStart=/usr/bin/python3 /opt/camera-alert/listener.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Telegram bot usage

Send `/menu` for the interactive button interface, or use text commands
directly:

| Command | Description |
|---|---|
| `/menu` | Interactive button menu |
| `/status` | Camera status, mute state, filter, disk usage |
| `/disk` | Disk space usage and alert threshold |
| `/snap [camera]` | Live snapshot (all cameras if omitted) |
| `/last [N]` | Last N events (default 5, max 20) |
| `/mute` | Silence alerts (auto re-enables after a reminder cycle) |
| `/keepmute` | Reset the mute reminder timer |
| `/unmute` | Re-enable alerts |
| `/filter` | Show/edit active event-type filter |
| `/help` | Show command list |

A daily status summary is sent automatically at the configured time
(`DAILY_STATUS_HOUR`/`DAILY_STATUS_MINUTE` in `config.py`).

## Notes and limitations

- Snapshots and clips are captured for **every** supported event type
  regardless of the notification filter — the filter only controls whether
  a Telegram message is sent, not whether the event is recorded.
- Fetching historical clips directly from camera SD-card storage
  (`mediaFileFind`) proved unreliable across firmware versions and isn't
  used; clips come from the `NewFile` push event or a live RTSP fallback.
- Health check endpoints (`HEALTH_PORT_*` in `config.py`) expose basic JSON
  status over HTTP for external monitoring; set to `None` to disable.
