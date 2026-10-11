import os
import unittest
from unittest import mock

import listener
import worker
from tools import feedback
import config


class FeedbackLearningTest(unittest.TestCase):
    def test_noise_feedback_pushes_threshold_up(self):
        with mock.patch.object(feedback, "_recent_scores", return_value=[
            ("noise", 0.81),
            ("noise", 0.76),
            ("human", 0.41),
            ("animal", 0.46),
        ]):
            threshold = feedback.adaptive_threshold("test")
        self.assertGreater(threshold, 0.5)

    def test_human_feedback_lowers_threshold(self):
        with mock.patch.object(feedback, "_recent_scores", return_value=[
            ("human", 0.51),
            ("animal", 0.55),
            ("noise", 0.72),
        ]):
            threshold = feedback.adaptive_threshold("test")
        self.assertLess(threshold, 0.6)

    def test_learning_can_be_toggled_via_file(self):
        try:
            os.remove(config.LEARNING_FILE)
        except FileNotFoundError:
            pass
        self.assertTrue(feedback.learning_enabled())
        feedback.set_learning_enabled(False)
        self.assertFalse(feedback.learning_enabled())
        feedback.set_learning_enabled(True)
        self.assertTrue(feedback.learning_enabled())

    def test_learning_mode_bypasses_filter(self):
        with mock.patch.object(worker, "active_filter", return_value={"VideoMotion"}), \
             mock.patch.object(worker, "learning_enabled", return_value=True):
            self.assertTrue(worker.event_allowed("SmartMotionHuman"))
            self.assertTrue(worker.event_allowed("VideoMotion"))

        with mock.patch.object(worker, "active_filter", return_value={"VideoMotion"}), \
             mock.patch.object(worker, "learning_enabled", return_value=False):
            self.assertFalse(worker.event_allowed("SmartMotionHuman"))
            self.assertTrue(worker.event_allowed("VideoMotion"))

    def test_learning_mode_keeps_snapshot_even_if_scored_as_noise(self):
        with mock.patch.object(listener, "learning_enabled", return_value=True):
            self.assertTrue(listener._snapshot_is_likely_target(b"dummy", "left"))

        with mock.patch.object(listener, "learning_enabled", return_value=False), \
             mock.patch.object(listener, "score_snapshot", return_value={"score": 0.1, "likely_human_or_animal": True}), \
             mock.patch.object(listener, "should_accept_snapshot", return_value=False):
            self.assertFalse(listener._snapshot_is_likely_target(b"dummy", "left"))


if __name__ == "__main__":
    unittest.main()
