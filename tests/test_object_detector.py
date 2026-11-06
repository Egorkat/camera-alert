import unittest
from unittest.mock import patch

import cv2
import numpy as np

from tools import object_detector


class _FakeModel:
    def infer(self, _image):
        return np.array([
            [64, 32, 128, 160, 0.91, 0],
            [320, 320, 96, 96, 0.88, 56],
        ])


class ObjectDetectorTests(unittest.TestCase):
    def test_returns_person_box_and_ignores_stationary_class(self):
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        encoded, jpeg = cv2.imencode(".jpg", image)
        self.assertTrue(encoded)

        with patch.object(object_detector, "load_model", return_value=_FakeModel()):
            detections = object_detector.detect_objects(jpeg.tobytes())

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0]["label"], "person")
        self.assertEqual(detections[0]["bbox"], [20, 10, 40, 50])
        self.assertAlmostEqual(detections[0]["confidence"], 0.91)

    def test_rejects_invalid_image_bytes(self):
        with self.assertRaises(ValueError):
            object_detector.detect_objects(b"not an image")


if __name__ == "__main__":
    unittest.main()
