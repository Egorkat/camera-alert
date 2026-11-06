import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, "/opt/camera-alert")

os.environ.setdefault("CAMERA_BOT_TOKEN", "test-token")
os.environ.setdefault("CAMERA_CHAT_ID", "123")
os.environ.setdefault("CAMERA_PASS", "test-pass")

import converter
import listener


class ConvertedClipNamingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.previous_db_path = converter.config.DB_PATH
        self.previous_clip_dir = converter.config.CLIP_DIR
        self.previous_listener_db = listener.DB
        converter.config.DB_PATH = os.path.join(self.tmpdir.name, "events.db")
        converter.config.CLIP_DIR = os.path.join(self.tmpdir.name, "clips")
        listener.DB = converter.config.DB_PATH

        conn = sqlite3.connect(converter.config.DB_PATH)
        conn.execute(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                ts INTEGER,
                event_type TEXT,
                recorded_clip_path TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        converter.config.DB_PATH = self.previous_db_path
        converter.config.CLIP_DIR = self.previous_clip_dir
        listener.DB = self.previous_listener_db
        self.tmpdir.cleanup()

    def _output_for(self, event_type):
        source_path = (
            "/opt/camera-alert/ftp/Dacha2/2026-11-05/001/dav/10/"
            "10.30.18-10.33.19[M][0@0][0].dav"
        )
        event_ts = int(time.mktime((2026, 11, 5, 10, 37, 15, 0, 0, -1)))
        conn = sqlite3.connect(converter.config.DB_PATH)
        conn.execute(
            "INSERT INTO events (ts, event_type, recorded_clip_path) VALUES (?, ?, ?)",
            (event_ts, event_type, source_path),
        )
        conn.commit()
        conn.close()
        return converter._output_path(source_path)

    def test_smart_and_cross_events_use_event_timestamp(self):
        expected = {
            "SmartMotionHuman": "2026-11-05_10-37-15_left_smart.mp4",
            "SmartMotionVehicle": "2026-11-05_10-37-15_left_smart.mp4",
            "CrossRegionDetection": "2026-11-05_10-37-15_left_crossreg.mp4",
            "CrossLineDetection": "2026-11-05_10-37-15_left_crosslin.mp4",
        }

        for event_type, filename in expected.items():
            with self.subTest(event_type=event_type):
                conn = sqlite3.connect(converter.config.DB_PATH)
                conn.execute("DELETE FROM events")
                conn.commit()
                conn.close()
                self.assertEqual(
                    os.path.basename(self._output_for(event_type)),
                    filename,
                )

    def test_motion_keeps_existing_recording_filename(self):
        output_path = self._output_for("VideoMotion")
        self.assertEqual(
            os.path.basename(output_path),
            "10.30.18-10.33.19[M][0@0][0]_left.mp4",
        )

    def test_ftp_upload_is_linked_to_triggering_event(self):
        source_path = "/opt/camera-alert/ftp/Dacha2/2026-11-05/001/dav/10/recording.dav"
        conn = sqlite3.connect(converter.config.DB_PATH)
        conn.execute(
            "INSERT INTO events (id, ts, event_type) VALUES (?, ?, ?)",
            (1, 0, "CrossRegionDetection"),
        )
        conn.commit()
        conn.close()

        with (
            patch.object(listener, "_ftp_dir_for", return_value="/ftp"),
            patch.object(listener, "_find_ftp_upload", return_value=source_path),
            patch.object(listener, "_discard_live_capture"),
        ):
            listener._fetch_recorded_clip(1, "left", "CrossRegionDetection", "", "", "", 0)

        conn = sqlite3.connect(converter.config.DB_PATH)
        linked_path = conn.execute(
            "SELECT recorded_clip_path FROM events WHERE id=1",
        ).fetchone()[0]
        conn.close()
        self.assertEqual(linked_path, source_path)


if __name__ == "__main__":
    unittest.main()
