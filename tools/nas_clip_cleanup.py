#!/usr/bin/env python3
import datetime
import os
import shlex
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path


NAS_TARGET = "master@10.10.0.50"
NAS_DIR = "/home/video"
NAS_STATE_DIR = f"{NAS_DIR}/.camera-alert-sync-state"
SSH_KEY = "/root/.ssh/camera-alert-clips"
CLIP_DIR = "/opt/camera-alert/clips"
FTP_DIR = "/opt/camera-alert/ftp"
DB_PATH = "/opt/camera-alert/events.db"
STATE_DIR = Path("/var/lib/camera-alert")
PENDING_DIR = STATE_DIR / "nas-cleanup-pending"
DONE_DIR = STATE_DIR / "nas-cleanup-done"
CONVERTER_SERVICE = "camera-converter.service"
CLEANUP_GRACE_SECONDS = 24 * 60 * 60


def _ssh(remote_command):
    result = subprocess.run(
        [
            "ssh",
            "-i", SSH_KEY,
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "Ciphers=+aes128-cbc",
            NAS_TARGET,
            remote_command,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        detail = result.stderr.strip() or f"ssh exited with {result.returncode}"
        raise RuntimeError(f"NAS query failed: {detail}")
    return result.stdout.strip()


def _synced_days():
    state_dir = shlex.quote(NAS_STATE_DIR)
    output = _ssh(
        f"if [ -d {state_dir} ]; then "
        f"find {state_dir} -maxdepth 1 -type f -name '????-??-??' -printf '%f\\n'; "
        "fi"
    )
    days = set()
    for name in output.splitlines():
        try:
            parsed = datetime.date.fromisoformat(name)
        except ValueError:
            continue
        if parsed.isoformat() == name:
            days.add(name)
    return days


def _nas_day_present(day):
    path = shlex.quote(f"{NAS_DIR}/{day}")
    result = _ssh(
        f"if [ -d {path} ]; then printf present; else printf absent; fi"
    )
    if result not in {"present", "absent"}:
        raise RuntimeError(f"Unexpected NAS directory status for {day}: {result!r}")
    return result == "present"


def _ensure_state_dirs():
    for directory in (PENDING_DIR, DONE_DIR):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)


def _mark_day_deleted(day):
    if not os.path.isfile(DB_PATH):
        raise FileNotFoundError(f"Camera Alert database is missing: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS processed_days (
                   day TEXT PRIMARY KEY,
                   status TEXT NOT NULL,
                   updated_ts INTEGER NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO processed_days(day, status, updated_ts) VALUES(?, 'deleted', ?)
               ON CONFLICT(day) DO UPDATE SET status='deleted', updated_ts=excluded.updated_ts""",
            (day, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def _ftp_day_dirs(day):
    root = Path(FTP_DIR)
    if not root.is_dir():
        return
    for current, directories, _ in os.walk(root, topdown=True, followlinks=False):
        for name in list(directories):
            path = Path(current) / name
            if name != day:
                continue
            directories.remove(name)
            if path.is_symlink():
                print(f"Skipping symlinked FTP date directory: {path}")
                continue
            yield path


def _remove_tree(path, root):
    path = Path(path)
    root = Path(root).resolve()
    if path.is_symlink():
        raise RuntimeError(f"Refusing to remove symlink: {path}")
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise RuntimeError(f"Refusing to remove path outside {root}: {path}")
    if path.exists():
        shutil.rmtree(path)
        print(f"Removed {path}")


def _converter_is_active():
    result = subprocess.run(
        ["systemctl", "show", "--property=ActiveState", "--value", CONVERTER_SERVICE],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() not in {"inactive", "failed"}


def _cleanup_local_day(day, pending_path, done_path):
    converter_was_active = _converter_is_active()
    if converter_was_active:
        subprocess.run(["systemctl", "stop", CONVERTER_SERVICE], check=True)

    try:
        _mark_day_deleted(day)

        day_date = datetime.date.fromisoformat(day)
        clip_day = Path(CLIP_DIR) / day_date.strftime("%d-%m")
        _remove_tree(clip_day, CLIP_DIR)

        for ftp_day in list(_ftp_day_dirs(day) or []):
            _remove_tree(ftp_day, FTP_DIR)

        done_path.touch(mode=0o600, exist_ok=True)
        os.chmod(done_path, 0o600)
        pending_path.unlink(missing_ok=True)
        print(f"Local cleanup completed for {day}")
    finally:
        if converter_was_active:
            subprocess.run(["systemctl", "start", CONVERTER_SERVICE], check=True)


def _process_day(day, now=None):
    try:
        parsed_day = datetime.date.fromisoformat(day)
    except ValueError:
        return

    today = datetime.date.today()
    if parsed_day.year != today.year or parsed_day >= today:
        return

    _ensure_state_dirs()
    pending_path = PENDING_DIR / f"{day}.pending"
    done_path = DONE_DIR / day

    if _nas_day_present(day):
        pending_path.unlink(missing_ok=True)
        done_path.unlink(missing_ok=True)
        return
    if done_path.exists():
        return

    if not pending_path.exists():
        pending_path.touch(mode=0o600)
        os.chmod(pending_path, 0o600)
        print(f"NAS folder for {day} is absent; starting 24-hour cleanup grace period")
        return

    current_time = time.time() if now is None else now
    if current_time - pending_path.stat().st_mtime < CLEANUP_GRACE_SECONDS:
        return
    if _nas_day_present(day):
        pending_path.unlink(missing_ok=True)
        return

    _cleanup_local_day(day, pending_path, done_path)


def run_once():
    _ensure_state_dirs()
    for day in sorted(_synced_days()):
        _process_day(day)


if __name__ == "__main__":
    run_once()
