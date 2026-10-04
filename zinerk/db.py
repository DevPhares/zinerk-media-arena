import sqlite3
from .config import DATA_DIR, DB_PATH

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash BLOB NOT NULL,
    salt          BLOB NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user'
                  CHECK (role IN ('user', 'admin')),
    root_folder   TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    last_login    INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    url              TEXT NOT NULL,
    platform         TEXT,
    resource_type    TEXT NOT NULL CHECK (resource_type IN
                     ('audio','video','image_slides','photo','webpage','document')),
    format           TEXT,
    tool             TEXT CHECK (tool IN ('yt-dlp','gallery-dl','other')),
    title            TEXT,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','running','done','failed','paused')),
    error            TEXT,
    run_when         TEXT NOT NULL DEFAULT 'now',
    scheduled_at     INTEGER,
    created_at       INTEGER NOT NULL,
    started_at       INTEGER,
    finished_at      INTEGER,
    elapsed_ms       INTEGER,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS job_destinations (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    path   TEXT NOT NULL,
    copied INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS file_moves (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id    INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    from_path TEXT NOT NULL,
    to_path   TEXT NOT NULL,
    method    TEXT,
    moved_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bandwidth_daily (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day     TEXT NOT NULL,
    tool    TEXT NOT NULL,
    bytes   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, day, tool)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (user_id, key)
);

CREATE INDEX IF NOT EXISTS idx_jobs_user   ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_url    ON jobs(url);
"""


def get_connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    conn = get_connection()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    conn = get_connection()
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    print("Tables:", [r["name"] for r in rows])
    conn.close()
