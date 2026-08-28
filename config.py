import os

BOT_TOKEN = os.environ["CAMERA_BOT_TOKEN"]
CHAT_ID   = os.environ["CAMERA_CHAT_ID"]

CAMERAS = [
    {"name": "left",  "ip": "10.30.0.201"},
    {"name": "right", "ip": "10.30.0.202"},
    # Per-camera credentials are optional; falls back to CAMERA_USER/CAMERA_PASS:
    # {"name": "gate", "ip": "10.30.0.203", "user": "viewer", "pass": "secret"},
]

CAMERA_USER = os.environ.get("CAMERA_USER", "admin")
CAMERA_PASS = os.environ["CAMERA_PASS"]

DB_PATH       = "/opt/camera-alert/events.db"
SNAPSHOT_DIR  = "/opt/camera-alert/snapshots"
CLIP_DIR      = "/opt/camera-alert/clips"
CLIP_SECONDS  = 15  # length of live RTSP capture recorded per motion event (fallback only)
CLIP_FALLBACK_DELAY = 20  # seconds to wait for a NewFile clip before falling back to live capture
LOG_PATH      = "/opt/camera-alert/listener.log"
LOG_KEEP_DAYS = 14  # number of daily log files to keep
MUTE_FILE     = "/opt/camera-alert/muted"  # presence of this file silences alerts
FILTER_FILE   = "/opt/camera-alert/filter.json"  # JSON list of active event codes; absent = use EVENT_CODES

DISK_CHECK_PATH    = "/opt/camera-alert"  # filesystem to monitor for free space
DISK_CHECK_INTERVAL = 300   # seconds between disk space checks
DISK_ALERT_PERCENT  = 10    # send an alert when free space drops below this percent
DISK_ALERT_COOLDOWN = 21600 # seconds (6h) between repeat low-space alerts

DAILY_STATUS_HOUR   = 9   # local hour (0-23) to send the automatic daily status message
DAILY_STATUS_MINUTE = 0

# HTTP health check ports (GET / → 200 JSON).  Set to None to disable.
HEALTH_PORT_LISTENER = 8081
HEALTH_PORT_WORKER   = 8082
HEALTH_PORT_BOT      = 8083

EVENT_CODES = [
    "VideoMotion",
    "SmartMotionHuman",
]

# Event codes this camera model supports, confirmed via RemoteEventManager.getEventList
# (captured from the camera's own web UI network traffic); excludes internal/system
# events like NewFile, TimeChange, InterVideoAccess, VideoMotionInfo.
ALL_EVENT_CODES = [
    "VideoMotion",
    "SmartMotionHuman",
    "SmartMotionVehicle",
    "CrossLineDetection",
    "CrossRegionDetection",
    "AudioAnomaly",
    "AudioMutation",
    "VideoBlind",
    "OverVoltage",
    "UnderVoltage",
    "SafetyAbnormal",
    "StorageNotExist",
    "StorageFailure",
    "StorageLowSpace",
]
