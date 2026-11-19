"""
Connected X (Twitter) accounts: OAuth2 tokens, refresh, and account info.

Each connected account is one row of the "tokens" table (see db.py). The row's
id is the account key used everywhere else in the app (the account_id columns).
The first account ever connected keeps the legacy key "twitter_token"; later
ones use their X user id.

Code that talks to X does not pass an account around. Instead each thread
selects one with use_account() (a web request picks the account chosen in the
dashboard, a background worker picks the account it works for) and everything
below reads that selection.
"""
import os
import base64
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

import db

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

LEGACY_ACCOUNT_KEY = "twitter_token"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
ME_URL = "https://api.x.com/2/users/me"

# X access tokens last 2 hours; refresh a little early.
TOKEN_LIFETIME = timedelta(minutes=115)

# Tables whose rows belong to one account.
ACCOUNT_TABLES = ("tweet_queue", "scheduled_posts", "post_history", "post_failures", "community_tweet_queue")
# Settings stored once per account, as "<key>:<account key>".
ACCOUNT_SETTINGS = ("auto_post_enabled", "community_auto_post_enabled", "community_auto_post_id", "ai_voice")

_local = threading.local()
_refresh_lock = threading.Lock()
_accounts_cache = {"at": 0.0, "rows": None}
ACCOUNTS_CACHE_SECONDS = 20     # authorize.py adds accounts from another process


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def list_accounts(fresh=False):
    """Connected accounts, oldest first: key, x_user_id, username, name, checked_at, updated_at."""
    now = time.monotonic()
    if fresh or _accounts_cache["rows"] is None or now - _accounts_cache["at"] > ACCOUNTS_CACHE_SECONDS:
        rows = db.query(
            "SELECT id AS key, x_user_id, username, name, checked_at, updated_at FROM tokens ORDER BY rowid ASC"
        )
        _accounts_cache.update(at=now, rows=rows)
    return [dict(row) for row in _accounts_cache["rows"]]


def _forget_cache():
    _accounts_cache["rows"] = None


def use_account(key):
    """Select the account this thread works with. None means the first account."""
    _local.account = key


def current_account_id():
    """The selected account key, falling back to the first account. None if there are no accounts."""
    accounts = list_accounts()
    keys = [account["key"] for account in accounts]
    selected = getattr(_local, "account", None)
    if selected in keys:
        return selected
    return keys[0] if keys else None


def get_account_info(key=None):
    """Cached info for an account (default: the current one), without calling X."""
    key = key or current_account_id()
    for account in list_accounts():
        if account["key"] == key:
            return {
                "key": key,
                "id": account["x_user_id"] or None,
                "username": account["username"] or None,
                "name": account["name"] or None,
                "checked_at": account["checked_at"] or None,
            }
    return {"key": None, "id": None, "username": None, "name": None, "checked_at": None}


def _fetch_me(access_token):
    response = requests.get(ME_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    if response.status_code != 200:
        raise Exception(f"{response.status_code} - {response.text}")
    return response.json().get("data", {})


def refresh_account_info(access_token=None, key=None):
    """Ask X who an account's token belongs to and cache it. Costs one paid read.

    Returns a dict with id/username/name, or raises on failure.
    """
    key = key or current_account_id()
    if access_token is None:
        access_token = get_valid_token(key=key)
    if not access_token:
        raise Exception("No X access token found. Run authorize.py to authorize the app.")
    data = _fetch_me(access_token)
    db.execute(
        "UPDATE tokens SET x_user_id = ?, username = ?, name = ?, checked_at = ? WHERE id = ?",
        (str(data.get("id", "")), data.get("username", ""), data.get("name", ""), db.utcnow_str(), key),
    )
    _forget_cache()
    print(f"[rest] Connected account: @{data.get('username')} ({data.get('id')})")
    return data


def connect_account(access_token, refresh_token):
    """Store tokens from a finished authorization, adding the account or updating it.

    Returns (account_info, is_new). Raises if X will not say whose tokens these
    are, unless this is the very first account.
    """
    accounts = list_accounts(fresh=True)
    try:
        me = _fetch_me(access_token)
    except Exception:
        if accounts:
            raise
        me = {}     # first account: keep the tokens and look the account up later
    x_user_id = str(me.get("id") or "")

    existing = next((a for a in accounts if x_user_id and a["x_user_id"] == x_user_id), None)
    if existing is None and len(accounts) == 1 and not accounts[0]["x_user_id"]:
        existing = accounts[0]      # the one account whose identity was never looked up
    key = existing["key"] if existing else (x_user_id if accounts else LEGACY_ACCOUNT_KEY)

    save_tokens(access_token, refresh_token, key)
    if me:
        db.execute(
            "UPDATE tokens SET x_user_id = ?, username = ?, name = ?, checked_at = ? WHERE id = ?",
            (x_user_id, me.get("username", ""), me.get("name", ""), db.utcnow_str(), key),
        )
    _forget_cache()
    if not accounts:
        adopt_orphans(key)
    return get_account_info(key), existing is None


def remove_account(key):
    """Disconnect an account and delete what was waiting to be posted for it."""
    for table in ("tweet_queue", "scheduled_posts", "community_tweet_queue", "post_failures",
                  "watched_accounts", "engage_posts"):
        db.execute(f"DELETE FROM {table} WHERE account_id = ?", (key,))
    for name in ACCOUNT_SETTINGS + ("engage_mentions_since", "engage_last_check"):
        db.delete_setting(f"{name}:{key}")
    db.execute("DELETE FROM tokens WHERE id = ?", (key,))
    _forget_cache()


def adopt_orphans(key):
    """Give rows and settings from before multi-account support to one account."""
    for table in ACCOUNT_TABLES:
        db.execute(f"UPDATE {table} SET account_id = ? WHERE account_id IS NULL", (key,))
    for name in ACCOUNT_SETTINGS + ("engage_mentions_since",):
        old = db.get_setting(name)
        if old is not None:
            if db.get_setting(f"{name}:{key}") is None:
                db.set_setting(f"{name}:{key}", old)
            db.delete_setting(name)
    row = db.query_one("SELECT value FROM bot_stats WHERE key = 'posted_count'")
    if row:
        db.execute(
            "INSERT INTO bot_stats (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
            (f"posted_count:{key}", row["value"]),
        )
        db.execute("DELETE FROM bot_stats WHERE key = 'posted_count'")


def _migrate():
    """One-time move from the single-account layout. Safe to run on every start."""
    if db.get_setting("x_user_id") is not None:
        db.execute(
            "UPDATE tokens SET x_user_id = ?, username = ?, name = ?, checked_at = ? "
            "WHERE id = ? AND x_user_id IS NULL",
            (db.get_setting("x_user_id") or "", db.get_setting("x_username") or "",
             db.get_setting("x_name") or "", db.get_setting("x_account_checked_at") or "", LEGACY_ACCOUNT_KEY),
        )
        for name in ("x_user_id", "x_username", "x_name", "x_account_checked_at"):
            db.delete_setting(name)
    accounts = list_accounts(fresh=True)
    if accounts and db.get_setting("accounts_migrated") != "1":
        adopt_orphans(accounts[0]["key"])
        db.set_setting("accounts_migrated", "1")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def get_tokens(key=None):
    key = key or current_account_id()
    if not key:
        return None
    row = db.query_one(
        "SELECT id AS key, access_token, refresh_token, updated_at, x_user_id FROM tokens WHERE id = ?",
        (key,),
    )
    if row:
        row["updated_at"] = db.parse_utc(row["updated_at"])
    return row


def save_tokens(access_token, refresh_token, key):
    # UPDATE first: INSERT OR REPLACE would drop the account's name and its place in the list.
    changed = db.query(
        "UPDATE tokens SET access_token = ?, refresh_token = ?, updated_at = ? WHERE id = ? RETURNING id",
        (access_token, refresh_token, db.utcnow_str(), key),
    )
    if not changed:
        db.execute(
            "INSERT INTO tokens (id, access_token, refresh_token, updated_at) VALUES (?, ?, ?, ?)",
            (key, access_token, refresh_token, db.utcnow_str()),
        )
        _forget_cache()


def is_expired(updated_at):
    now = datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return now - updated_at > TOKEN_LIFETIME


def _refresh(token_info):
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": token_info["refresh_token"],
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
    }
    response = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)
    if response.status_code != 200:
        raise Exception(f"Failed to refresh token: {response.text}")
    result = response.json()
    save_tokens(result["access_token"], result["refresh_token"], token_info["key"])
    return get_tokens(token_info["key"])


def get_valid_token(return_full_response=False, key=None):
    """Get a valid access token for an account (default: the current one), refreshing if necessary.

    With return_full_response=True, returns the token row plus "user_id".
    """
    key = key or current_account_id()
    token_info = get_tokens(key)
    if not token_info:
        print("[rest] No token found in the database. Run authorize.py to authorize.")
        return None

    if is_expired(token_info["updated_at"]):
        # A refresh token works once, so two threads must not refresh the same account together.
        with _refresh_lock:
            token_info = get_tokens(key)
            if is_expired(token_info["updated_at"]):
                token_info = _refresh(token_info)

    if not return_full_response:
        return token_info["access_token"]

    user_id = token_info.get("x_user_id")
    if not user_id:
        try:
            user_id = refresh_account_info(token_info["access_token"], key).get("id")
        except Exception as e:
            print(f"[rest] Could not fetch the connected account: {e}")
    if user_id:
        token_info["user_id"] = user_id
    return token_info


_migrate()
