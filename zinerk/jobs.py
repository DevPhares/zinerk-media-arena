from datetime import datetime

from .config import now_ms
from .db import get_connection

ALLOWED = {"status", "error", "title", "started_at", "finished_at",
           "elapsed_ms", "bytes_downloaded"}


def create_job(user_id, url, platform, resource_type, fmt, tool, destinations):
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO jobs (user_id, url, platform, resource_type, format, tool, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (user_id, url, platform, resource_type, fmt, tool, now_ms()),
        )
        job_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO job_destinations (job_id, path) VALUES (?, ?)",
            [(job_id, str(p)) for p in destinations],
        )
        conn.commit()
        return job_id
    finally:
        conn.close()


def get_job(user_id, job_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_destinations(user_id, job_id):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT d.id, d.path, d.copied FROM job_destinations d "
            "JOIN jobs j ON j.id = d.job_id WHERE d.job_id = ? AND j.user_id = ?",
            (job_id, user_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_copied(user_id, dest_id):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE job_destinations SET copied = 1 WHERE id = ? "
            "AND job_id IN (SELECT id FROM jobs WHERE user_id = ?)",
            (dest_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_job(user_id, job_id, **fields):
    bad = set(fields) - ALLOWED
    if bad:
        raise ValueError(f"Cannot update fields: {bad}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE jobs SET {sets} WHERE id = ? AND user_id = ?",
            (*fields.values(), job_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def resumable_jobs(user_id):
    """Jobs that never finished (never started, or killed mid-download)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE user_id = ? "
            "AND status IN ('pending', 'running') ORDER BY id",
            (user_id,),
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


def add_bandwidth(user_id, tool, nbytes):
    day = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO bandwidth_daily (user_id, day, tool, bytes) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, day, tool) DO UPDATE SET bytes = bytes + excluded.bytes",
            (user_id, day, tool, nbytes),
        )
        conn.commit()
    finally:
        conn.close()
