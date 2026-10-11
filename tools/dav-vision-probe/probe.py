#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np


MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/"
    "object_detection_yolox/object_detection_yolox_2022nov_int8bq.onnx"
)
WRAPPER_URL = (
    "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/"
    "object_detection_yolox/yolox.py"
)
MODEL_DIR = Path.home() / ".cache" / "camera-alert-dav-probe"
INPUT_SIZE = 640
COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
)


def _download(url, destination, minimum_size):
    if destination.is_file() and destination.stat().st_size >= minimum_size:
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    request = urllib.request.Request(url, headers={"User-Agent": "camera-alert-dav-probe"})
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    if temporary.stat().st_size < minimum_size:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded file is unexpectedly small: {destination.name}")
    temporary.replace(destination)


def _load_model():
    wrapper_path = MODEL_DIR / "yolox.py"
    model_path = MODEL_DIR / "yolox_int8bq.onnx"
    _download(WRAPPER_URL, wrapper_path, 1000)
    _download(MODEL_URL, model_path, 1_000_000)

    spec = importlib.util.spec_from_file_location("opencv_zoo_yolox", wrapper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.YoloX(
        str(model_path),
        confThreshold=0.35,
        objThreshold=0.25,
        backendId=cv2.dnn.DNN_BACKEND_OPENCV,
        targetId=cv2.dnn.DNN_TARGET_CPU,
    )


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


def _letterbox(frame):
    height, width = frame.shape[:2]
    scale = min(INPUT_SIZE / height, INPUT_SIZE / width)
    resized_width = int(width * scale)
    resized_height = int(height * scale)
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((INPUT_SIZE, INPUT_SIZE, 3), 114, dtype=np.uint8)
    canvas[:resized_height, :resized_width] = resized
    return canvas.astype(np.float32), scale


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
                model_input, scale = _letterbox(frame)
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
    model = _load_model()

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