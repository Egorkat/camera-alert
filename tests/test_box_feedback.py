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

import bot
import config
import worker
from tools import feedback


class BoxFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.previous_db_path = config.DB_PATH
        self.previous_snapshot_dir = config.SNAPSHOT_DIR
        self.previous_learning_file = config.LEARNING_FILE
        config.DB_PATH = os.path.join(self.tmpdir.name, "events.db")
        config.SNAPSHOT_DIR = os.path.join(self.tmpdir.name, "snapshots")
        config.LEARNING_FILE = os.path.join(self.tmpdir.name, "learning_enabled")
        os.makedirs(config.SNAPSHOT_DIR)

    def tearDown(self):
        config.DB_PATH = self.previous_db_path
        config.SNAPSHOT_DIR = self.previous_snapshot_dir
        config.LEARNING_FILE = self.previous_learning_file
        self.tmpdir.cleanup()

    def _register(self, event_id=17, snapshot_path="snapshot.jpg", predicted_boxes=None):
        feedback.register_box_feedback(
            event_id,
            "left",
            "VideoMotion",
            snapshot_path,
            predicted_boxes if predicted_boxes is not None else [
                {"label": "person", "confidence": 0.9, "bbox": [20, 10, 40, 60]}
            ],
            801,
        )

    def test_vote_and_correction_are_persisted(self):
        self._register()
        self.assertTrue(feedback.has_box_feedback(17))
        self.assertTrue(feedback.record_box_vote(17, False))
        self.assertTrue(feedback.set_box_correction_prompt(17, 802))
        self.assertEqual(feedback.pending_box_feedback(802), (17, "snapshot.jpg"))
        self.assertTrue(feedback.record_box_correction(17, [10, 15, 50, 70], "marked.jpg"))
        self.assertIsNone(feedback.pending_box_feedback(802))

        with sqlite3.connect(config.DB_PATH) as conn:
            row = conn.execute(
                "SELECT status, corrected_bbox, marked_image_path FROM box_feedback WHERE event_id=17"
            ).fetchone()
        self.assertEqual(row, ("corrected", json.dumps([10, 15, 50, 70]), "marked.jpg"))

    def test_none_reply_is_persisted_as_no_object(self):
        self._register()
        self.assertTrue(feedback.record_box_vote(17, False))
        self.assertTrue(feedback.record_no_object(17))

        with sqlite3.connect(config.DB_PATH) as conn:
            (status,) = conn.execute(
                "SELECT status FROM box_feedback WHERE event_id=17"
            ).fetchone()
        self.assertEqual(status, "no_object")

    def test_bot_accepts_none_as_a_reply_to_the_correction_prompt(self):
        self._register()
        feedback.record_box_vote(17, False)
        feedback.set_box_correction_prompt(17, 802)

        with patch.object(bot, "_reply") as reply:
            handled = bot._handle_pending_box_reply(
                {
                    "reply_to_message": {"message_id": 802},
                    "text": "none",
                },
                "123",
            )

        self.assertTrue(handled)
        self.assertEqual(feedback.pending_box_feedback(802), None)
        reply.assert_called_once_with("123", "Thanks — saved this as a false detection for training.")

    def test_invalid_markup_keeps_original_prompt_retryable(self):
        self._register()
        feedback.record_box_vote(17, False)
        feedback.set_box_correction_prompt(17, 802)

        with patch.object(bot, "_handle_box_correction", side_effect=ValueError("no red rectangle")), patch.object(
            bot, "_reply",
        ) as reply:
            handled = bot._handle_pending_box_reply(
                {
                    "reply_to_message": {"message_id": 802},
                    "photo": [{"file_id": "photo"}],
                },
                "123",
            )

        self.assertTrue(handled)
        self.assertEqual(feedback.pending_box_feedback(802), (17, "snapshot.jpg"))
        self.assertEqual(reply.call_args.kwargs["reply_to_message_id"], 802)
        self.assertIn("one bright red rectangle", reply.call_args.args[1])

    def test_corrected_photo_coordinates_scale_to_original_snapshot(self):
        original = np.zeros((200, 300, 3), dtype=np.uint8)
        encoded, original_jpeg = cv2.imencode(".jpg", original)
        self.assertTrue(encoded)
        snapshot_path = os.path.join(config.SNAPSHOT_DIR, "original.jpg")
        with open(snapshot_path, "wb") as image_file:
            image_file.write(original_jpeg.tobytes())

        marked = np.zeros((100, 150, 3), dtype=np.uint8)
        cv2.rectangle(marked, (20, 10), (80, 60), (0, 0, 255), 3)
        encoded, marked_jpeg = cv2.imencode(".jpg", marked)
        self.assertTrue(encoded)

        self._register(snapshot_path=snapshot_path)
        feedback.record_box_vote(17, False)
        feedback.set_box_correction_prompt(17, 802)
        with patch.object(bot, "_download_telegram_photo", return_value=marked_jpeg.tobytes()), patch.object(
            bot, "_reply",
        ):
            bot._handle_box_correction({}, "123", 17, snapshot_path)

        with sqlite3.connect(config.DB_PATH) as conn:
            bbox_json, marked_path = conn.execute(
                "SELECT corrected_bbox, marked_image_path FROM box_feedback WHERE event_id=17"
            ).fetchone()
        bbox = json.loads(bbox_json)
        self.assertLessEqual(bbox[0], 40)
        self.assertLessEqual(bbox[1], 20)
        self.assertGreaterEqual(bbox[0] + bbox[2], 160)
        self.assertGreaterEqual(bbox[1] + bbox[3], 120)
        self.assertTrue(os.path.isfile(marked_path))

    def test_worker_includes_box_buttons_when_detector_finds_no_object(self):
        with patch.object(worker, "learning_enabled", return_value=True):
            keyboard = worker._feedback_keyboard(17, has_snapshot=True)
        self.assertEqual(keyboard[1][0]["callback_data"], "box:17:yes")
        self.assertEqual(keyboard[1][1]["callback_data"], "box:17:no")
        self.assertIn("missing", keyboard[1][1]["text"])

    def test_worker_omits_all_feedback_buttons_when_feedback_is_disabled(self):
        with patch.object(worker, "learning_enabled", return_value=False):
            self.assertEqual(worker._feedback_keyboard(17, has_snapshot=True), [])

    def test_box_button_labels_match_the_post_classification_menu(self):
        labels = [button["text"] for button in bot._box_vote_keyboard(17)[0]]
        self.assertEqual(labels, ["📦 Box OK / none needed", "↩️ Box wrong / missing"])

    def test_old_box_feedback_button_is_rejected_when_feedback_is_disabled(self):
        callback = {
            "id": "callback-1",
            "message": {"chat": {"id": "123"}, "message_id": 801},
            "data": "box:17:yes",
        }
        with patch.object(bot, "learning_enabled", return_value=False), \
             patch.object(bot, "_answer_callback") as answer, \
             patch.object(bot, "record_box_vote") as record_vote:
            bot._handle_callback(callback)
        answer.assert_called_once_with("callback-1", "Feedback collection is OFF")
        record_vote.assert_not_called()

    def test_empty_detection_list_is_registered_for_box_feedback(self):
        self._register(predicted_boxes=[])
        self.assertTrue(feedback.has_box_feedback(17))



if __name__ == "__main__":
    unittest.main()
