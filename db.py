#!/usr/bin/env python3
"""
Storage layer for Tweppy.

Primary backend: Cloudflare D1 (SQLite) through its REST API.
Fallback backend: a local SQLite file, used when the Cloudflare variables are
missing or D1 cannot be reached. The fallback keeps the app usable locally but
does not survive a host restart on ephemeral platforms such as Koyeb or Render.

Both backends speak the same SQL dialect (SQLite), so every query in the app is
written once and runs unchanged on either.

Environment variables:
    CF_ACCOUNT_ID       Cloudflare account ID
    CF_D1_DATABASE_ID   ID of the D1 database
    CF_API_TOKEN        API token with the "Account / D1 / Edit" permission
    LOCAL_DB_PATH       Optional path for the fallback SQLite file (default: local.db)
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_D1_DATABASE_ID = os.getenv("CF_D1_DATABASE_ID")
CF_API_TOKEN = os.getenv("CF_API_TOKEN")
LOCAL_DB_PATH = os.getenv("LOCAL_DB_PATH", "local.db")

# Every timestamp is stored as a UTC string in this format. The format sorts
# lexicographically, so "scheduled_at <= ?" comparisons work on plain strings.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS tokens (
        id TEXT PRIMARY KEY,
        access_token TEXT NOT NULL,
        refresh_token TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tweet_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        posted INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS post_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        posted_at TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'profile'
    )""",
    """CREATE TABLE IF NOT EXISTS bot_stats (
        key TEXT PRIMARY KEY,
        value INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS scheduled_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        scheduled_at TEXT NOT NULL,
        posted INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS communities (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        auto_posting_active INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS community_tweet_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        community_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        posted INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS post_failures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        error TEXT NOT NULL,
        failed_at TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'profile'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_history_posted_at ON post_history (posted_at)",
    "CREATE INDEX IF NOT EXISTS idx_failures_failed_at ON post_failures (failed_at)",
    "CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled_posts (posted, scheduled_at)",
]


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def utcnow_str():
    """Current UTC time as the canonical string stored in the database."""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def to_db_str(dt):
    """Convert an aware datetime to the stored UTC string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_utc(value):
    """Parse a stored timestamp string back into an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class D1Backend:
    """Runs SQL against Cloudflare D1 over HTTPS."""

    def __init__(self, account_id, database_id, api_token):
        self.url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    def run(self, sql, params=()):
        response = requests.post(
            self.url,
            headers=self.headers,
            json={"sql": sql, "params": list(params)},
            timeout=20,
        )
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(
                f"D1 returned a non-JSON response ({response.status_code}): "
                f"{response.text[:200]}"
            )
        if not data.get("success"):
            raise RuntimeError(f"D1 query failed: {data.get('errors')}")
        result = (data.get("result") or [{}])[0]
        return result.get("results") or [], result.get("meta") or {}


class SQLiteBackend:
    """Runs SQL against a local SQLite file. Used only as a fallback."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()

    def run(self, sql, params=()):
        with self.lock:
            cursor = self.conn.execute(sql, tuple(params))
            rows = [dict(row) for row in cursor.fetchall()]
            self.conn.commit()
            meta = {
                "changes": cursor.rowcount if cursor.rowcount >= 0 else 0,
                "last_row_id": cursor.lastrowid,
            }
            return rows, meta


backend = None
connected = False        # True only while Cloudflare D1 is in use
backend_name = "none"


def init():
    """Pick a backend and make sure the schema exists. Safe to call repeatedly."""
    global backend, connected, backend_name
    if backend is not None:
        return

    if CF_ACCOUNT_ID and CF_D1_DATABASE_ID and CF_API_TOKEN:
        print("[db] Connecting to Cloudflare D1...")
        try:
            candidate = D1Backend(CF_ACCOUNT_ID, CF_D1_DATABASE_ID, CF_API_TOKEN)
            candidate.run("SELECT 1")
            backend = candidate
            connected = True
            backend_name = "Cloudflare D1"
            print("[db] Connected to Cloudflare D1.")
        except Exception as e:
            print(f"[db] Cloudflare D1 connection failed: {str(e)[:200]}")
    else:
        print("[db] CF_ACCOUNT_ID, CF_D1_DATABASE_ID or CF_API_TOKEN is not set.")

    if backend is None:
        backend = SQLiteBackend(LOCAL_DB_PATH)
        backend_name = f"local SQLite ({LOCAL_DB_PATH})"
        print(f"[db] Falling back to {backend_name}. Data will not survive a host restart.")

    for statement in SCHEMA:
        backend.run(statement)
    _load_settings_cache()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def query(sql, params=()):
    """Run a SELECT and return a list of dict rows."""
    rows, _ = backend.run(sql, params)
    return rows


def query_one(sql, params=()):
    """Run a SELECT and return the first row as a dict, or None."""
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    """Run a write statement and return its meta ("changes", "last_row_id")."""
    _, meta = backend.run(sql, params)
    return meta


def insert_many(table, columns, rows, chunk_size=40):
    """Insert many rows with a few multi-row INSERT statements.

    D1 limits bound parameters per statement, so rows are chunked.
    """
    if not rows:
        return 0
    column_list = ", ".join(columns)
    row_placeholder = "(" + ", ".join("?" * len(columns)) + ")"
    for start in range(0, len(rows), chunk_size):
        batch = rows[start:start + chunk_size]
        placeholders = ", ".join([row_placeholder] * len(batch))
        params = [value for row in batch for value in row]
        execute(f"INSERT INTO {table} ({column_list}) VALUES {placeholders}", params)
    return len(rows)


# ---------------------------------------------------------------------------
# Settings (key/value), cached in memory so workers don't pay a D1 round trip
# every time they read one. Every write goes through set_setting, which keeps
# the cache and the table in step.
# ---------------------------------------------------------------------------

_settings_cache = {}
_settings_lock = threading.Lock()


def _load_settings_cache():
    rows = query("SELECT key, value FROM settings")
    with _settings_lock:
        _settings_cache.clear()
        _settings_cache.update({row["key"]: row["value"] for row in rows})


def get_setting(key, default=None):
    with _settings_lock:
        return _settings_cache.get(key, default)


def set_setting(key, value):
    value = "" if value is None else str(value)
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    with _settings_lock:
        _settings_cache[key] = value


def delete_setting(key):
    execute("DELETE FROM settings WHERE key = ?", (key,))
    with _settings_lock:
        _settings_cache.pop(key, None)


init()
