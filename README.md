# Camera Alert

Motion-alert pipeline for Dahua (and Dahua-OEM) IP cameras: listens for camera
events over HTTP long-poll, stores snapshots/clips, and delivers Telegram alerts
with an interactive bot menu for status, mute, and filtering.

## How it works

Four application services plus the FTP upload service communicate through a
shared SQLite database and small flag files:

```
listener.py  →  events.db  →  worker.py  →  Telegram
     │                                         ▲
  ├── snapshots/, clips           bot.py ─┘ (commands, menu, mute, filter)
  └── FTP .dav files → converter.py → clips/DD-MM/*.mp4
```

- **`listener.py`** — starts one persistent HTTPS event stream per camera,
  captures snapshots, stores events in SQLite, and captures clips. For each
  event it looks up the camera's own SD-card `.dav` recording through
  `mediaFileFind` and `RPC_Loadfile`, while also using a live RTSP capture as a
  fallback. FTP-uploaded `.dav` files are processed independently by
  `converter.py` and are not linked to events by the listener.
  It also handles reconnects, disk-space monitoring, and a JSON health
  endpoint.
- **`worker.py`** — polls unsent events and sends Telegram alerts with optional
  snapshots, applying the active event-type filter and mute state. Events that
  are filtered or muted are marked processed; only send failures are retried.
- **`bot.py`** — Telegram bot with both text commands and an inline-button
  menu: live snapshots, recent event history, mute/unmute with reminders,
  event-type filtering, disk usage, and daily status summaries. Muting also
  disables camera email alerts.
- **`converter.py`** — scans completed FTP `.dav` files oldest-first, converts
  the first six seconds to smaller H.264/AAC MP4 files at up to 1280 pixels
  wide, and stores them in `clips/DD-MM/`. Conversion state is tracked in
  SQLite; failures generate Telegram warnings, and manually deleted outputs
  are recorded so they are not regenerated indefinitely.
- **`camera-ftp.service`** — accepts camera FTP uploads into `ftp/`; it is
  separate from the four Python application services.

## Requirements

- Python 3 with `requests`
- `ffmpeg` (for MP4 conversion and live RTSP fallback capture)
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

This installs system dependencies (`ffmpeg`, `python3-requests`, and
`vsftpd`), creates the `snapshots/`, `clips/`, and `ftp/` directories, writes a
secrets template to `/etc/camera-alert.env`, and installs four Python systemd
units plus the FTP systemd unit. The FTP server listens on port 21 and accepts
uploads into `/opt/camera-alert/ftp` using a dedicated local account. Then:

1. Edit `/etc/camera-alert.env` with your real Telegram bot token, chat ID,
   camera credentials, and FTP credentials if needed.
2. Edit `config.py`'s `CAMERAS` list with your camera IPs.
3. Configure one camera's FTP destination as this host's LAN IP, port `21`,
   the `CAMERA_FTP_USER`/`CAMERA_FTP_PASS` credentials, and remote directory
   `/` (the account is chrooted to the FTP drop directory).
4. `systemctl enable --now camera-listener camera-worker camera-bot camera-converter`

The installation path must be `/opt/camera-alert` because the paths in
`config.py` are absolute.

## Running manually (without systemd)

```bash
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... CAMERA_PASS=... python3 listener.py
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... python3 worker.py
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... python3 bot.py
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... CAMERA_PASS=... python3 converter.py
```

## Storage and monitoring

- `events.db` stores event metadata, snapshot paths, fallback clip paths,
  recorded camera clip paths, and conversion status/output paths. For `left`,
  the recorded path points into the FTP upload tree after the `.dav` and
  matching `.idx` files are complete.
- `snapshots/DD-MM/` contains snapshots; `clips/DD-MM/` contains live fallback
  clips and the converter's six-second MP4 review clips.
- `ftp/` is the FTP upload drop directory, exposed as `/` to the dedicated FTP
  account and writable for Samba access. FTP transfer logs are written to
  `/var/log/camera-alert/ftp.log`.
- `filter.json` contains the active notification event codes. The listener
  still records all codes in `ALL_EVENT_CODES`; the filter only controls
  Telegram delivery.
- A `muted` flag file silences worker delivery while the bot manages the mute
  reminder and automatic unmute cycle.
- `bot`, `listener`, and `worker` logs are stored in `/var/log/camera-alert`,
  rotate daily, and retain the configured number of days.
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
  `.dav` recordings. The listener uses `mediaFileFind` and `RPC_Loadfile` to
  download the camera's SD-card recording, with live RTSP capture as a
  fallback. FTP-uploaded `.dav` files are handled separately by the converter.
- The converter processes FTP recordings oldest-first and processes each source
  day once. It records conversion status in SQLite, sends a Telegram warning
  for a failed conversion, and records manually deleted outputs so old clips
  are not regenerated indefinitely. Failed conversions are not silently
  retried forever.
- JSON-RPC sessions are reused per camera to avoid exhausting the camera's
  concurrent-session limit; expired sessions are recreated automatically.
- Camera HTTP endpoints use digest authentication and disable TLS certificate
  verification for trusted-LAN, self-signed camera certificates.
- Health check endpoints (`HEALTH_PORT_*` in `config.py`) expose basic JSON
  status over HTTP for external monitoring; set to `None` to disable.

## Verified camera configuration

The Dahua JSON-RPC challenge/response login has been verified against both
configured cameras using the same credentials loaded by systemd from
`/etc/camera-alert.env`. The configuration tables exposed by this firmware are:

- `MotionDetect` — ordinary motion detection settings
- `VideoAnalyseRule` — IVS analytics rules, including cross-line and
  cross-region detection

The two cameras currently match for `MotionDetect`: enabled state, sensitivity
(`60`), threshold (`5`), full-frame region, 24/7 schedule, recording,
snapshots, and alarm-output actions.

Their `VideoAnalyseRule` settings are not identical. The cross-line geometry is
different, and the first analytics rule has mail notifications disabled on
`10.30.0.201` but enabled on `10.30.0.202`. No camera settings are changed by
the checker; [check_camera_rpc.py](check_camera_rpc.py) only reads them.
