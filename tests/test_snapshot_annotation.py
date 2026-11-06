import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

os.environ.setdefault("CAMERA_BOT_TOKEN", "test-token")
os.environ.setdefault("CAMERA_CHAT_ID", "123")
os.environ.setdefault("CAMERA_PASS", "test-pass")

import listener
from tools.snapshot_probe import annotate_detections, annotate_snapshot


class SnapshotAnnotationTests(unittest.TestCase):
    def test_saved_event_snapshot_includes_detected_object_box(self):
        image = np.zeros((100, 120, 3), dtype=np.uint8)
        encoded, jpeg = cv2.imencode(".jpg", image)
        self.assertTrue(encoded)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "events.db")
            snapshot_dir = os.path.join(tmpdir, "snapshots")
            previous_db = listener.DB
            previous_snapshot_dir = listener.config.SNAPSHOT_DIR
            listener.DB = db_path
            listener.config.SNAPSHOT_DIR = snapshot_dir

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY,
                    ts INTEGER,
                    camera TEXT,
                    event_type TEXT,
                    raw TEXT,
                    snapshot_path TEXT,
                    bbox TEXT,
                    sent INTEGER
                )
                """
            )
            conn.commit()
            conn.close()
            try:
                with patch.object(listener, "learning_enabled", return_value=True), patch.object(
                    listener,
                    "score_snapshot",
                    return_value={"components": []},
                ), patch.object(
                    listener,
                    "detect_objects",
                    return_value=[{"label": "person", "confidence": 0.9, "bbox": [20, 25, 50, 40]}],
                ):
                    listener.save_event("left", "", "VideoMotion", "", jpeg.tobytes())

                conn = sqlite3.connect(db_path)
                snapshot_path, bbox_json = conn.execute(
                    "SELECT snapshot_path, bbox FROM events",
                ).fetchone()
                conn.close()
                saved = cv2.imread(snapshot_path)
                self.assertEqual(int(saved[25, 40, 1]), 0)
                self.assertEqual(int(saved[50, 45, 1]), 0)
                self.assertEqual(
                    json.loads(bbox_json),
                    [{"label": "person", "confidence": 0.9, "bbox": [20, 25, 50, 40]}],
                )
            finally:
                listener.DB = previous_db
                listener.config.SNAPSHOT_DIR = previous_snapshot_dir

    def test_draws_rectangle_at_detection(self):
        image = np.zeros((100, 120, 3), dtype=np.uint8)
        encoded, jpeg = cv2.imencode(".jpg", image)
        self.assertTrue(encoded)

        annotated = cv2.imdecode(
            np.frombuffer(annotate_snapshot(jpeg.tobytes(), [20, 25, 50, 40]), dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )

        self.assertGreater(int(annotated[25, 40, 1]), 200)
        self.assertGreater(int(annotated[45, 20, 1]), 200)
        self.assertEqual(int(annotated[50, 45, 1]), 0)

    def test_rejects_invalid_box_dimensions(self):
        image = np.zeros((100, 120, 3), dtype=np.uint8)
        encoded, jpeg = cv2.imencode(".jpg", image)
        self.assertTrue(encoded)

        with self.assertRaises(ValueError):
            annotate_snapshot(jpeg.tobytes(), [20, 25, 0, 40])

    def test_saves_unannotated_snapshot_when_object_detection_is_empty(self):
        image = np.zeros((100, 120, 3), dtype=np.uint8)
        encoded, jpeg = cv2.imencode(".jpg", image)
        self.assertTrue(encoded)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "events.db")
            previous_db = listener.DB
            previous_snapshot_dir = listener.config.SNAPSHOT_DIR
            listener.DB = db_path
            listener.config.SNAPSHOT_DIR = os.path.join(tmpdir, "snapshots")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY,
                    ts INTEGER,
                    camera TEXT,
                    event_type TEXT,
                    raw TEXT,
                    snapshot_path TEXT,
                    bbox TEXT,
                    sent INTEGER
                )
                """
            )
            conn.commit()
            conn.close()
            try:
                with patch.object(listener, "learning_enabled", return_value=True), patch.object(
                    listener, "score_snapshot", return_value={"components": []},
                ), patch.object(listener, "detect_objects", return_value=[]):
                    listener.save_event("left", "", "VideoMotion", "", jpeg.tobytes())

                conn = sqlite3.connect(db_path)
                snapshot_path = conn.execute("SELECT snapshot_path FROM events").fetchone()[0]
                conn.close()
                saved = cv2.imread(snapshot_path)
                self.assertEqual(int(saved[25, 40, 1]), 0)
            finally:
                listener.DB = previous_db
                listener.config.SNAPSHOT_DIR = previous_snapshot_dir

    def test_annotates_detected_objects_with_labels(self):
        image = np.zeros((100, 120, 3), dtype=np.uint8)
        encoded, jpeg = cv2.imencode(".jpg", image)
        self.assertTrue(encoded)

        annotated = cv2.imdecode(np.frombuffer(
            annotate_detections(jpeg.tobytes(), [
                {"label": "person", "bbox": [20, 25, 50, 40]},
            ]),
            dtype=np.uint8,
        ), cv2.IMREAD_COLOR)
        self.assertGreater(int(annotated[25, 40, 1]), 200)


if __name__ == "__main__":
    unittest.main()
