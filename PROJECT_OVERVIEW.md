# Camera Alert — Project Knowledge Dump

Motion-alert pipeline for Dahua (and Dahua-OEM) IP cameras. Listens for camera
events over HTTP long-poll, stores snapshots/clips, and delivers Telegram
alerts with an interactive bot menu for status, mute, and event filtering.

## Architecture

Four long-running Python services (no framework, stdlib + `requests`) plus
the FTP server communicate through a shared SQLite database (`events.db`) and
a couple of flag files on disk:

```
listener.py  →  events.db  →  worker.py  →  Telegram
     │                                         ▲
     └── snapshots/, clips/            bot.py ─┘ (commands, menu, mute, filter)
```

- **`listener.py`** — one thread per camera, opens a persistent HTTPS
  connection to `eventManager.cgi?action=attach&codes=[All]` and streams
  motion/alarm events as a multipart text stream (boundary `--myboundary`).
  For each supported event:
  - fetches a live JPEG snapshot (digest auth, `snapshot.cgi`) if the event
    says `WithSnap`
  - inserts a row into the `events` table
  - captures six seconds of live RTSP video into memory, checks the local FTP
    tree for a matching `.dav` for up to 120 seconds, then checks the camera's
    SD-card recording via JSON-RPC (`mediaFileFind` + `RPC_Loadfile`) for up to
    300 more seconds. It saves the live buffer only if neither source succeeds.
    FTP `.dav` files are converted independently; FTP recordings are not linked
    to their triggering event rows.
  - Also runs a disk-space watchdog thread and per-camera reconnect/backoff
    logic that Telegram-alerts on camera connection loss/recovery.
  - Exposes an HTTP health endpoint (JSON) with per-camera up/down state.
- **`worker.py`** — polls `events` for `sent=0` rows every ~2s, applies the
  active event-type filter (`filter.json`, else `config.EVENT_CODES`) and the
  mute flag file, and sends a Telegram message (+ snapshot photo if present)
  for each. Marks rows `sent=1` whether actually delivered, filtered, or
  muted (only true send failures are retried). Rate-limited to 1 msg/sec.
- **`bot.py`** — Telegram long-polling bot (`getUpdates`) with text commands
  and an inline-button menu. Handles `/status`, `/disk`, `/snap`, `/last`,
  `/mute` (+ reminder/auto-unmute timers), `/unmute`, `/keepmute`,
  `/filter add|off|reset`, `/help`, `/menu`. Sends a daily status summary at
  a configured time. Mute also disables camera email alerts via
  `configManager.cgi?action=setConfig&Email.Enable=`.
- **`converter.py`** — processes FTP DAVs oldest-first, stream-copies the
  original HEVC video for the first six seconds into MP4, and converts A-law
  audio to AAC. Video resolution and picture encoding are preserved; output
  files are larger and need HEVC playback support. Conversion/deletion status
  and errors are tracked in SQLite.

## Data flow / storage

- `events.db` (SQLite) — table `events(id, ts, camera, event_type, raw,
  snapshot_path, clip_path, sent, bbox, recorded_clip_path, converted_clip_path,
  conversion_status, conversion_error)`. `clip_path` is populated only when
  the live RTSP buffer is needed; `recorded_clip_path` is used for a downloaded
  SD-card recording. FTP conversion is normally independent of event rows.
- `snapshots/DD-MM/` and `clips/DD-MM/` — per-day subfolders, world-writable
  (`chmod 777`) so a Samba user can also delete files (processes run as
  root).
- `filter.json` — JSON array of active event codes; current contents:
  `["CrossLineDetection", "CrossRegionDetection", "SmartMotionHuman"]`.
- `muted` file — presence silences worker delivery; managed by bot.py.
- Log files: `/var/log/camera-alert/{bot,listener,worker}.log`, daily-rotated
  (`TimedRotatingFileHandler`, `LOG_KEEP_DAYS=14`).
- FTP uploads are accepted by `camera-ftp.service` into
  `/opt/camera-alert/ftp`, using credentials from `/etc/camera-alert.env`.
  The directory is world-writable so Samba users can manage uploaded files.

## Configuration (`config.py`)

Secrets come from environment variables (`CAMERA_BOT_TOKEN`,
`CAMERA_CHAT_ID`, `CAMERA_USER`, `CAMERA_PASS`), loaded via
`/etc/camera-alert.env` under systemd. Everything else is in `config.py`:

- `CAMERAS` — list of `{name, ip, [user], [pass]}` dicts. Currently `left`
  (10.30.0.201) and `right` (10.30.0.202).
- `DB_PATH`, `SNAPSHOT_DIR`, `CLIP_DIR`, `FTP_DIR`, `LOG_PATH` — all hardcoded to
  `/opt/camera-alert/...` (install must be at that exact path).
- `CLIP_SECONDS=6` — live RTSP is captured immediately into memory. The
  listener waits up to 120 seconds for FTP, then up to 300 seconds for an
  SD-card recording before saving that buffer as fallback.
- `CONVERTER_DURATION=6` — converter output duration. It copies HEVC video
  without re-encoding; only unsupported A-law audio is converted to AAC for
  MP4 compatibility.
- `EVENT_CODES` — default/notification-filter event types (`VideoMotion`,
  `SmartMotionHuman`).
- `ALL_EVENT_CODES` — full set of event types this camera model supports
  (confirmed via `RemoteEventManager.getEventList`); listener records
  snapshots/clips for all of these regardless of the notification filter.
- `DISK_CHECK_*` — disk watchdog thresholds (alert below 10% free, 6h
  cooldown).
- `DAILY_STATUS_HOUR/MINUTE` — automatic daily status message time (09:00).
- `HEALTH_PORT_LISTENER/WORKER/BOT` (8081/8082/8083) — per-service HTTP
  health JSON endpoints; set to `None` to disable.

### Live camera configuration verification

`check_camera_rpc.py` uses the same `CAMERA_USER` and `CAMERA_PASS` values as
the services and performs the Dahua two-step challenge/response login. Live
testing confirmed that both cameras accept the login and expose these relevant
configuration tables through `configManager.getConfig`:

- `MotionDetect` for ordinary motion detection
- `VideoAnalyseRule` for IVS analytics rules

The `MotionDetect` tables currently match on both cameras: enabled state,
sensitivity `60`, threshold `5`, full-frame region, 24/7 schedule, recording,
snapshots, and alarm-output actions. The analytics tables do not fully match:
the cross-line coordinates differ (`Y=5240` on `10.30.0.201` versus `Y=4957`
on `10.30.0.202`), and mail notification for the first rule is disabled on
the former but enabled on the latter. The checker is read-only; no settings
have been synchronized automatically.

## Key implementation notes / gotchas

- **Filter vs. capture are decoupled**: `filter.json` only controls whether
  `worker.py` sends a Telegram message. `listener.py` always records
  snapshots/clips for every code in `ALL_EVENT_CODES`.
- **Clips**: the camera's `NewFile` push event only fires for `.jpg`
  snapshots, not `.dav` recordings. `listener.py` buffers six seconds of live
  RTSP immediately, polls its FTP tree for 120 seconds, then polls the camera's
  SD-card index for up to 300 seconds. A found FTP file is left for the
  independent converter; a found SD recording is downloaded with `RPC_Loadfile`
  and stored raw. The live buffer is saved only if those paths fail.
- **MP4 review clips**: `converter.py` demuxes the camera's DHAV payload and
  stream-copies the HEVC video into a six-second MP4, preserving the original
  picture and resolution. A-law audio is encoded to AAC because this FFmpeg
  MP4 muxer rejects the source audio stream. This trades larger files and HEVC player
  requirements for avoiding video transcoding artifacts.
- **JSON-RPC session reuse**: one login session per camera IP is cached and
  reused across events (`_rpc_sessions`), because logging in for every event
  exhausted the camera's concurrent-session limit ("too many connections!").
  Session invalidation is detected via error code `287637504` and triggers a
  fresh login.
- **Camera config names are firmware-specific**: the generic names
  `VideoMotion` and `SmartMotion` are not valid config tables on these cameras.
  Use `MotionDetect` and `VideoAnalyseRule` when inspecting their settings.
- **`mediaFileFind` via SD card listing proved unreliable across firmware
  versions** for *browsing* historical clips generically — it's only used
  narrowly here to find the one file matching a specific event timestamp.
- Digest auth (`HTTPDigestAuth`) is used for camera HTTP endpoints;
  self-signed certs mean `verify=False` everywhere (acceptable since
  cameras are on a trusted LAN, not the public internet).
- **Mute flow**: `/mute` sets a flag file, disables camera email alerts, and
  arms a 1h reminder timer; if unanswered, auto-unmutes 5 min after the
  reminder. `/keepmute` resets the 1h timer. Timer state is re-armed on
  bot.py restart if the mute file is still present.
- Health endpoints are simple stdlib `http.server` JSON responders running
  in a daemon thread inside each service — used by `bot.py`'s `/status` to
  show live camera up/down icons and by external monitoring.
- The four Python services are designed to run under systemd
  (`camera-listener`, `camera-worker`, `camera-bot`, `camera-converter`), installed by
  `install.sh`, which also installs `ffmpeg`/`python3-requests` and writes
  `/etc/camera-alert.env`. The repo **must** live at `/opt/camera-alert`
  since paths are hardcoded in `config.py`.

## Telegram bot commands

| Command | Description |
|---|---|
| `/menu` | Interactive button menu |
| `/status` | Camera status (via health endpoint), mute state, filter, disk usage |
| `/disk` | Disk space usage and alert threshold |
| `/snap [camera]` | Live snapshot (all cameras if omitted) |
| `/last [N]` | Last N events (default 5, max 20) |
| `/mute` | Silence alerts + disable camera email (auto re-enables after reminder cycle) |
| `/keepmute` | Reset the mute reminder timer |
| `/unmute` | Re-enable alerts + camera email |
| `/filter [add\|off\|reset] <code>` | Show/edit active event-type filter |
| `/help` | Show command list |

## Requirements

- Python 3 + `requests` (+ `urllib3`)
- `ffmpeg` (clip remuxing / live RTSP fallback capture)
- Dahua/Dahua-OEM camera(s) with HTTP API access enabled
- Telegram bot token + chat ID

## Repo layout

```
bot.py / worker.py / listener.py / converter.py - the four Python services
config.py                          - all configuration (non-secret)
filter.json                        - active event-type filter (mutable at runtime)
install.sh                         - systemd + dependency installer
events.db                          - SQLite event log (not in repo listing but created at runtime)
snapshots/DD-MM/, clips/DD-MM/     - captured media, per-day folders
{bot,listener,worker}.log.DATE     - daily rotated logs
```
