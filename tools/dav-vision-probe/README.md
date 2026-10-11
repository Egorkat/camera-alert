# DAV Vision Probe

An isolated CPU benchmark for object detection on original camera DAV files.
It samples frames directly from the embedded DHAV stream, without making MP4s
or changing the production services. OpenCV Zoo's quantized YOLOX model detects
COCO objects such as people, cars, and animals; it does not describe actions in
natural language.

## Setup

```bash
python3 -m venv /tmp/dav-vision-venv
/tmp/dav-vision-venv/bin/pip install -r tools/dav-vision-probe/requirements.txt
```

The first run downloads the Apache-2.0 OpenCV Zoo YOLOX wrapper and quantized
ONNX model to `~/.cache/camera-alert-dav-probe/`. No model files are added to
the repository.

## Run

```bash
/tmp/dav-vision-venv/bin/python tools/dav-vision-probe/probe.py \
  /opt/camera-alert/ftp/Dacha2/2026-10-01/001/dav/14/14.14.38-14.19.09\[M\]\[0@0\]\[0\].dav \
  /opt/camera-alert/ftp/Dacha1/2026-10-01/001/dav/16/16.14.31-16.18.27\[M\]\[0@0\]\[0\].dav
```

By default it samples at 0.5 frames per second, up to 30 frames per DAV, and
uses one OpenCV CPU thread. Output reports per-file wall time, inference time,
processing speed relative to the sampled timeline, and detected object counts.
Use `--interval 1` for one frame per second or `--frames 10` for a shorter
smoke test. To gauge whether it can keep up with ongoing FTP recordings,
compare `timeline_seconds_per_second` to `1 / interval`: values at or above
that rate mean sampled frames are being processed as fast as they arrive.

The first run includes model download time only in setup, not in the per-file
benchmark. Decode and inference time are both included in the reported wall
time. A real-time factor below 1 means it cannot analyze the full recording
timeline at the selected sampling rate on this host.