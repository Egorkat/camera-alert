import sqlite3
import requests
import time
import os
import json
import logging
import logging.handlers
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import config


def _start_health(port, get_status):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(get_status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    srv = HTTPServer(("", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def active_filter():
    """Return the set of event codes that should trigger alerts."""
    try:
        with open(config.FILTER_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, ValueError):
        return set(config.EVENT_CODES)


def send_telegram(text, image=None):
    try:
        if image:
            logging.info("sending photo to Telegram")
            url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendPhoto"
            r = requests.post(
                url,
                data={"chat_id": config.CHAT_ID, "caption": text},
                files={"photo": image},
                timeout=30,
            )
        else:
            logging.info("sending message to Telegram")
            url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
            r = requests.post(
                url,
                data={"chat_id": config.CHAT_ID, "text": text},
                timeout=15,
            )
        logging.debug("Telegram %s %s", r.status_code, r.text[:100])
        r.raise_for_status()
        return True

    except Exception as e:
        logging.error("Telegram error: %s", e)
        return False


def run():
    handler = logging.handlers.TimedRotatingFileHandler(
        "/opt/camera-alert/worker.log", when="midnight",
        backupCount=config.LOG_KEEP_DAYS, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    logging.info("Worker started, polling DB")

    if config.HEALTH_PORT_WORKER:
        def _status():
            try:
                conn = sqlite3.connect(config.DB_PATH)
                (unsent,) = conn.execute("SELECT COUNT(*) FROM events WHERE sent=0").fetchone()
                conn.close()
            except Exception:
                unsent = -1
            return {"ok": True, "unsent_events": unsent}
        _start_health(config.HEALTH_PORT_WORKER, _status)
        logging.info("Health endpoint on port %d", config.HEALTH_PORT_WORKER)

    while True:
        try:
            conn = sqlite3.connect(config.DB_PATH)
            try:
                c = conn.cursor()

                rows = c.execute("""
                    SELECT id, camera, event_type, raw, snapshot_path
                    FROM events
                    WHERE sent = 0
                """).fetchall()

                logging.debug("found %d unsent events", len(rows))

                for r in rows:
                    eid, cam, etype, raw, snap_path = r

                    logging.info("event %s: camera=%s type=%s", eid, cam, etype)

                    msg = f"🚨 {cam} — {etype}"

                    snap = None
                    if snap_path and os.path.exists(snap_path):
                        try:
                            with open(snap_path, "rb") as f:
                                snap = f.read()
                        except Exception as e:
                            logging.warning("could not read snapshot %s: %s", snap_path, e)

                    if etype not in active_filter():
                        logging.info("filtered %s, skipping event %s", etype, eid)
                        c.execute("UPDATE events SET sent=1 WHERE id=?", (eid,))
                        conn.commit()
                        continue

                    if os.path.exists(config.MUTE_FILE):
                        logging.info("muted, skipping event %s (not sent, will not retry)", eid)
                        c.execute("UPDATE events SET sent=1 WHERE id=?", (eid,))
                        conn.commit()
                        continue

                    if send_telegram(msg, snap):
                        c.execute("UPDATE events SET sent=1 WHERE id=?", (eid,))
                        conn.commit()
                        logging.info("sent event %s", eid)
                    else:
                        logging.warning("send failed for event %s, will retry", eid)

                    time.sleep(1)  # respect Telegram's 1 msg/sec per-chat rate limit

            finally:
                conn.close()

            time.sleep(2)

        except Exception as e:
            logging.error("worker loop error: %s", e)
            time.sleep(2)


if __name__ == "__main__":
    run()
