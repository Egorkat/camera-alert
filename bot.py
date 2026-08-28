import os, time, json, sqlite3, logging, logging.handlers, datetime, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
from requests.auth import HTTPDigestAuth
from urllib3.exceptions import InsecureRequestWarning
import config

_start_time = time.time()

_MUTE_REMINDER_DELAY = 3600  # seconds before first reminder
_MUTE_CONFIRM_DELAY  = 300   # seconds to auto-unmute after reminder
_pending_timer      = None
_timer_lock         = threading.Lock()


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

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


def _api(method, **kwargs):
    r = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}",
        json=kwargs, timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _reply(chat_id, text, keyboard=None):
    try:
        kwargs = {"chat_id": chat_id, "text": text}
        if keyboard is not None:
            kwargs["reply_markup"] = {"inline_keyboard": keyboard}
        _api("sendMessage", **kwargs)
    except Exception as e:
        logging.warning("reply error: %s", e)


def _edit(chat_id, message_id, text, keyboard=None):
    try:
        kwargs = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if keyboard is not None:
            kwargs["reply_markup"] = {"inline_keyboard": keyboard}
        _api("editMessageText", **kwargs)
    except Exception as e:
        logging.warning("edit error: %s", e)


def _answer_callback(callback_id, text=None):
    def _do():
        try:
            kwargs = {"callback_query_id": callback_id}
            if text:
                kwargs["text"] = text
            _api("answerCallbackQuery", **kwargs)
        except Exception as e:
            logging.warning("answerCallbackQuery error: %s", e)
    # fire-and-forget: Telegram only needs this acked eventually, don't block the
    # main handling path on it (it was adding a full extra round-trip of latency)
    threading.Thread(target=_do, daemon=True).start()


def _back_kb():
    return [[{"text": "⬅️ Menu", "callback_data": "menu"}]]


def _main_menu_kb():
    return [
        [{"text": "📊 Status", "callback_data": "status"},
         {"text": "📷 Snap", "callback_data": "snap_menu"}],
        [{"text": "🕘 Last 5", "callback_data": "last:5"},
         {"text": "🕘 Last 20", "callback_data": "last:20"}],
        [{"text": "🔇 Mute", "callback_data": "mute"},
         {"text": "🔔 Unmute", "callback_data": "unmute"}],
        [{"text": "🎛 Filter", "callback_data": "filter_menu"},
         {"text": "💾 Disk", "callback_data": "disk"}],
        [{"text": "❓ Help", "callback_data": "help"}],
    ]


def _snap_menu_kb():
    rows = [[{"text": cam["name"], "callback_data": f"snap:{cam['name']}"}] for cam in config.CAMERAS]
    rows.append([{"text": "📷 All cameras", "callback_data": "snap:all"}])
    rows.append([{"text": "⬅️ Menu", "callback_data": "menu"}])
    return rows


def _filter_menu_kb():
    active = set(_read_filter())
    codes  = sorted(set(config.ALL_EVENT_CODES) | set(config.EVENT_CODES) | active)
    rows = [[{"text": f"{'✅' if c in active else '⬜'} {c}", "callback_data": f"filter_toggle:{c}"}]
            for c in codes]
    rows.append([{"text": "♻️ Reset defaults", "callback_data": "filter_reset"}])
    rows.append([{"text": "⬅️ Menu", "callback_data": "menu"}])
    return rows


def _send_photo(chat_id, photo_bytes, caption=""):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": ("snap.jpg", photo_bytes, "image/jpeg")},
            timeout=60,
        )
        r.raise_for_status()
    except Exception as e:
        logging.error("sendPhoto failed: %s", e)
        _reply(chat_id, f"Could not send photo: {e}")


def _fetch_snapshot(cam):
    ip   = cam["ip"]
    user = cam.get("user", config.CAMERA_USER)
    pw   = cam.get("pass", config.CAMERA_PASS)
    r = requests.get(
        f"https://{ip}/cgi-bin/snapshot.cgi?channel=1",
        auth=HTTPDigestAuth(user, pw), timeout=10, verify=False,
    )
    r.raise_for_status()
    return r.content


def _read_filter():
    try:
        with open(config.FILTER_FILE) as f:
            return sorted(set(json.load(f)))
    except (FileNotFoundError, ValueError):
        return sorted(set(config.EVENT_CODES))


def _write_filter(codes):
    with open(config.FILTER_FILE, "w") as f:
        json.dump(sorted(set(codes)), f)


HELP_TEXT = (
    "Commands:\n"
    "/menu                - interactive button menu\n"
    "/status              - last event per camera + mute state\n"
    "/disk                - disk space usage and alert threshold\n"
    "/snap [camera]       - live snapshot (all cameras if omitted)\n"
    "/last [N]            - last N events (default 5, max 20)\n"
    "/mute                - silence alerts (reminder in 1 h, auto-unmute after 5 min)\n"
    "/keepmute            - reset the 1-hour mute reminder timer\n"
    "/unmute              - re-enable alerts\n"
    "/filter              - show active event-type filter\n"
    "/filter add <code>   - enable an event type\n"
    "/filter off <code>   - disable an event type\n"
    "/filter reset        - reset to config defaults\n"
    "/help                - this message"
)


def _cmd_status(chat_id):
    _reply(chat_id, _build_status_text(), keyboard=_back_kb())


def _build_status_text():
    # Fetch live camera connection state from listener health endpoint
    cam_state = {}
    if config.HEALTH_PORT_LISTENER:
        try:
            r = requests.get(f"http://localhost:{config.HEALTH_PORT_LISTENER}/", timeout=3)
            cam_state = r.json().get("cameras", {})
        except Exception:
            pass  # health endpoint unreachable — omit connection state

    conn = sqlite3.connect(config.DB_PATH)
    try:
        lines = []
        for cam in config.CAMERAS:
            name = cam["name"]
            state = cam_state.get(name)
            icon = "🟢" if state == "up" else ("🔴" if state == "down" else "⚪")
            row = conn.execute(
                "SELECT ts, event_type FROM events WHERE camera=? ORDER BY ts DESC LIMIT 1",
                (name,),
            ).fetchone()
            if row:
                ts, etype = row
                age = int(time.time()) - ts
                m, s = divmod(age, 60)
                age_str = f"{m}m {s}s ago" if m else f"{s}s ago"
                lines.append(f"{icon} {name}: last {etype} {age_str}")
            else:
                lines.append(f"{icon} {name}: no events recorded")
    finally:
        conn.close()

    muted = os.path.exists(config.MUTE_FILE)
    lines.append(f"\nAlerts: {'MUTED' if muted else 'active'}")
    lines.append(f"Filter: {', '.join(_read_filter())}")

    import shutil
    total, _, free = shutil.disk_usage(config.DISK_CHECK_PATH)
    percent_free = free / total * 100
    disk_icon = "🟢" if percent_free >= config.DISK_ALERT_PERCENT else "🔴"
    lines.append(f"Disk: {disk_icon} {free / (1024**3):.1f} GB free ({percent_free:.1f}%)")

    return "\n".join(lines)


def _cmd_disk(chat_id):
    _reply(chat_id, _build_disk_text(), keyboard=_back_kb())


def _build_disk_text():
    import shutil
    total, used, free = shutil.disk_usage(config.DISK_CHECK_PATH)
    percent_free = free / total * 100
    gb = 1024 ** 3
    icon = "🟢" if percent_free >= config.DISK_ALERT_PERCENT else "🔴"
    return (
        f"{icon} Disk usage ({config.DISK_CHECK_PATH}):\n"
        f"Total: {total / gb:.1f} GB\n"
        f"Used:  {used / gb:.1f} GB\n"
        f"Free:  {free / gb:.1f} GB ({percent_free:.1f}%)\n"
        f"Alert threshold: {config.DISK_ALERT_PERCENT}% free"
    )


def _cmd_snap(chat_id, args):
    names = [a.lower() for a in args] if args else [c["name"] for c in config.CAMERAS]
    cams  = [c for c in config.CAMERAS if c["name"] in names]
    if not cams:
        _reply(chat_id, f"Unknown camera. Available: {', '.join(c['name'] for c in config.CAMERAS)}")
        return
    _reply(chat_id, f"Fetching {len(cams)} snapshot(s)...")
    for cam in cams:
        try:
            logging.info("snap: fetching %s", cam['name'])
            data = _fetch_snapshot(cam)
            logging.info("snap: got %d bytes from %s, uploading", len(data), cam['name'])
            _send_photo(chat_id, data, caption=f"snap: {cam['name']}")
            logging.info("snap: sent %s", cam['name'])
        except Exception as e:
            logging.error("snap error %s: %s", cam['name'], e)
            _reply(chat_id, f"Error {cam['name']}: {e}")


def _cmd_last(chat_id, args):
    n = 5
    if args:
        try:
            n = min(int(args[0]), 20)
        except ValueError:
            pass
    _reply(chat_id, _build_last_text(n), keyboard=_back_kb())


def _build_last_text(n):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT ts, camera, event_type FROM events ORDER BY ts DESC LIMIT ?", (n,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return "No events yet."
    lines = [f"- {datetime.datetime.fromtimestamp(ts).strftime('%m-%d %H:%M:%S')} [{cam}] {etype}"
             for ts, cam, etype in rows]
    return "\n".join(lines)


def _do_unmute() -> str:
    """Core unmute logic shared by /unmute and auto-unmute; returns reply text."""
    _cancel_pending_timer()
    try:
        os.remove(config.MUTE_FILE)
    except FileNotFoundError:
        pass
    errors = _set_camera_email(True)
    msg = "Alerts re-enabled and camera email alerts restored."
    if errors:
        msg += "\n\u26a0\ufe0f Email enable failed for: " + ", ".join(errors)
    return msg


def _cancel_pending_timer():
    global _pending_timer
    with _timer_lock:
        if _pending_timer is not None:
            _pending_timer.cancel()
            _pending_timer = None


def _schedule_timer(delay, fn):
    global _pending_timer
    with _timer_lock:
        if _pending_timer is not None:
            _pending_timer.cancel()
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()
        _pending_timer = t


def _schedule_reminder():
    _schedule_timer(_MUTE_REMINDER_DELAY, _send_reminder)


def _send_reminder():
    if not os.path.exists(config.MUTE_FILE):
        return
    _reply(config.CHAT_ID,
           "\u23f0 Alerts are still muted.\n"
           "Send /keepmute to stay muted for another hour, or /unmute to re-enable.\n"
           "Auto-unmuting in 5 minutes if no reply.")
    _schedule_timer(_MUTE_CONFIRM_DELAY, _auto_unmute)


def _auto_unmute():
    if not os.path.exists(config.MUTE_FILE):
        return
    logging.info("auto-unmuting after no response to reminder")
    msg = _do_unmute()
    _reply(config.CHAT_ID, "No reply received — " + msg)


def _set_camera_email(enable: bool) -> list[str]:
    """Toggle email alerts on every camera; returns list of error strings."""
    errors = []
    value  = "true" if enable else "false"
    for cam in config.CAMERAS:
        ip   = cam["ip"]
        user = cam.get("user", config.CAMERA_USER)
        pw   = cam.get("pass", config.CAMERA_PASS)
        try:
            r = requests.get(
                f"https://{ip}/cgi-bin/configManager.cgi"
                f"?action=setConfig&Email.Enable={value}",
                auth=HTTPDigestAuth(user, pw),
                timeout=10,
                verify=False,
            )
            r.raise_for_status()
            logging.info("camera %s email=%s", cam["name"], value)
        except Exception as e:
            logging.warning("camera %s email toggle failed: %s", cam["name"], e)
            errors.append(f"{cam['name']}: {e}")
    return errors


def _do_mute():
    """Core mute logic shared by /mute and the menu button; returns reply text."""
    already = os.path.exists(config.MUTE_FILE)
    open(config.MUTE_FILE, "w").close()
    if already:
        _schedule_reminder()
        return "Still muted — reminder reset. You'll be asked again in 1 hour."
    errors = _set_camera_email(False)
    _schedule_reminder()
    msg = "Alerts muted and camera email alerts disabled. I'll remind you in 1 hour."
    if errors:
        msg += "\n⚠️ Email disable failed for: " + ", ".join(errors)
    return msg


def _cmd_mute(chat_id):
    _reply(chat_id, _do_mute(), keyboard=_back_kb())


def _do_keepmute():
    if not os.path.exists(config.MUTE_FILE):
        return "Not currently muted."
    _schedule_reminder()
    return "OK, staying muted. I'll remind you again in 1 hour."


def _cmd_keepmute(chat_id):
    _reply(chat_id, _do_keepmute(), keyboard=_back_kb())


def _cmd_unmute(chat_id):
    _reply(chat_id, _do_unmute(), keyboard=_back_kb())


def _cmd_filter(chat_id, args):
    if not args:
        _reply(chat_id, "Active alert filter (tap to toggle):", keyboard=_filter_menu_kb())
        return
    sub  = args[0].lower()
    code = args[1] if len(args) > 1 else ""
    if sub == "reset":
        try:
            os.remove(config.FILTER_FILE)
        except FileNotFoundError:
            pass
        _reply(chat_id, f"Filter reset to defaults: {', '.join(sorted(config.EVENT_CODES))}")
    elif sub == "add":
        if not code:
            _reply(chat_id, "Usage: /filter add <EventCode>"); return
        codes = set(_read_filter()); codes.add(code); _write_filter(codes)
        _reply(chat_id, f"Added {code}. Active: {', '.join(sorted(codes))}")
    elif sub == "off":
        if not code:
            _reply(chat_id, "Usage: /filter off <EventCode>"); return
        codes = set(_read_filter()); codes.discard(code)
        if not codes:
            _reply(chat_id, "Cannot disable all event types - filter unchanged."); return
        _write_filter(codes)
        _reply(chat_id, f"Removed {code}. Active: {', '.join(sorted(codes))}")
    else:
        _reply(chat_id, "Usage: /filter | /filter add <code> | /filter off <code> | /filter reset")


def _handle_message(msg):
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(config.CHAT_ID):
        logging.warning("Ignored message from unauthorised chat_id %s", chat_id)
        return
    text = msg.get("text", "").strip()
    if not text.startswith("/"):
        return
    parts   = text.lstrip("/").split()
    command = parts[0].lower().split("@")[0]
    args    = parts[1:]
    logging.info("CMD /%s %s", command, args)
    dispatch = {
        "help":    lambda: _reply(chat_id, HELP_TEXT, keyboard=_back_kb()),
        "menu":    lambda: _reply(chat_id, "Camera Alert menu:", keyboard=_main_menu_kb()),
        "start":   lambda: _reply(chat_id, "Camera Alert menu:", keyboard=_main_menu_kb()),
        "status":  lambda: _cmd_status(chat_id),
        "disk":    lambda: _cmd_disk(chat_id),
        "snap":    lambda: _cmd_snap(chat_id, args),
        "last":    lambda: _cmd_last(chat_id, args),
        "mute":     lambda: _cmd_mute(chat_id),
        "keepmute": lambda: _cmd_keepmute(chat_id),
        "unmute":   lambda: _cmd_unmute(chat_id),
        "filter":  lambda: _cmd_filter(chat_id, args),
    }
    fn = dispatch.get(command)
    if fn:
        fn()
    else:
        _reply(chat_id, f"Unknown command: /{command}\n\n{HELP_TEXT}", keyboard=_back_kb())


def _handle_callback(cq):
    chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != str(config.CHAT_ID):
        logging.warning("Ignored callback from unauthorised chat_id %s", chat_id)
        _answer_callback(cq["id"])
        return

    message_id = cq["message"]["message_id"]
    data       = cq.get("data", "")
    logging.info("CALLBACK %s", data)
    _answer_callback(cq["id"])

    if data == "menu":
        _edit(chat_id, message_id, "Camera Alert menu:", _main_menu_kb())
    elif data == "help":
        _edit(chat_id, message_id, HELP_TEXT, _back_kb())
    elif data == "status":
        _edit(chat_id, message_id, _build_status_text(), _back_kb())
    elif data == "disk":
        _edit(chat_id, message_id, _build_disk_text(), _back_kb())
    elif data == "snap_menu":
        _edit(chat_id, message_id, "Pick a camera:", _snap_menu_kb())
    elif data.startswith("snap:"):
        name = data.split(":", 1)[1]
        args = [] if name == "all" else [name]
        _cmd_snap(chat_id, args)
    elif data.startswith("last:"):
        n = int(data.split(":", 1)[1])
        _edit(chat_id, message_id, _build_last_text(n), _back_kb())
    elif data == "mute":
        _edit(chat_id, message_id, _do_mute(), _back_kb())
    elif data == "unmute":
        _edit(chat_id, message_id, _do_unmute(), _back_kb())
    elif data == "filter_menu":
        _edit(chat_id, message_id, "Active alert filter (tap to toggle):", _filter_menu_kb())
    elif data.startswith("filter_toggle:"):
        code  = data.split(":", 1)[1]
        codes = set(_read_filter())
        if code in codes:
            if len(codes) == 1:
                _answer_callback(cq["id"], "Cannot disable all event types.")
                return
            codes.discard(code)
        else:
            codes.add(code)
        _write_filter(codes)
        _edit(chat_id, message_id, "Active alert filter (tap to toggle):", _filter_menu_kb())
    elif data == "filter_reset":
        try:
            os.remove(config.FILTER_FILE)
        except FileNotFoundError:
            pass
        _edit(chat_id, message_id, "Active alert filter (tap to toggle):", _filter_menu_kb())


def _daily_status_scheduler():
    import datetime as _dt
    while True:
        now = _dt.datetime.now()
        target = now.replace(hour=config.DAILY_STATUS_HOUR, minute=config.DAILY_STATUS_MINUTE,
                              second=0, microsecond=0)
        if target <= now:
            target += _dt.timedelta(days=1)
        time.sleep((target - now).total_seconds())
        try:
            _reply(config.CHAT_ID, "📅 Daily status:\n" + _build_status_text())
        except Exception as e:
            logging.warning("daily status send error: %s", e)


def main():
    handler = logging.handlers.TimedRotatingFileHandler(
        "/opt/camera-alert/bot.log", when="midnight",
        backupCount=config.LOG_KEEP_DAYS, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    logging.info("Bot started")

    if os.path.exists(config.MUTE_FILE):
        # re-arm the reminder timer so mute doesn't silently persist across restarts
        _schedule_reminder()
        logging.info("mute file present at startup, reminder re-armed")
        _reply(config.CHAT_ID, "⚠️ Bot restarted while muted — reminder timer reset. Send /unmute to re-enable.")

    if config.HEALTH_PORT_BOT:
        _start_health(config.HEALTH_PORT_BOT, lambda: {
            "ok": True,
            "uptime_s": int(time.time() - _start_time),
            "muted": os.path.exists(config.MUTE_FILE),
        })
        logging.info("Health endpoint on port %d", config.HEALTH_PORT_BOT)

    threading.Thread(target=_daily_status_scheduler, daemon=True).start()

    offset = None
    while True:
        try:
            params = {"timeout": 5, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            r = requests.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates",
                json=params, timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                cq  = update.get("callback_query")
                if msg:
                    _handle_message(msg)
                elif cq:
                    _handle_callback(cq)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 409:
                logging.error("Bot loop error: %s (conflict, waiting 20s)", e)
                time.sleep(20)
            else:
                logging.error("Bot loop error: %s", e)
                time.sleep(5)
        except Exception as e:
            logging.error("Bot loop error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
