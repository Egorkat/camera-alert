# Camera Alert

Motion-alert pipeline for Dahua (and Dahua-OEM) IP cameras: listens for camera
events over HTTP long-poll, stores snapshots/clips, and delivers Telegram alerts
with an interactive bot menu for status, mute, and filtering.

## How it works

Three independent services, communicating through a shared SQLite database and
small flag files:

```
listener.py  →  events.db  →  worker.py  →  Telegram
     │                                         ▲
     └── snapshots/, clips/            bot.py ─┘ (commands, menu, mute, filter)
```

- **`listener.py`** — starts one persistent HTTPS event stream per camera,
  captures snapshots, stores events in SQLite, and captures clips. It starts a
  live RTSP fallback immediately and separately searches the camera SD card
  for the matching `.dav` recording with `mediaFileFind` and `RPC_Loadfile`.
  It also handles reconnects, disk-space monitoring, and a JSON health
  endpoint.
  Telegram, applying the active event-type filter and mute state. Events that
  are filtered or muted are marked processed; only send failures are retried.
- **`bot.py`** — Telegram bot with both text commands and an inline-button
  menu: live snapshots, recent event history, mute/unmute with reminders,
  event-type filtering, disk usage, and daily status summaries. Muting also
  disables camera email alerts.

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

## Installation

```bash
git clone <repo-url> /opt/camera-alert
cd /opt/camera-alert
sudo ./install.sh
```

This installs system dependencies (`ffmpeg`, `python3-requests`), creates the
`snapshots/`/`clips/` directories, writes a secrets template to
`/etc/camera-alert.env`, and installs the three systemd units. Then:

1. Edit `/etc/camera-alert.env` with your real Telegram bot token, chat ID,
   and camera credentials.
2. Edit `config.py`'s `CAMERAS` list with your camera IPs.
3. `systemctl enable --now camera-listener camera-worker camera-bot`

The installation path must be `/opt/camera-alert` because the paths in
`config.py` are absolute.

## Running manually (without systemd)

```bash
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... CAMERA_PASS=... python3 listener.py
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... python3 worker.py
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... python3 bot.py
```

## Storage and monitoring

- `events.db` stores event metadata, snapshot paths, fallback clip paths, and
  recorded camera clip paths.
- `snapshots/DD-MM/` and `clips/DD-MM/` contain per-day media directories.
- `filter.json` contains the active notification event codes. The listener
  still records all codes in `ALL_EVENT_CODES`; the filter only controls
  Telegram delivery.
- A `muted` flag file silences worker delivery while the bot manages the mute
  reminder and automatic unmute cycle.
- `bot`, `listener`, and `worker` logs rotate daily and retain the configured
  number of days.
- The services expose JSON health endpoints on ports 8081, 8082, and 8083 by
  default. Set the corresponding `HEALTH_PORT_*` value to `None` to disable
  one.

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
| `/filter [add\|off\|reset] <code>` | Show/edit active event-type filter |
| `/help` | Show command list |

A daily status summary is sent automatically at the configured time
(`DAILY_STATUS_HOUR`/`DAILY_STATUS_MINUTE` in `config.py`).

## Notes and limitations

- Snapshots and clips are captured for **every** supported event type
  regardless of the notification filter — the filter only controls whether
  a Telegram message is sent, not whether the event is recorded.
- The camera's `NewFile` event is reliable for `.jpg` snapshots but not for
  `.dav` recordings. The listener therefore polls `mediaFileFind` narrowly for
  the recording covering each event, while retaining the live RTSP capture as
  a fallback. Downloaded `.dav`/dhav files are stored as-is because this
  camera's variant cannot be remuxed reliably by `ffmpeg`.
- JSON-RPC sessions are reused per camera to avoid exhausting the camera's
  concurrent-session limit; expired sessions are recreated automatically.
- Camera HTTP endpoints use digest authentication and disable TLS certificate
  verification for trusted-LAN, self-signed camera certificates.
- Health check endpoints (`HEALTH_PORT_*` in `config.py`) expose basic JSON
  status over HTTP for external monitoring; set to `None` to disable.
