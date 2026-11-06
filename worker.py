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
from tools.feedback import (
    ensure_box_feedback_table,
    learning_enabled,
    register_box_feedback,
)
from tools.snapshot_probe import annotate_detections


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


def event_allowed(event_type):
    """Allow all events while learning is enabled so feedback can tune thresholds."""
    if learning_enabled():
        return True
    return event_type in active_filter()


def _feedback_keyboard(event_id, has_snapshot=False):
    if not learning_enabled():
        return []

    rows = []
    rows.append([
        {"text": "✅ Human", "callback_data": f"feedback:{event_id}:human"},
        {"text": "🐾 Animal", "callback_data": f"feedback:{event_id}:animal"},
        {"text": "🚫 Noise", "callback_data": f"feedback:{event_id}:noise"},
    ])
    if has_snapshot:
        rows.append([
            {"text": "📦 Box OK / none needed", "callback_data": f"box:{event_id}:yes"},
            {"text": "↩️ Box wrong / missing", "callback_data": f"box:{event_id}:no"},
        ])
    return rows


def send_telegram(text, image=None, keyboard=None):
    try:
        if image:
            logging.info("sending photo to Telegram")
            url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendPhoto"
            payload = {
                "chat_id": config.CHAT_ID,
                "caption": text,
                "reply_markup": json.dumps({"inline_keyboard": keyboard or []}) if keyboard else None,
            }
            r = requests.post(
                url,
                data=payload,
                files={"photo": image},
                timeout=30,
            )
        else:
            logging.info("sending message to Telegram")
            url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
            payload = {"chat_id": config.CHAT_ID, "text": text}
            if keyboard:
                payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
            r = requests.post(
                url,
                data=payload,
                timeout=15,
            )
        r.raise_for_status()
        response = r.json()
        if not response.get("ok"):
            raise RuntimeError(f"Telegram API rejected the message: {response}")
        return response.get("result")

    except Exception as e:
        logging.error("Telegram error: %s", e)
        return False


def run():
    handler = logging.handlers.TimedRotatingFileHandler(
        f"{config.LOG_DIR}/worker.log", when="midnight",
        backupCount=config.LOG_KEEP_DAYS, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    with sqlite3.connect(config.DB_PATH) as conn:
        ensure_box_feedback_table(conn)
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
                    SELECT id, camera, event_type, raw, snapshot_path, bbox
                    FROM events
                    WHERE sent = 0
                """).fetchall()

                logging.debug("found %d unsent events", len(rows))

                for r in rows:
                    eid, cam, etype, raw, snap_path, bbox_json = r

                    logging.info("event %s: camera=%s type=%s", eid, cam, etype)

                    msg = f"🚨 {cam} — {etype}"

                    snap = None
                    detections = []
                    if snap_path and os.path.exists(snap_path):
                        try:
                            with open(snap_path, "rb") as f:
                                snap = f.read()
                        except Exception as e:
                            logging.warning("could not read snapshot %s: %s", snap_path, e)
                    if bbox_json:
                        try:
                            detections = json.loads(bbox_json)
                            if snap and detections:
                                snap = annotate_detections(snap, detections)
                        except Exception as e:
                            logging.warning("could not annotate snapshot for event %s: %s", eid, e)
                            detections = []

                    if not event_allowed(etype):
                        logging.info("filtered %s, skipping event %s", etype, eid)
                        c.execute("UPDATE events SET sent=1 WHERE id=?", (eid,))
                        conn.commit()
                        continue

                    if os.path.exists(config.MUTE_FILE):
                        logging.info("muted, skipping event %s (not sent, will not retry)", eid)
                        c.execute("UPDATE events SET sent=1 WHERE id=?", (eid,))
                        conn.commit()
                        continue

                    has_snapshot = bool(snap_path and snap)
                    keyboard = _feedback_keyboard(eid, has_snapshot) or None
                    sent_message = send_telegram(msg, snap, keyboard)
                    if sent_message:
                        if learning_enabled() and has_snapshot and isinstance(sent_message, dict):
                            try:
                                register_box_feedback(
                                    eid,
                                    cam,
                                    etype,
                                    snap_path,
                                    detections,
                                    sent_message["message_id"],
                                )
                            except Exception:
                                logging.exception("could not register box feedback for event %s", eid)
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
