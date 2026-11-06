#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.object_detector import COCO_CLASSES, INPUT_SIZE, letterbox_image, load_model


def _dhav_offset(path):
    with path.open("rb") as source:
        offset = 0
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                return None
            marker = chunk.find(b"DHAV")
            if marker >= 0:
                return offset + marker
            offset += len(chunk)


def _read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None if remaining == size else b"".join(chunks)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _analyze_file(path, model, interval, max_frames):
    offset = _dhav_offset(path)
    if offset is None:
        raise RuntimeError("No DHAV signature found in DAV file")

    filter_graph = (
        f"fps={1 / interval:g},"
        "scale=640:640:force_original_aspect_ratio=decrease,"
        "pad=640:640:(ow-iw)/2:(oh-ih)/2"
    )
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "dhav",
        "-i", "pipe:0", "-vf", filter_graph, "-frames:v", str(max_frames),
        "-an", "-sn", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
    ]

    detections_by_frame = []
    inference_seconds = 0.0
    start_time = time.perf_counter()
    frame_size = INPUT_SIZE * INPUT_SIZE * 3
    with path.open("rb") as source:
        source.seek(offset)
        process = subprocess.Popen(
            command,
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            frame_index = 0
            while frame_index < max_frames:
                raw = _read_exact(process.stdout, frame_size)
                if not raw:
                    break
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    INPUT_SIZE, INPUT_SIZE, 3
                )
                model_input, scale = letterbox_image(frame)
                inference_start = time.perf_counter()
                predictions = model.infer(model_input)
                inference_seconds += time.perf_counter() - inference_start

                frame_detections = []
                for x, y, width, height, score, class_id in predictions:
                    class_id = int(class_id)
                    frame_detections.append({
                        "label": COCO_CLASSES[class_id],
                        "confidence": round(float(score), 3),
                        "box_xywh": [
                            round(float(x / scale), 1),
                            round(float(y / scale), 1),
                            round(float(width / scale), 1),
                            round(float(height / scale), 1),
                        ],
                    })
                detections_by_frame.append({
                    "sample_seconds": round(frame_index * interval, 2),
                    "detections": frame_detections,
                })
                frame_index += 1
        finally:
            process.stdout.close()
            return_code = process.wait()

    if return_code != 0:
        raise RuntimeError(f"ffmpeg exited with status {return_code}")

    wall_seconds = time.perf_counter() - start_time
    frame_count = len(detections_by_frame)
    sampled_timeline = frame_count * interval
    objects = {}
    for frame in detections_by_frame:
        for detection in frame["detections"]:
            label = detection["label"]
            objects[label] = objects.get(label, 0) + 1

    return {
        "file": str(path),
        "frames_analyzed": frame_count,
        "sample_interval_seconds": interval,
        "sampled_timeline_seconds": round(sampled_timeline, 2),
        "wall_seconds": round(wall_seconds, 2),
        "inference_ms_per_frame": round(1000 * inference_seconds / frame_count, 1) if frame_count else None,
        "timeline_seconds_per_second": round(sampled_timeline / wall_seconds, 2) if wall_seconds else None,
        "object_detection_counts": objects,
        "detections_by_frame": detections_by_frame,
    }


def main():
    parser = argparse.ArgumentParser(description="Benchmark CPU object detection on original DAV files")
    parser.add_argument("dav_files", nargs="+", type=Path)
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between sampled frames (default: 2)")
    parser.add_argument("--frames", type=int, default=30, help="Maximum frames to analyze per DAV (default: 30)")
    parser.add_argument("--threads", type=int, default=1, help="OpenCV CPU threads (default: 1)")
    args = parser.parse_args()

    if args.interval <= 0 or args.frames <= 0 or args.threads <= 0:
        parser.error("interval, frames, and threads must all be positive")
    if not shutil.which("ffmpeg"):
        parser.error("ffmpeg is required and was not found on PATH")

    cv2.setNumThreads(args.threads)
    print("Loading OpenCV Zoo quantized YOLOX model on CPU...", file=sys.stderr)
    model = load_model()

    reports = []
    for path in args.dav_files:
        if not path.is_file():
            reports.append({"file": str(path), "error": "file does not exist"})
            continue
        try:
            reports.append(_analyze_file(path, model, args.interval, args.frames))
        except Exception as exc:
            reports.append({"file": str(path), "error": str(exc)})

    print(json.dumps(reports, indent=2))
    if any("error" in report for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()