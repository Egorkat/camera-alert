import json
import os
import sqlite3
import time

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
        (event_id, camera, label, float(score), int(time.time())),
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


def ensure_box_feedback_table(conn=None):
    should_close = conn is None
    conn = conn or sqlite3.connect(config.DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS box_feedback (
            event_id INTEGER PRIMARY KEY,
            camera TEXT NOT NULL,
            event_type TEXT NOT NULL,
            snapshot_path TEXT NOT NULL,
            predicted_boxes TEXT NOT NULL,
            status TEXT NOT NULL CHECK(
                status IN ('pending', 'correct', 'awaiting_correction', 'corrected', 'no_object')
            ),
            alert_message_id INTEGER,
            prompt_message_id INTEGER,
            corrected_bbox TEXT,
            marked_image_path TEXT,
            ts INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    if should_close:
        conn.close()


def register_box_feedback(event_id, camera, event_type, snapshot_path, predicted_boxes, alert_message_id):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        ensure_box_feedback_table(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO box_feedback (
                event_id, camera, event_type, snapshot_path, predicted_boxes,
                status, alert_message_id, prompt_message_id, corrected_bbox,
                marked_image_path, ts
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL, ?)
            """,
            (
                event_id,
                camera,
                event_type,
                snapshot_path,
                json.dumps(predicted_boxes),
                alert_message_id,
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_box_vote(event_id, correct):
    status = "correct" if correct else "awaiting_correction"
    conn = sqlite3.connect(config.DB_PATH)
    try:
        ensure_box_feedback_table(conn)
        updated = conn.execute(
            "UPDATE box_feedback SET status=? WHERE event_id=?",
            (status, event_id),
        )
        conn.commit()
        return updated.rowcount > 0
    finally:
        conn.close()


def has_box_feedback(event_id):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        ensure_box_feedback_table(conn)
        return conn.execute(
            "SELECT 1 FROM box_feedback WHERE event_id=?",
            (event_id,),
        ).fetchone() is not None
    finally:
        conn.close()


def set_box_correction_prompt(event_id, prompt_message_id):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        ensure_box_feedback_table(conn)
        updated = conn.execute(
            """
            UPDATE box_feedback
            SET prompt_message_id=?
            WHERE event_id=? AND status='awaiting_correction'
            """,
            (prompt_message_id, event_id),
        )
        conn.commit()
        return updated.rowcount > 0
    finally:
        conn.close()


def pending_box_feedback(prompt_message_id):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        ensure_box_feedback_table(conn)
        return conn.execute(
            """
            SELECT event_id, snapshot_path
            FROM box_feedback
            WHERE prompt_message_id=? AND status='awaiting_correction'
            """,
            (prompt_message_id,),
        ).fetchone()
    finally:
        conn.close()


def record_box_correction(event_id, corrected_bbox, marked_image_path):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        ensure_box_feedback_table(conn)
        updated = conn.execute(
            """
            UPDATE box_feedback
            SET status='corrected', corrected_bbox=?, marked_image_path=?
            WHERE event_id=? AND status='awaiting_correction'
            """,
            (json.dumps(corrected_bbox), marked_image_path, event_id),
        )
        conn.commit()
        return updated.rowcount > 0
    finally:
        conn.close()


def record_no_object(event_id):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        ensure_box_feedback_table(conn)
        updated = conn.execute(
            """
            UPDATE box_feedback
            SET status='no_object'
            WHERE event_id=? AND status='awaiting_correction'
            """,
            (event_id,),
        )
        conn.commit()
        return updated.rowcount > 0
    finally:
        conn.close()
