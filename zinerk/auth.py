import hashlib
import hmac
import os
import re
import sqlite3

from .config import now_ms
from .db import get_connection

ITERATIONS = 200_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
MIN_PASSWORD_LEN = 6


def _hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)


def create_user(username, password, root_folder, role="user"):
    """Create an account. Returns the new user's id, or raises ValueError."""
    if not USERNAME_RE.match(username):
        raise ValueError("Username must be 3-32 letters, numbers or _")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters")

    salt = os.urandom(16)
    pw_hash = _hash_password(password, salt)

    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, salt, role, root_folder, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, pw_hash, salt, role, root_folder, now_ms()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("That username is already taken")
    finally:
        conn.close()


def username_exists(username):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def check_credentials(username, password):
    """Check a username and password WITHOUT changing last_login.
    Returns the user as a dict (no hash or salt), or None if wrong."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            return None
        attempt = _hash_password(password, row["salt"])
        if not hmac.compare_digest(attempt, row["password_hash"]):
            return None
        user = dict(row)
        del user["password_hash"], user["salt"]
        return user
    finally:
        conn.close()


def login(username, password):
    """A real login: checks the password and records the login time.
    Returns the user as a dict (with 'previous_login'), or None if wrong."""
    user = check_credentials(username, password)
    if user is None:
        return None

    user["previous_login"] = user["last_login"]
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?", (now_ms(), user["id"])
        )
        conn.commit()
    finally:
        conn.close()
    return user


def verify_admin(username, password):
    user = check_credentials(username, password)
    return user is not None and user["role"] == "admin"


if __name__ == "__main__":
    from .db import init_db

    init_db()
    uid = create_user("demo", "secret123", "/tmp/demo")
    print("created id:", uid)
    print("right password:", login("demo", "secret123") is not None)
    print("wrong password:", login("demo", "nope") is not None)
    try:
        create_user("DEMO", "secret123", "/tmp/demo")
    except ValueError as e:
        print("duplicate blocked:", e)

    conn = get_connection()
    conn.execute("DELETE FROM users WHERE username = 'demo'")
    conn.commit()
    conn.close()
    print("demo user cleaned up")
