"""
Media library for Tweppy: images and videos that can be attached to posts.

Files are stored in the database (see the media and media_chunks tables in
db.py) so they survive restarts on hosts with temporary disks.
"""
import base64

import db

CHUNK_CHARS = 200_000            # base64 characters per row, well under D1's 2 MB value limit
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 64 * 1024 * 1024
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
VIDEO_TYPES = ("video/mp4", "video/quicktime")

COLUMNS = "id, kind, mime, provider, prompt, size, status, error, created_at"


def kind_for(mime):
    if mime in IMAGE_TYPES:
        return "image"
    if mime in VIDEO_TYPES:
        return "video"
    return None


def create_pending(kind, provider, prompt):
    """Reserve a library entry for a generation that is still running."""
    meta = db.execute(
        "INSERT INTO media (kind, mime, provider, prompt, status, created_at) VALUES (?, '', ?, ?, 'pending', ?)",
        (kind, provider, prompt[:1000], db.utcnow_str()),
    )
    return meta["last_row_id"]


def store(media_id, data, mime):
    """Save the bytes for an entry and mark it ready. Raises ValueError if unusable."""
    kind = kind_for(mime)
    if kind is None:
        raise ValueError(f"X does not accept files of type {mime or 'unknown'}.")
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_VIDEO_BYTES
    if len(data) > limit:
        raise ValueError(f"The {kind} is {len(data) // 1024 // 1024} MB, over the {limit // 1024 // 1024} MB limit.")
    encoded = base64.b64encode(data).decode()
    chunks = [(media_id, seq, encoded[start:start + CHUNK_CHARS])
              for seq, start in enumerate(range(0, len(encoded), CHUNK_CHARS))]
    db.execute("DELETE FROM media_chunks WHERE media_id = ?", (media_id,))
    db.insert_many("media_chunks", ("media_id", "seq", "data"), chunks, chunk_size=1)
    db.execute(
        "UPDATE media SET kind = ?, mime = ?, size = ?, status = 'ready', error = '' WHERE id = ?",
        (kind, mime, len(data), media_id),
    )


def add(data, mime, provider="upload", prompt=""):
    """Store a finished file in one step. Returns the new id."""
    kind = kind_for(mime)
    if kind is None:
        raise ValueError(f"X does not accept files of type {mime or 'unknown'}.")
    media_id = create_pending(kind, provider, prompt)
    try:
        store(media_id, data, mime)
    except Exception:
        delete(media_id)
        raise
    return media_id


def fail(media_id, error):
    db.execute("UPDATE media SET status = 'failed', error = ? WHERE id = ?", (str(error)[:500], media_id))


def get(media_id):
    """Library entry without its bytes, or None."""
    return db.query_one(f"SELECT {COLUMNS} FROM media WHERE id = ?", (media_id,))


def load(media_id):
    """Library entry with its bytes under "data", or None if missing or not ready."""
    item = get(media_id)
    if not item or item["status"] != "ready":
        return None
    rows = db.query("SELECT data FROM media_chunks WHERE media_id = ? ORDER BY seq ASC", (media_id,))
    item["data"] = base64.b64decode("".join(row["data"] for row in rows))
    return item


def recent(limit=40):
    return db.query(f"SELECT {COLUMNS} FROM media ORDER BY id DESC LIMIT ?", (limit,))


def delete(media_id):
    """Remove a file and detach it from any queued or scheduled post."""
    db.execute("UPDATE tweet_queue SET media_id = NULL WHERE media_id = ?", (media_id,))
    db.execute("UPDATE scheduled_posts SET media_id = NULL WHERE media_id = ?", (media_id,))
    db.execute("DELETE FROM media_chunks WHERE media_id = ?", (media_id,))
    db.execute("DELETE FROM media WHERE id = ?", (media_id,))


def fail_interrupted():
    """Generations that were running when the app stopped can never finish."""
    db.execute("UPDATE media SET status = 'failed', error = 'Interrupted by a restart.' WHERE status = 'pending'")
