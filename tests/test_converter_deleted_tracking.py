import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, "/opt/camera-alert")

os.environ.setdefault("CAMERA_BOT_TOKEN", "test-token")
os.environ.setdefault("CAMERA_CHAT_ID", "123")
os.environ.setdefault("CAMERA_PASS", "test-pass")

import converter


class DeletedOutputTrackingTests(unittest.TestCase):
    def test_missing_converted_output_is_marked_deleted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "events.db")
            converter.config.DB_PATH = db_path

            source_path = os.path.join(tmpdir, "source.dav")
            output_path = os.path.join(tmpdir, "missing.mp4")
            with open(source_path, "wb") as handle:
                handle.write(b"fake-dav-data")

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER,
                    camera TEXT,
                    event_type TEXT,
                    raw TEXT,
                    snapshot_path TEXT,
                    clip_path TEXT,
                    sent INTEGER DEFAULT 0,
                    recorded_clip_path TEXT,
                    media_status TEXT NOT NULL DEFAULT 'unknown',
                    media_checked_ts INTEGER,
                    media_error TEXT,
                    converted_clip_path TEXT,
                    conversion_status TEXT NOT NULL DEFAULT 'unknown',
                    conversion_error TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO events (recorded_clip_path, converted_clip_path, conversion_status) VALUES (?, ?, ?)",
                (source_path, output_path, "converted"),
            )
            conn.commit()
            conn.close()

            self.assertTrue(converter._mark_deleted_if_missing(source_path, output_path))

            conn = sqlite3.connect(db_path)
            status = conn.execute(
                "SELECT conversion_status FROM events WHERE recorded_clip_path=?",
                (source_path,),
            ).fetchone()[0]
            conn.close()

            self.assertEqual(status, "deleted")

    def test_deleted_output_is_skipped_by_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "events.db")
            converter.config.DB_PATH = db_path
            converter.config.FTP_DIR = tmpdir
            converter.config.CLIP_DIR = tmpdir

            source_path = os.path.join(tmpdir, "source.dav")
            output_path = os.path.join(tmpdir, "clip.mp4")
            with open(source_path, "wb") as handle:
                handle.write(b"data")

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER,
                    camera TEXT,
                    event_type TEXT,
                    raw TEXT,
                    snapshot_path TEXT,
                    clip_path TEXT,
                    sent INTEGER DEFAULT 0,
                    recorded_clip_path TEXT,
                    media_status TEXT NOT NULL DEFAULT 'unknown',
                    media_checked_ts INTEGER,
                    media_error TEXT,
                    converted_clip_path TEXT,
                    conversion_status TEXT NOT NULL DEFAULT 'unknown',
                    conversion_error TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO events (recorded_clip_path, converted_clip_path, conversion_status) VALUES (?, ?, ?)",
                (source_path, output_path, "deleted"),
            )
            conn.commit()
            conn.close()

            self.assertIsNone(converter._find_next_recording())

    def test_oldest_missing_record_is_processed_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "events.db")
            clip_dir = os.path.join(tmpdir, "clips")
            converter.config.DB_PATH = db_path
            converter.config.FTP_DIR = tmpdir
            converter.config.CLIP_DIR = clip_dir
            os.makedirs(clip_dir, exist_ok=True)

            old_dir = os.path.join(tmpdir, "2026-09-10", "001", "dav", "00")
            new_dir = os.path.join(tmpdir, "2026-09-11", "001", "dav", "00")
            os.makedirs(old_dir, exist_ok=True)
            os.makedirs(new_dir, exist_ok=True)

            old_path = os.path.join(old_dir, "00.00.00-00.00.10[M][0@0][0].dav")
            new_path = os.path.join(new_dir, "00.00.00-00.00.10[M][0@0][0].dav")
            with open(old_path, "wb") as handle:
                handle.write(b"prefixDHAVsuffix")
            with open(new_path, "wb") as handle:
                handle.write(b"prefixDHAVsuffix")
            os.utime(old_path, (10, 10))
            os.utime(new_path, (20, 20))

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER,
                    camera TEXT,
                    event_type TEXT,
                    raw TEXT,
                    snapshot_path TEXT,
                    clip_path TEXT,
                    sent INTEGER DEFAULT 0,
                    recorded_clip_path TEXT,
                    media_status TEXT NOT NULL DEFAULT 'unknown',
                    media_checked_ts INTEGER,
                    media_error TEXT,
                    converted_clip_path TEXT,
                    conversion_status TEXT NOT NULL DEFAULT 'unknown',
                    conversion_error TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO events (recorded_clip_path, conversion_status, ts) VALUES (?, ?, ?)",
                (new_path, "unknown", 200),
            )
            conn.execute(
                "INSERT INTO events (recorded_clip_path, conversion_status, ts) VALUES (?, ?, ?)",
                (old_path, "unknown", 100),
            )
            conn.commit()
            conn.close()

            selected = converter._find_next_recording()
            self.assertEqual(selected[0], old_path)

    def test_processed_day_is_not_requeued(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "events.db")
            converter.config.DB_PATH = db_path
            converter.config.FTP_DIR = tmpdir
            converter.config.CLIP_DIR = os.path.join(tmpdir, "clips")
            os.makedirs(converter.config.CLIP_DIR, exist_ok=True)

            day_dir = os.path.join(tmpdir, "2026-09-13", "001", "dav", "00")
            os.makedirs(day_dir, exist_ok=True)
            source_path = os.path.join(day_dir, "00.00.00-00.00.10[M][0@0][0].dav")
            with open(source_path, "wb") as handle:
                handle.write(b"prefixDHAVsuffix")

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_clip_path TEXT,
                    conversion_status TEXT NOT NULL DEFAULT 'unknown',
                    conversion_error TEXT,
                    converted_clip_path TEXT,
                    ts INTEGER
                )
                """
            )
            conn.execute("CREATE TABLE IF NOT EXISTS processed_days (day TEXT PRIMARY KEY, status TEXT NOT NULL, updated_ts INTEGER NOT NULL)")
            conn.execute(
                "INSERT INTO processed_days(day, status, updated_ts) VALUES(?, ?, ?)",
                ("2026-09-13", "processed", 1),
            )
            conn.commit()
            conn.close()

            self.assertIsNone(converter._find_next_recording())


if __name__ == "__main__":
    unittest.main()
