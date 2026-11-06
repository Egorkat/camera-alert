#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

import cv2
import numpy as np


MIN_HUMAN_AREA_RATIO = 0.0006
MAX_HUMAN_AREA_RATIO = 0.30
MIN_ASPECT_RATIO = 0.18
MAX_ASPECT_RATIO = 3.5


def _as_bgr_image(image_or_path):
    if isinstance(image_or_path, (str, Path)):
        img = cv2.imread(str(image_or_path), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"Could not read image: {image_or_path}")
        return img
    if isinstance(image_or_path, np.ndarray):
        return image_or_path
    if isinstance(image_or_path, (bytes, bytearray)):
        arr = np.frombuffer(bytes(image_or_path), dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not decode JPEG bytes")
        return img
    raise TypeError("Expected image array, path, or JPEG bytes")


def _mask_foreground(gray):
    blurred = cv2.GaussianBlur(gray, (9, 9), 0)
    edges = cv2.Canny(blurred, 30, 100)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    edges = cv2.dilate(edges, kernel, iterations=1)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
    return edges


def score_snapshot(image_or_path):
    img = _as_bgr_image(image_or_path)
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError("Expected a 3-channel image array")

    height, width = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = _mask_foreground(gray)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    strongest = None
    for contour in contours:
        area = cv2.contourArea(contour)
        if area <= 0:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if w <= 0 or h <= 0:
            continue

        area_ratio = area / max(1.0, width * height)
        aspect = w / max(1.0, h)
        if area_ratio < MIN_HUMAN_AREA_RATIO:
            continue
        if area_ratio > MAX_HUMAN_AREA_RATIO:
            continue
        if aspect < MIN_ASPECT_RATIO or aspect > MAX_ASPECT_RATIO:
            continue
        if w < 30 or h < 40:
            continue
        if min(w, h) < 30 or max(w, h) < 80:
            continue

        compactness = area / max(1.0, w * h)
        person_score = 0.0
        person_score += min(area_ratio / 0.02, 1.0) * 0.45
        person_score += min(max(0.0, 1.0 - abs(aspect - 0.9) / 1.7), 1.0) * 0.35
        person_score += min(compactness / 0.18, 1.0) * 0.20

        if strongest is None or person_score > strongest[0]:
            strongest = (person_score, area_ratio, aspect, (x, y, w, h))

    if strongest is None:
        return {"score": 0.0, "likely_human_or_animal": False, "components": []}

    score = min(1.0, strongest[0])
    bbox = strongest[3]
    return {
        "score": round(float(score), 4),
        "likely_human_or_animal": score >= 0.5,
        "components": [
            {
                "area_ratio": round(float(strongest[1]), 6),
                "aspect_ratio": round(float(strongest[2]), 4),
                "bbox": list(map(int, bbox)),
            }
        ],
    }


def annotate_snapshot(image_bytes, bbox):
    img = _as_bgr_image(image_bytes)
    if len(bbox) != 4:
        raise ValueError("Expected bounding box as (x, y, width, height)")

    x, y, width, height = (int(value) for value in bbox)
    if width <= 0 or height <= 0:
        raise ValueError("Bounding box dimensions must be positive")

    cv2.rectangle(
        img,
        (x, y),
        (x + width, y + height),
        (0, 255, 0),
        max(2, min(img.shape[:2]) // 360),
    )
    encoded, result = cv2.imencode(".jpg", img)
    if not encoded:
        raise ValueError("Could not encode annotated snapshot")
    return result.tobytes()


def annotate_detections(image_bytes, detections):
    img = _as_bgr_image(image_bytes)
    thickness = max(2, min(img.shape[:2]) // 360)
    for detection in detections:
        x, y, width, height = (int(value) for value in detection["bbox"])
        if width <= 0 or height <= 0:
            continue
        cv2.rectangle(img, (x, y), (x + width, y + height), (0, 255, 0), thickness)
        label = detection["label"]
        cv2.putText(
            img,
            label,
            (x, max(thickness * 4, y - thickness)),
            cv2.FONT_HERSHEY_SIMPLEX,
            max(0.6, min(img.shape[:2]) / 1200),
            (0, 255, 0),
            thickness,
            cv2.LINE_AA,
        )
    encoded, result = cv2.imencode(".jpg", img)
    if not encoded:
        raise ValueError("Could not encode annotated snapshot")
    return result.tobytes()


def extract_red_rectangle(image_bytes):
    img = _as_bgr_image(image_bytes)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    red = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 100, 80), (10, 255, 255)),
        cv2.inRange(hsv, (170, 100, 80), (180, 255, 255)),
    )
    red = cv2.morphologyEx(
        red,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
    )
    contours, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    image_height, image_width = img.shape[:2]
    candidates = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width < image_width * 0.04 or height < image_height * 0.04:
            continue
        if width * height > image_width * image_height * 0.8:
            continue

        thickness = max(2, min(width, height) // 20)
        roi = red[y:y + height, x:x + width]
        top = roi[:thickness, :]
        bottom = roi[-thickness:, :]
        left = roi[:, :thickness]
        right = roi[:, -thickness:]
        edge_coverage = (
            cv2.countNonZero(top) / max(1, top.size),
            cv2.countNonZero(bottom) / max(1, bottom.size),
            cv2.countNonZero(left) / max(1, left.size),
            cv2.countNonZero(right) / max(1, right.size),
        )
        if sum(coverage >= 0.2 for coverage in edge_coverage) < 3:
            continue
        candidates.append([x, y, width, height])

    if len(candidates) != 1:
        raise ValueError(
            "Could not identify exactly one red rectangle; mark one box with a bright red outline"
        )
    return candidates[0], (image_width, image_height)


def _iter_snapshot_files(root):
    root = Path(root)
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*.jpg")):
        if path.is_file():
            yield path


def analyze_snapshot_directory(root, limit=None):
    results = []
    for idx, path in enumerate(_iter_snapshot_files(root)):
        if limit is not None and idx >= limit:
            break
        try:
            result = score_snapshot(path)
        except Exception as exc:  # pragma: no cover - guard against malformed files
            result = {"score": 0.0, "likely_human_or_animal": False, "error": str(exc), "file": str(path)}
        result["file"] = str(path)
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description="Lightweight snapshot analysis for likely human or animal movement")
    parser.add_argument("paths", nargs="+", help="Snapshot file or directory")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit of files to scan")
    args = parser.parse_args()

    payload = []
    for path in args.paths:
        payload.extend(analyze_snapshot_directory(path, limit=args.limit))

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
