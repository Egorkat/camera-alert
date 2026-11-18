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
  event it captures six seconds of live RTSP video into memory, then checks the
  local FTP tree for a matching `.dav` for up to two minutes. If found, the
  buffer is discarded and `converter.py` handles the FTP file independently.
  Otherwise it checks the camera's SD-card recording through `mediaFileFind`
  and `RPC_Loadfile` for up to five more minutes; the live buffer is saved only
  if neither recording source succeeds.
  It also handles reconnects, disk-space monitoring, and a JSON health
  endpoint. Accepted snapshots are checked with a local YOLO object detector;
  supported person, animal, and vehicle detections are outlined when found.
  The model is downloaded on first use and cached locally. No rectangle is
  drawn when the detector finds no supported object.
- **`worker.py`** — polls unsent events and sends Telegram alerts with optional
  snapshots, applying the active event-type filter and mute state. Events that
  are filtered or muted are marked processed; only send failures are retried.
- **`bot.py`** — Telegram bot with both text commands and an inline-button
  menu: live snapshots, recent event history, mute/unmute with reminders,
  event-type filtering, disk usage, and daily status summaries. Muting also
  disables camera email alerts.
- **`converter.py`** — scans completed FTP `.dav` files oldest-first and puts
  the first six seconds into MP4 without re-encoding the original HEVC video.
  The video stays at its source resolution; the camera's A-law audio is
  converted to AAC because FFmpeg's MP4 muxer does not support that audio
  stream. Output is
  larger than the previous downscaled encode and requires an HEVC-capable
  player. Conversion state is tracked in SQLite; failures generate Telegram
  warnings. If a previously converted output disappears, that date is marked
  deleted so FTP files for that day are not regenerated.
- **`camera-ftp.service`** — accepts camera FTP uploads into `ftp/`; it is
  separate from the four Python application services.

## Requirements

- Python 3 with `requests` and `opencv-python-headless` 4.12.0.88
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

This installs system dependencies (`ffmpeg`, `python3-requests`, `vsftpd`,
`rsync`, and `openssh-client`), creates an isolated listener environment with
OpenCV 4.12, creates the `snapshots/`, `clips/`, and `ftp/` directories, writes
a secrets template to `/etc/camera-alert.env`, and installs four Python
systemd units plus the FTP systemd unit and a nightly clip-sync timer. The FTP
server listens on port 21 and accepts uploads into `/opt/camera-alert/ftp`
using a dedicated local account. It also installs a cleanup timer that checks
for NAS date folders deleted after a successful sync. The listener downloads
the object-detection model on first use and caches it under the service
account's home directory. Then:

1. Edit `/etc/camera-alert.env` with your real Telegram bot token, chat ID,
   camera credentials, and FTP credentials if needed.
2. Edit `config.py`'s `CAMERAS` list with your camera IPs.
3. Configure one camera's FTP destination as this host's LAN IP, port `21`,
   the `CAMERA_FTP_USER`/`CAMERA_FTP_PASS` credentials, and remote directory
   `/` (the account is chrooted to the FTP drop directory).
4. `systemctl enable --now camera-listener camera-worker camera-bot camera-converter`

The installation path must be `/opt/camera-alert` because the paths in
`config.py` are absolute.

### Nightly NAS clip sync

The installer enables `camera-clips-sync.timer`, which runs daily at 02:30
server-local time. It copies only the previous day's clips to
`master@10.10.0.50:/home/video/YYYY-MM-DD/` using rsync over SSH. Local clips
are retained until the matching NAS date folder is removed. The source folder
is named `DD-MM`; the destination uses the full date to keep separate years
distinct. A timer missed while the host is off runs once on return and syncs
the then-previous day.

On the camera-alert host, create a dedicated root SSH key and authorize it for
the `master` account on the NAS:

```bash
sudo install -d -m 0700 /root/.ssh
sudo ssh-keygen -t ed25519 -N '' -f /root/.ssh/camera-alert-clips
```

The NAS offers only the legacy `aes128-cbc` cipher. Keep the compatibility
exception limited to this NAS connection. First, compare the NAS host-key
fingerprint shown on the NAS with the prompt from this command on the
camera-alert host; accept the host key only if they match:

```bash
sudo ssh -o Ciphers=+aes128-cbc master@10.10.0.50
```

Then authorize the key (this may prompt for the NAS account password):

```bash
sudo ssh-copy-id -o Ciphers=+aes128-cbc \
  -i /root/.ssh/camera-alert-clips.pub master@10.10.0.50
```

Ensure `/home/video` exists on the NAS and is writable by `master`. Confirm
passwordless access and permissions with:

```bash
sudo ssh -i /root/.ssh/camera-alert-clips -o BatchMode=yes \
  -o Ciphers=+aes128-cbc master@10.10.0.50 'test -w /home/video'
```

The sync runs as root, requires the NAS host key to be trusted, and reports
transfer failures in the systemd journal (`journalctl -u camera-clips-sync`).
To run it manually, use `sudo systemctl start camera-clips-sync.service`.

After a successful sync, the sync service writes a hidden success marker under
`/home/video/.camera-alert-sync-state/`. The cleanup timer checks every
15 minutes. If a marked NAS date folder is absent continuously for 24 hours,
it removes the matching local `clips/DD-MM/` and all matching `YYYY-MM-DD/`
directories under `ftp/`. It records the date as deleted for the converter and
temporarily stops the converter during cleanup. Restoring the NAS date folder
within 24 hours cancels the pending cleanup. Today's and non-current-year dates
are never automatically cleaned, because local clip folder names do not
include a year.

Existing NAS folders synced before this feature have no success marker. Run
`sudo systemctl start camera-clips-sync.service` once for the current
previous-day folder to create its marker. To inspect cleanup activity, use
`journalctl -u camera-clips-cleanup.service`.

## Running manually (without systemd)

```bash
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... CAMERA_PASS=... .venv/bin/python listener.py
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... python3 worker.py
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... python3 bot.py
CAMERA_BOT_TOKEN=... CAMERA_CHAT_ID=... CAMERA_PASS=... python3 converter.py
```

## Storage and monitoring

- `events.db` stores event metadata, snapshot paths, live fallback paths,
  SD-recording paths, and conversion status/output paths. FTP recordings found
  by the listener are currently handled by the converter independently and are
  not linked to their triggering event rows.
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

## UPS notifications

When `apcupsd` is installed on the same Debian host, run
`sudo ./tools/install_apcupsd_telegram.sh` to route UPS events to the existing
Telegram chat configured in `/etc/camera-alert.env`. The installer preserves
existing event scripts as `*.camera-alert-original`; the hook wrapper continues
to run those scripts before sending Telegram notifications. It covers power
loss/restoration, UPS communication loss/recovery, battery replacement, and
low-battery shutdown events. Check the rendered message without sending it
with `python3 tools/apcupsd_telegram.py --dry-run onbattery`.

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

When all feedback collection is ON, alerts include classification buttons and
box feedback buttons for snapshots, even when the detector finds no object.
Choose whether the box is correct (or no box is needed) or wrong / missing.
Mark a correction by replying to the correction prompt with the image containing
one bright-red rectangle around the object; reply `none` if there is no target.
These responses are stored alongside the original snapshot as training examples;
they do not retrain the detector automatically. Use `/learn off` or the menu
toggle to stop collecting both classification and box feedback; `/learn on`
enables them again.

## Notes and limitations

- Snapshots and clips are captured for **every** supported event type
  regardless of the notification filter — the filter only controls whether
  a Telegram message is sent, not whether the event is recorded.
- The camera's `NewFile` event is reliable for `.jpg` snapshots but not for
  `.dav` recordings. The listener captures live RTSP into memory immediately,
  checks the local FTP tree for a matching recording for up to 120 seconds,
  then checks the camera's SD card via `mediaFileFind`/`RPC_Loadfile` for up
  to 300 seconds. It saves the live capture only if both recording paths fail.
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
