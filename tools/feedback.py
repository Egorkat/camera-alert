import os
import sqlite3

import config


VALID_LABELS = {"human", "animal", "noise"}


def learning_enabled():
    return not os.path.exists(config.LEARNING_FILE)


def set_learning_enabled(enabled):
    if enabled:
        try:
            os.remove(config.LEARNING_FILE)
        except FileNotFoundError:
            pass
        return True
    with open(config.LEARNING_FILE, "w", encoding="utf-8") as f:
        f.write("0")
    return False


def ensure_feedback_table(conn=None):
    should_close = conn is None
    conn = conn or sqlite3.connect(config.DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER,
            camera TEXT,
            label TEXT CHECK(label IN ('human', 'animal', 'noise')),
            score REAL NOT NULL,
            ts INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    if should_close:
        conn.close()


def record_feedback(event_id, camera, label, score):
    label = (label or "").strip().lower()
    if label not in VALID_LABELS:
        raise ValueError(f"Unsupported feedback label: {label!r}")

    conn = sqlite3.connect(config.DB_PATH)
    ensure_feedback_table(conn)
    conn.execute(
        "INSERT INTO feedback (event_id, camera, label, score, ts) VALUES (?, ?, ?, ?, ?)",
        (event_id, camera, label, float(score), int(__import__("time").time())),
    )
    conn.commit()
    conn.close()


def _recent_scores(camera, limit=200):
    conn = sqlite3.connect(config.DB_PATH)
    ensure_feedback_table(conn)
    rows = conn.execute(
        "SELECT label, score FROM feedback WHERE camera=? ORDER BY ts DESC LIMIT ?",
        (camera, limit),
    ).fetchall()
    conn.close()
    return rows


def adaptive_threshold(camera):
    rows = _recent_scores(camera)
    if not rows:
        return 0.5

    positive = [score for label, score in rows if label in {"human", "animal"}]
    negative = [score for label, score in rows if label == "noise"]

    if positive and negative:
        avg_positive = sum(positive) / len(positive)
        avg_negative = sum(negative) / len(negative)
        return max(0.35, min(0.85, 0.5 + 0.35 * (avg_negative - avg_positive)))
    if positive:
        avg_positive = sum(positive) / len(positive)
        return max(0.35, min(0.75, avg_positive - 0.12))
    if negative:
        avg_negative = sum(negative) / len(negative)
        return max(0.45, min(0.85, avg_negative + 0.08))
    return 0.5


def should_accept_snapshot(camera, score):
    if not learning_enabled():
        return float(score) >= 0.5
    threshold = adaptive_threshold(camera)
    return float(score) >= threshold
