# rest.py
"""
X (Twitter) OAuth2 token storage, refresh, and connected-account info.

Tokens live in the "tokens" table of the Tweppy database (see db.py).
The connected account's id, handle and name are cached in the settings table
so the app doesn't pay for a /users/me read on every restart.
"""
import os
import base64
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

import db

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

TOKEN_ROW_ID = "twitter_token"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
ME_URL = "https://api.x.com/2/users/me"

# X access tokens last 2 hours; refresh a little early.
TOKEN_LIFETIME = timedelta(minutes=115)

ACCOUNT_KEYS = ("x_user_id", "x_username", "x_name", "x_account_checked_at")


def get_tokens():
    row = db.query_one(
        "SELECT access_token, refresh_token, updated_at FROM tokens WHERE id = ?",
        (TOKEN_ROW_ID,),
    )
    if row:
        row["updated_at"] = db.parse_utc(row["updated_at"])
    return row


def save_tokens(access_token, refresh_token):
    db.execute(
        "INSERT OR REPLACE INTO tokens (id, access_token, refresh_token, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (TOKEN_ROW_ID, access_token, refresh_token, db.utcnow_str()),
    )


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
    save_tokens(result["access_token"], result["refresh_token"])
    return get_tokens()


def refresh_account_info(access_token=None):
    """Ask X who the token belongs to and cache it. Costs one paid read.

    Returns a dict with id/username/name, or raises on failure.
    """
    if access_token is None:
        access_token = get_valid_token()
    if not access_token:
        raise Exception("No X access token found. Run authorize.py to authorize the app.")
    response = requests.get(
        ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if response.status_code != 200:
        raise Exception(f"{response.status_code} - {response.text}")
    data = response.json().get("data", {})
    db.set_setting("x_user_id", data.get("id", ""))
    db.set_setting("x_username", data.get("username", ""))
    db.set_setting("x_name", data.get("name", ""))
    db.set_setting("x_account_checked_at", db.utcnow_str())
    print(f"[rest] Connected account: @{data.get('username')} ({data.get('id')})")
    return data


def forget_account_info():
    """Drop the cached account, e.g. before authorizing a different account."""
    for key in ACCOUNT_KEYS:
        db.delete_setting(key)


def get_account_info():
    """Cached account info, without calling X."""
    return {
        "id": db.get_setting("x_user_id") or None,
        "username": db.get_setting("x_username") or None,
        "name": db.get_setting("x_name") or None,
        "checked_at": db.get_setting("x_account_checked_at") or None,
    }


def get_valid_token(return_full_response=False):
    """Get a valid access token, refreshing if necessary.

    With return_full_response=True, returns the token row plus "user_id".
    """
    token_info = get_tokens()
    if not token_info:
        print("[rest] No token found in the database. Run authorize.py to authorize.")
        return None

    if is_expired(token_info["updated_at"]):
        token_info = _refresh(token_info)

    if not return_full_response:
        return token_info["access_token"]

    user_id = db.get_setting("x_user_id")
    if not user_id:
        try:
            user_id = refresh_account_info(token_info["access_token"]).get("id")
        except Exception as e:
            print(f"[rest] Could not fetch the connected account: {e}")
    if user_id:
        token_info["user_id"] = user_id
    return token_info
