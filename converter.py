import logging
import logging.handlers
import os
import subprocess
import time
import sqlite3
import requests

import config


def _notify_conversion_failure(source_path, error):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        event_ids = [
            str(row[0]) for row in conn.execute(
                "SELECT id FROM events WHERE recorded_clip_path=? ORDER BY id",
                (source_path,),
            )
        ]
    finally:
        conn.close()

    event_label = ", ".join(event_ids) if event_ids else "unlinked DAV"
    message = (
        "⚠️ Video conversion failed\n"
        f"Event: {event_label}\n"
        f"File: {source_path}\n"
        f"Error: {error[:300]}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
            data={"chat_id": config.CHAT_ID, "text": message},
            timeout=15,
        ).raise_for_status()
    except Exception as notify_error:
        logging.warning("conversion failure notification failed: %s", notify_error)


def _init_db():
    conn = sqlite3.connect(config.DB_PATH)
    try:
        for statement in (
            "ALTER TABLE events ADD COLUMN converted_clip_path TEXT",
            "ALTER TABLE events ADD COLUMN conversion_status TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE events ADD COLUMN conversion_error TEXT",
        ):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def _day_key(source_path):
    parts = os.path.normpath(source_path).split(os.sep)
    for part in parts:
        if len(part) == 10 and part[4] == "-" and part[7] == "-":
            return part
    return None


def _day_output_dir(source_path):
    day = _day_key(source_path)
    if not day:
        return None
    return os.path.join(config.CLIP_DIR, f"{day[8:10]}-{day[5:7]}")


def _mark_day_processed(source_path, status="processed"):
    day = _day_key(source_path)
    if not day:
        return
    if day >= time.strftime("%Y-%m-%d"):
        return  # never mark today (or later) as done — more clips can still arrive before midnight
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS processed_days (day TEXT PRIMARY KEY, status TEXT NOT NULL, updated_ts INTEGER NOT NULL)",
        )
        conn.execute(
            "INSERT INTO processed_days(day, status, updated_ts) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET status=excluded.status, updated_ts=excluded.updated_ts",
            (day, status, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def _processed_days():
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS processed_days (day TEXT PRIMARY KEY, status TEXT NOT NULL, updated_ts INTEGER NOT NULL)")
        rows = conn.execute("SELECT day FROM processed_days").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _camera_label(source_path):
    parts = os.path.normpath(source_path).split(os.sep)
    for camera in config.CAMERAS:
        if os.path.basename(camera.get("ftp_dir", "")) in parts:
            return camera["name"]
    return "camera"


def _output_path(source_path):
    output_dir = _day_output_dir(source_path)
    if not output_dir:
        return None
    basename = os.path.splitext(os.path.basename(source_path))[0]
    return os.path.join(output_dir, f"{basename}_{_camera_label(source_path)}.mp4")


def _is_stable(path):
    try:
        first_size = os.path.getsize(path)
        if first_size <= 0:
            return False
        time.sleep(2)
        return os.path.getsize(path) == first_size
    except OSError:
        return False


def _dhav_offset(path):
    with open(path, "rb") as source:
        offset = 0
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                return None
            position = chunk.find(b"DHAV")
            if position >= 0:
                return offset + position
            offset += len(chunk)


def _mark_deleted_if_missing(source_path, output_path=None):
    if output_path is None:
        output_path = _output_path(source_path)
    if not output_path or not os.path.isfile(source_path):
        return False
    if os.path.exists(output_path):
        return False

    conn = sqlite3.connect(config.DB_PATH)
    try:
        updated = conn.execute(
            """
            UPDATE events
            SET conversion_status='deleted',
                conversion_error='Output file was deleted or removed manually'
            WHERE recorded_clip_path=?
              AND conversion_status IN ('unknown', 'pending', 'failed', 'converted')
            """,
            (source_path,),
        )
        conn.commit()
        if updated.rowcount:
            logging.info("marked deleted output for %s -> %s", source_path, output_path)
            return True
        return False
    finally:
        conn.close()


def _find_next_recording():
    processed_days = _processed_days()
    conn = sqlite3.connect(config.DB_PATH)
    try:
        failed = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT recorded_clip_path FROM events WHERE conversion_status='failed'"
            )
        }
        deleted = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT recorded_clip_path FROM events WHERE conversion_status='deleted'"
            )
        }
        linked = conn.execute(
            """SELECT recorded_clip_path, conversion_status, converted_clip_path, MIN(ts)
               FROM events
               WHERE recorded_clip_path IS NOT NULL
                 AND conversion_status NOT IN ('deleted')
               GROUP BY recorded_clip_path
               ORDER BY MIN(ts)"""
        ).fetchall()
    finally:
        conn.close()

    candidates = []
    for (source_path, status, current_output, _) in linked:
        day = _day_key(source_path)
        if day in processed_days:
            continue
        if source_path in deleted:
            continue
        output_path = current_output or _output_path(source_path)
        if not output_path or not os.path.isfile(source_path):
            continue
        if os.path.exists(output_path):
            continue
        if status == 'converted':
            if _mark_deleted_if_missing(source_path, output_path):
                _mark_day_processed(source_path, "deleted")
                continue
            continue
        if _dhav_offset(source_path) not in (None, 0):
            candidates.append((os.path.getmtime(source_path), source_path, output_path))

    for root, _, files in os.walk(config.FTP_DIR):
        for filename in files:
            if filename.lower().endswith(".dav"):
                source_path = os.path.join(root, filename)
                day = _day_key(source_path)
                if day in processed_days:
                    continue
                if source_path in failed or source_path in deleted:
                    continue
                output_path = _output_path(source_path)
                if output_path and not os.path.exists(output_path):
                    candidates.append((os.path.getmtime(source_path), source_path, output_path))

    if not candidates:
        return None
    _, source_path, output_path = min(candidates, key=lambda item: item[0])
    return source_path, output_path


def _mark_conversion(source_path, status, output_path=None, error=None):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            """UPDATE events
               SET conversion_status=?, converted_clip_path=COALESCE(?, converted_clip_path),
                   conversion_error=?
               WHERE recorded_clip_path=?""",
            (status, output_path, error, source_path),
        )
        conn.commit()
    finally:
        conn.close()


def _convert(source_path, output_path):
    os.makedirs(os.path.dirname(output_path), mode=0o777, exist_ok=True)
    os.chmod(os.path.dirname(output_path), 0o777)
    temporary_path = output_path + ".part.mp4"
    _mark_conversion(source_path, "pending", error=None)
    try:
        logging.info("converting %s -> %s", source_path, output_path)
        offset = _dhav_offset(source_path)
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        ]
        source = None
        if offset not in (None, 0):
            source = open(source_path, "rb")
            source.seek(offset)
            command.extend(["-f", "dhav", "-i", "pipe:0"])
            logging.info("recovering embedded DHAV stream at byte %d: %s", offset, source_path)
        else:
            command.extend(["-i", source_path])
        command.extend([
            "-t", str(config.CONVERTER_DURATION),
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", f"scale='min({config.CONVERTER_WIDTH},iw)':-2",
            "-c:v", "libx264", "-preset", config.CONVERTER_PRESET,
            "-crf", str(config.CONVERTER_CRF),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+faststart", temporary_path,
        ])
        result = subprocess.run(
            command,
            stdin=source,
            timeout=1800,
            capture_output=True,
            text=True,
        )
        if source:
            source.close()
        if result.returncode:
            diagnostic = (result.stderr or result.stdout or "ffmpeg returned an error").strip()
            raise RuntimeError(diagnostic[-1000:])
        if os.path.getsize(temporary_path) <= 0:
            raise RuntimeError("ffmpeg produced an empty file")
        os.replace(temporary_path, output_path)
        os.chmod(output_path, 0o666)
        _mark_conversion(source_path, "converted", output_path=output_path)
        _mark_day_processed(source_path, "processed")
        logging.info("converted %s (%d bytes)", output_path, os.path.getsize(output_path))
    except Exception as exc:
        _mark_conversion(source_path, "failed", error=str(exc)[:500])
        logging.warning("conversion failed for %s: %s", source_path, exc)
        _notify_conversion_failure(source_path, str(exc))
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


def run():
    handler = logging.handlers.TimedRotatingFileHandler(
        f"{config.LOG_DIR}/converter.log", when="midnight",
        backupCount=config.LOG_KEEP_DAYS, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    _init_db()
    logging.info("Converter started")

    while True:
        try:
            candidate = _find_next_recording()
            if candidate:
                source_path, output_path = candidate
                if _is_stable(source_path):
                    _convert(source_path, output_path)
            else:
                time.sleep(config.CONVERTER_INTERVAL)
        except Exception:
            logging.exception("converter scan failed")
            time.sleep(config.CONVERTER_INTERVAL)


if __name__ == "__main__":
    run()