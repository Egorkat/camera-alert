import os
import requests
from requests.auth import HTTPDigestAuth
import sqlite3
import json
import logging
import logging.handlers
import time
import threading
import subprocess
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
import config
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


DB = config.DB_PATH
last_event = {}
last_event_lock = threading.Lock()
COOLDOWN = 3

cam_down = {}  # tracks which cameras are currently offline


def _start_health(port, get_status):
    """Start a minimal HTTP health server in a daemon thread."""
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(get_status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass  # suppress per-request access logs
    srv = HTTPServer(("", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def send_telegram_text(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
            data={"chat_id": config.CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        logging.warning("telegram alert error: %s", e)


def disk_usage_info():
    """Return (total, used, free, percent_free) in bytes for DISK_CHECK_PATH."""
    total, used, free = shutil.disk_usage(config.DISK_CHECK_PATH)
    percent_free = free / total * 100
    return total, used, free, percent_free


def _disk_watchdog():
    last_alert = 0
    while True:
        try:
            total, used, free, percent_free = disk_usage_info()
            if percent_free < config.DISK_ALERT_PERCENT:
                now = time.time()
                if now - last_alert > config.DISK_ALERT_COOLDOWN:
                    last_alert = now
                    send_telegram_text(
                        "\U0001f4be Low disk space on {}: {:.1f}% free ({} MB of {} MB)".format(
                            config.DISK_CHECK_PATH, percent_free,
                            free // (1024 ** 2), total // (1024 ** 2),
                        )
                    )
                    logging.warning("low disk space: %.1f%% free", percent_free)
        except Exception as e:
            logging.warning("disk watchdog error: %s", e)
        time.sleep(config.DISK_CHECK_INTERVAL)


def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            camera TEXT,
            event_type TEXT,
            raw TEXT,
            snapshot_path TEXT,
            clip_path TEXT,
            sent INTEGER DEFAULT 0
        )
    """)
    try:
        c.execute("ALTER TABLE events ADD COLUMN clip_path TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists on existing databases
    conn.commit()
    conn.close()


def _fetch_live_snapshot(ip, user, pw):
    url = f"https://{ip}/cgi-bin/snapshot.cgi?channel=1"
    r = requests.get(url, auth=HTTPDigestAuth(user, pw), timeout=10, verify=False)
    r.raise_for_status()
    return r.content


def fetch_snapshot(ip, user, pw, real_utc=None):
    try:
        return _fetch_live_snapshot(ip, user, pw)
    except Exception as e:
        logging.warning("snapshot fetch error: %s", e)
        return None


def process_event(name, ip, user, pw, event_lines):
    code_line = next((l for l in event_lines if l.startswith("Code=")), None)
    if not code_line:
        return

    # Parse semicolon-separated fields: Code=X;action=Y;index=Z;data={
    meta = {}
    for segment in code_line.split(";"):
        if "=" in segment:
            k, _, v = segment.partition("=")
            meta[k.strip()] = v.strip()

    code   = meta.get("Code", "")
    action = meta.get("action", "")

    # JSON body starts inline after "data=" and continues on following lines
    json_start = code_line.find("data=")
    json_str = code_line[json_start + 5:] if json_start != -1 else ""
    idx = event_lines.index(code_line)
    json_str += "\n" + "\n".join(event_lines[idx + 1:])

    if code == "NewFile":
        _handle_new_file(name, ip, user, pw, json_str)
        return

    # Capture snapshots/clips for all known supported event types; filter.json only
    # controls whether worker.py actually sends a Telegram notification for it.
    if action != "Start" or code not in config.ALL_EVENT_CODES:
        return

    with_snap = False
    real_utc  = None
    try:
        data = json.loads(json_str.strip())
        with_snap = data.get("WithSnap", False)
        real_utc  = data.get("RealUTC")
    except Exception:
        pass

    key = f"{name}_{code}"
    now = time.time()
    with last_event_lock:
        if now - last_event.get(key, 0) < COOLDOWN:
            return
        last_event[key] = now

    snapshot = fetch_snapshot(ip, user, pw, real_utc) if with_snap else None

    logging.info("%s %s%s", code, name, " (snap attached)" if snapshot else "")
    event_id = save_event(name, ip, code, "\n".join(event_lines), snapshot)

    if event_id:
        threading.Timer(
            config.CLIP_FALLBACK_DELAY, _fallback_capture_if_needed,
            args=(event_id, name, code, ip, user, pw),
        ).start()


def listen(cam):
    ip   = cam["ip"]
    name = cam["name"]
    user = cam.get("user", config.CAMERA_USER)
    pw   = cam.get("pass", config.CAMERA_PASS)

    # Use HTTPS with verify=False for self-signed certs common on IP cameras.
    # Replace verify=False with a proper CA bundle if available.
    url = f"https://{ip}/cgi-bin/eventManager.cgi?action=attach&codes=[All]"

    logging.info("[%s] listener started", name)

    backoff = 1

    while True:
        try:
            # stream=True keeps the connection open and lets us read the
            # multipart response line-by-line as the camera pushes events.
            with requests.get(
                url,
                auth=HTTPDigestAuth(user, pw),
                stream=True,
                timeout=(10, 300),  # (connect timeout, read timeout) — 5 min without data = dead connection
                verify=False,
            ) as r:
                r.raise_for_status()
                if cam_down.get(name):
                    cam_down[name] = False
                    send_telegram_text(f"✅ Camera [{name}] reconnected")
                backoff = 1  # reset on successful connection

                event_buf = []
                for raw_line in r.iter_lines():
                    if raw_line is None:
                        continue
                    # Cameras return bytes with no declared charset — decode explicitly
                    line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
                    line = line.rstrip("\r")  # iter_lines splits on \n but leaves \r on CRLF streams
                    if line.strip():
                        logging.debug("[%s] RAW: %s", name, line)

                    if line == "--myboundary":
                        if event_buf:
                            process_event(name, ip, user, pw, event_buf)
                        event_buf = []
                    else:
                        event_buf.append(line)

        except Exception as e:
            if "Read timed out" in str(e):
                # Camera is alive but idle (no motion for 5 min) — silent reconnect.
                logging.debug("[%s] read timeout (idle), reconnecting", name)
                backoff = 1
            else:
                logging.error("[%s] retry in %ss: %s", name, backoff, e)
                if not cam_down.get(name):
                    cam_down[name] = True
                    send_telegram_text(f"⚠️ Camera [{name}] connection lost: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


def save_event(name, ip, event_type, raw, snapshot):
    snapshot_path = None
    if snapshot:
        try:
            os.makedirs(config.SNAPSHOT_DIR, exist_ok=True)
            ts = int(time.time())
            _labels = {"VideoMotion": "motion", "SmartMotionHuman": "smart"}
            label = _labels.get(event_type, event_type.lower()[:8])
            fname = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(ts)) + f"_{name}_{label}.jpg"
            snapshot_path = os.path.join(config.SNAPSHOT_DIR, fname)
            with open(snapshot_path, "wb") as f:
                f.write(snapshot)
        except Exception as e:
            logging.warning("snapshot write error: %s", e)
            snapshot_path = None

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
        INSERT INTO events (ts, camera, event_type, raw, snapshot_path, sent)
        VALUES (?, ?, ?, ?, ?, 0)
    """, (
        int(time.time()),
        name,
        event_type,
        raw,
        snapshot_path,
    ))

    conn.commit()
    conn.close()

    logging.info("STORED %s (%s)", event_type, name)
    return c.lastrowid


def _download_clip(event_id, name, code, ip, user, pw, real_utc):
    """Fallback: capture CLIP_SECONDS of live RTSP, used only if no NewFile clip arrives in time."""
    os.makedirs(config.CLIP_DIR, exist_ok=True)
    _labels = {"VideoMotion": "motion", "SmartMotionHuman": "smart"}
    label = _labels.get(code, code.lower()[:8])
    fname = time.strftime("%Y-%m-%d_%H-%M-%S") + f"_{name}_{label}.mp4"
    clip_path = os.path.join(config.CLIP_DIR, fname)
    url = f"rtsp://{user}:{pw}@{ip}:554/cam/realmonitor?channel=1&subtype=0"

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", url,
             "-t", str(config.CLIP_SECONDS), "-c:v", "copy", "-c:a", "aac", clip_path],
            timeout=config.CLIP_SECONDS + 30, capture_output=True, check=True,
        )
        logging.info("fallback clip saved: %s", fname)
        conn = sqlite3.connect(DB)
        conn.execute("UPDATE events SET clip_path=? WHERE id=? AND clip_path IS NULL", (clip_path, event_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning("fallback clip capture failed for event %s: %s", event_id, e)


def _fallback_capture_if_needed(event_id, name, code, ip, user, pw):
    """Run the live-capture fallback only if no NewFile-sourced clip has arrived yet."""
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT clip_path FROM events WHERE id=?", (event_id,)).fetchone()
    conn.close()
    if row and row[0] is None:
        logging.info("no recorded clip for event %s yet, falling back to live capture", event_id)
        _download_clip(event_id, name, code, ip, user, pw, None)


def _handle_new_file(name, ip, user, pw, json_str):
    """Camera pushes the exact recording path via NewFile; grab video files, skip jpg (snapshot already handled live)."""
    try:
        data = json.loads(json_str.strip())
    except Exception:
        return
    path = data.get("File")
    if not path or not path.lower().endswith((".dav", ".mp4")):
        return
    threading.Thread(
        target=_download_recorded_clip,
        args=(name, ip, user, pw, path),
        daemon=True,
    ).start()


def _download_recorded_clip(name, ip, user, pw, path):
    """Download the camera's own recorded file via RPC_Loadfile and remux to mp4."""
    data = None
    for prefix in ("/cgi-bin/RPC_Loadfile", "/RPC_Loadfile"):
        try:
            r = requests.get(
                f"https://{ip}{prefix}{path}",
                auth=HTTPDigestAuth(user, pw), timeout=60, verify=False,
            )
            r.raise_for_status()
            data = r.content
            break
        except Exception as e:
            logging.debug("RPC_Loadfile via %s failed: %s", prefix, e)
    if data is None:
        logging.warning("recorded clip download failed for %s", path)
        return

    os.makedirs(config.CLIP_DIR, exist_ok=True)
    ts = int(time.time())
    fname = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(ts)) + f"_{name}_recorded.mp4"
    clip_path = os.path.join(config.CLIP_DIR, fname)
    tmp_dav = clip_path + ".dav.tmp"
    try:
        with open(tmp_dav, "wb") as f:
            f.write(data)
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_dav, "-c", "copy", clip_path],
            timeout=60, capture_output=True, check=True,
        )
    except Exception as e:
        logging.warning("recorded clip convert failed for %s: %s", path, e)
        return
    finally:
        try:
            os.remove(tmp_dav)
        except FileNotFoundError:
            pass

    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(
            "SELECT id FROM events WHERE camera=? AND clip_path IS NULL AND ts > ? "
            "ORDER BY ts DESC LIMIT 1",
            (name, ts - 120),
        ).fetchone()
        if row:
            conn.execute("UPDATE events SET clip_path=? WHERE id=?", (clip_path, row[0]))
            conn.commit()
            logging.info("recorded clip saved and linked to event %s: %s", row[0], fname)
        else:
            logging.info("recorded clip saved (no matching event to link): %s", fname)
    finally:
        conn.close()


def main():
    handler = logging.handlers.TimedRotatingFileHandler(
        config.LOG_PATH,
        when="midnight",
        backupCount=config.LOG_KEEP_DAYS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.DEBUG)
    init_db()

    for cam in config.CAMERAS:
        threading.Thread(target=listen, args=(cam,), daemon=True).start()
        logging.info("Started %s", cam['name'])

    threading.Thread(target=_disk_watchdog, daemon=True).start()

    if config.HEALTH_PORT_LISTENER:
        _start_health(config.HEALTH_PORT_LISTENER, lambda: {
            "ok": True,
            "cameras": {c["name"]: "down" if cam_down.get(c["name"]) else "up"
                        for c in config.CAMERAS},
        })
        logging.info("Health endpoint on port %d", config.HEALTH_PORT_LISTENER)

    while True:
        time.sleep(10)


if __name__ == "__main__":
    main()