import unittest

import cv2
import numpy as np

from tools.snapshot_probe import score_snapshot


def _make_image(background=(35, 55, 45), height=720, width=1280):
    return np.full((height, width, 3), background, dtype=np.uint8)


class SnapshotProbeHeuristicsTest(unittest.TestCase):
    def test_human_like_blob_is_flagged(self):
        img = _make_image()
        img[220:620, 560:710] = (90, 90, 90)
        img[260:500, 500:770] = (120, 120, 120)
        img[350:620, 540:730] = (70, 70, 70)
        result = score_snapshot(img)
        self.assertTrue(result["likely_human_or_animal"])
        self.assertGreater(result["score"], 0.5)

    def test_branch_like_noise_is_rejected(self):
        img = _make_image()
        for x in range(200, 900, 30):
            img[150:650, x:x+8] = (90, 90, 90)
        result = score_snapshot(img)
        self.assertFalse(result["likely_human_or_animal"])
        self.assertLess(result["score"], 0.5)

    def test_jpeg_bytes_are_supported(self):
        img = _make_image()
        img[220:620, 560:710] = (90, 90, 90)
        img[260:500, 500:770] = (120, 120, 120)
        img[350:620, 540:730] = (70, 70, 70)
        encode_ok, encoded = cv2.imencode(".jpg", img)
        self.assertTrue(encode_ok)
        result = score_snapshot(encoded.tobytes())
        self.assertTrue(result["likely_human_or_animal"])


if __name__ == "__main__":
    unittest.main()
