import importlib.util
import shutil
import threading
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
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop",
    "mouse", "remote", "keyboard", "cell phone", "microwave", "oven",
    "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
)
_BOX_CLASSES = frozenset({
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "skateboard", "surfboard",
})

_model = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def _download(url, destination, minimum_size):
    if destination.is_file() and destination.stat().st_size >= minimum_size:
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    request = urllib.request.Request(url, headers={"User-Agent": "camera-alert"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if temporary.stat().st_size < minimum_size:
            raise RuntimeError(f"Downloaded file is unexpectedly small: {destination.name}")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _create_model():
    version = tuple(int(part) for part in cv2.__version__.split("-")[0].split(".")[:2])
    if version < (4, 12):
        raise RuntimeError(
            f"YOLO object detection requires OpenCV 4.12 or newer; found {cv2.__version__}"
        )

    wrapper_path = MODEL_DIR / "yolox.py"
    model_path = MODEL_DIR / "yolox_int8bq.onnx"
    _download(WRAPPER_URL, wrapper_path, 1000)
    _download(MODEL_URL, model_path, 1_000_000)

    spec = importlib.util.spec_from_file_location("opencv_zoo_yolox", wrapper_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Could not load YOLO model wrapper: {wrapper_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.YoloX(
        str(model_path),
        confThreshold=0.35,
        objThreshold=0.25,
        backendId=cv2.dnn.DNN_BACKEND_OPENCV,
        targetId=cv2.dnn.DNN_TARGET_CPU,
    )


def load_model():
    global _model
    with _model_lock:
        if _model is None:
            _model = _create_model()
        return _model


def letterbox_image(image):
    height, width = image.shape[:2]
    scale = min(INPUT_SIZE / height, INPUT_SIZE / width)
    resized_width = int(width * scale)
    resized_height = int(height * scale)
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((INPUT_SIZE, INPUT_SIZE, 3), 114, dtype=np.uint8)
    canvas[:resized_height, :resized_width] = resized
    return canvas.astype(np.float32), scale


def detect_objects(image_bytes):
    encoded = np.frombuffer(bytes(image_bytes), dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not decode snapshot for object detection")

    model_input, scale = letterbox_image(image)
    with _inference_lock:
        predictions = load_model().infer(model_input)

    image_height, image_width = image.shape[:2]
    detections = []
    for x, y, width, height, confidence, class_id in predictions:
        class_id = int(class_id)
        if not 0 <= class_id < len(COCO_CLASSES):
            continue
        label = COCO_CLASSES[class_id]
        if label not in _BOX_CLASSES:
            continue

        left = max(0, min(image_width, round(float(x) / scale)))
        top = max(0, min(image_height, round(float(y) / scale)))
        right = max(0, min(image_width, round((float(x) + float(width)) / scale)))
        bottom = max(0, min(image_height, round((float(y) + float(height)) / scale)))
        if right <= left or bottom <= top:
            continue
        detections.append({
            "label": label,
            "confidence": float(confidence),
            "bbox": [left, top, right - left, bottom - top],
        })
    return detections
