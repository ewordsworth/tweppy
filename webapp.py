#!/usr/bin/env python3
"""
Tweppy web dashboard.

Storage is Cloudflare D1 (see db.py). Timestamps are stored as UTC strings and
converted back to aware datetimes before rendering; the browser formats them in
the viewer's local time. Server-side features that depend on the user's clock
(quiet hours, analytics day buckets) use the timezone saved in Settings.
"""
import csv
import io
import os
import re
import threading
from datetime import datetime, timezone, timedelta, time as dtime
from functools import wraps
from zoneinfo import ZoneInfo, available_timezones

import requests
from dotenv import load_dotenv
from flask import (Flask, Response, render_template, request, redirect, url_for, flash,
                   jsonify, session, make_response)

import db
import rest
from rest import get_valid_token

load_dotenv()

app = Flask(__name__)
# A fixed SECRET_KEY keeps sessions (e.g. the active community) alive across restarts.
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(24)

# Minimal shared-secret protection
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

# AGPL-3.0 section 13: users of a network-hosted copy must be offered its source.
# If you run a modified version, point this at your own repository.
SOURCE_URL = os.getenv("SOURCE_URL", "https://github.com/ewordsworth/tweppy")


@app.context_processor
def _inject_source_url():
    return {"source_url": SOURCE_URL}

X_TWEETS_URL = "https://api.x.com/2/tweets"
TWEET_MAX_CHARS = 280
URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)

IDLE_POLL = 30          # seconds to wait when a queue is empty
RETRY_INTERVAL = 60     # seconds to wait after an unexpected worker error
SCHEDULER_POLL = 30     # how often due scheduled posts are checked
FAILURE_LOG_LIMIT = 200

INTERVAL_CHOICES = [30, 60, 90, 120, 180, 240, 360, 480, 720, 1440]

DEFAULT_SETTINGS = {
    "post_interval_minutes": "120",
    "community_interval_minutes": "120",
    "queue_order": "random",           # "random" or "fifo"
    "skip_duplicates": "1",
    "retweet_community_posts": "1",
    "quiet_hours_enabled": "0",
    "quiet_start": "00:00",
    "quiet_end": "07:00",
    "timezone": "UTC",
    "cost_per_post": "0.015",
    "cost_per_link_post": "0.20",
    # Persisted worker state, so auto-posting resumes after a restart
    "auto_post_enabled": "0",
    "community_auto_post_enabled": "0",
    "community_auto_post_id": "",
}


def require_admin(fn):
    """Minimal shared-secret check.
    Provide the token via header X-Admin-Token, query ?admin_token=, or cookie admin_token.
    If ADMIN_TOKEN is not set, this check is effectively disabled.
    """
    @wraps(fn)
    def _wrapped(*args, **kwargs):
        if not ADMIN_TOKEN:
            return fn(*args, **kwargs)
        token = (
            request.headers.get("X-Admin-Token")
            or request.args.get("admin_token")
            or request.cookies.get("admin_token")
        )
        if token == ADMIN_TOKEN:
            return fn(*args, **kwargs)
        return jsonify({"error": "forbidden"}), 403
    return _wrapped


# ---------------------------------------------------------------------------
# Settings and time helpers
# ---------------------------------------------------------------------------

_timezones = None


def known_timezones():
    global _timezones
    if _timezones is None:
        _timezones = sorted(available_timezones())
    return _timezones


def setting(key):
    value = db.get_setting(key)
    return DEFAULT_SETTINGS.get(key) if value is None else value


def setting_bool(key):
    return setting(key) == "1"


def setting_int(key, minimum, maximum):
    try:
        value = int(setting(key))
    except (TypeError, ValueError):
        value = int(DEFAULT_SETTINGS[key])
    return max(minimum, min(maximum, value))


def setting_float(key):
    try:
        return max(0.0, float(setting(key)))
    except (TypeError, ValueError):
        return float(DEFAULT_SETTINGS[key])


def user_tz():
    try:
        return ZoneInfo(setting("timezone"))
    except Exception:
        return timezone.utc


def interval_minutes(kind):
    key = "post_interval_minutes" if kind == "profile" else "community_interval_minutes"
    return setting_int(key, 5, 7 * 24 * 60)


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat() if dt else None


def humanize_minutes(minutes):
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _parse_hhmm(value):
    hours, minutes = value.split(":")
    hours, minutes = int(hours), int(minutes)
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(value)
    return hours * 60 + minutes


def quiet_until(now_utc):
    """If now falls inside quiet hours, return the UTC time they end, else None."""
    if not setting_bool("quiet_hours_enabled"):
        return None
    try:
        start = _parse_hhmm(setting("quiet_start"))
        end = _parse_hhmm(setting("quiet_end"))
    except ValueError:
        return None
    if start == end:
        return None
    local = now_utc.astimezone(user_tz())
    now_minutes = local.hour * 60 + local.minute
    inside = (start <= now_minutes < end) if start < end else (now_minutes >= start or now_minutes < end)
    if not inside:
        return None
    end_local = local.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if end_local <= local:
        end_local += timedelta(days=1)
    return end_local.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_tweets_from_input(input_text):
    """Parse tweets from input text using square brackets as delimiters.
    Example: [First tweet] [Second tweet]
    """
    tweets = re.findall(r"\[([^\]]+)\]", input_text or "")
    return [tweet.strip() for tweet in tweets if tweet.strip()]


def parse_int_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_schedule_datetime(local_str, utc_str):
    """Return an aware UTC datetime from the schedule form fields, or None if both are empty.
    The browser fills utc_str (ISO 8601 with Z); local_str is a datetime-local fallback.
    Raises ValueError on malformed input.
    """
    if utc_str:
        return datetime.fromisoformat(utc_str.replace("Z", "+00:00")).astimezone(timezone.utc)
    if local_str:
        return datetime.fromisoformat(local_str).replace(tzinfo=user_tz()).astimezone(timezone.utc)
    return None


def has_link(text):
    return bool(URL_RE.search(text or ""))


# ---------------------------------------------------------------------------
# History, stats, failures
# ---------------------------------------------------------------------------

def record_post(text, post_type="profile", count_in_stats=True):
    """Add a post to history and, for profile posts, bump the posted counter."""
    db.execute(
        "INSERT INTO post_history (text, posted_at, type) VALUES (?, ?, ?)",
        (text, db.utcnow_str(), post_type),
    )
    if count_in_stats:
        db.execute(
            "INSERT INTO bot_stats (key, value) VALUES ('posted_count', 1) "
            "ON CONFLICT(key) DO UPDATE SET value = value + 1"
        )


def record_failure(text, error, post_type="profile"):
    """Log a failed post attempt so the dashboard can explain what went wrong."""
    try:
        db.execute(
            "INSERT INTO post_failures (text, error, failed_at, type) VALUES (?, ?, ?, ?)",
            (text, error, db.utcnow_str(), post_type),
        )
        db.execute(
            "DELETE FROM post_failures WHERE id NOT IN "
            f"(SELECT id FROM post_failures ORDER BY id DESC LIMIT {FAILURE_LOG_LIMIT})"
        )
    except Exception as e:
        print(f"[failures] Could not record failure: {e}")


def last_post_time(post_type):
    row = db.query_one("SELECT MAX(posted_at) AS t FROM post_history WHERE type = ?", (post_type,))
    return db.parse_utc(row["t"]) if row and row.get("t") else None


def get_queue_stats():
    unposted = db.query_one("SELECT COUNT(*) AS n FROM tweet_queue WHERE posted = 0")["n"]
    row = db.query_one("SELECT value FROM bot_stats WHERE key = 'posted_count'")
    posted = row["value"] if row else 0
    return {"total": unposted + posted, "unposted": unposted, "posted": posted}


def latest_problem():
    """The newest failure, if it happened after the newest successful post."""
    failure = db.query_one(
        "SELECT text, error, failed_at, type FROM post_failures ORDER BY failed_at DESC, id DESC LIMIT 1"
    )
    if not failure:
        return None
    success = db.query_one("SELECT MAX(posted_at) AS t FROM post_history")
    if success and success.get("t") and success["t"] >= failure["failed_at"]:
        return None
    failure["failed_at"] = db.parse_utc(failure["failed_at"])
    return failure


# ---------------------------------------------------------------------------
# Profile queue
# ---------------------------------------------------------------------------

def drop_duplicates(texts, community_id=None):
    """Remove texts already queued or already posted. Returns (fresh, skipped_count).

    X rejects a post whose text matches one of your recent posts, so queuing an
    exact duplicate usually just produces a failure later.
    """
    if not setting_bool("skip_duplicates"):
        return texts, 0
    unique = list(dict.fromkeys(texts))
    existing = set()
    for start in range(0, len(unique), 40):
        chunk = unique[start:start + 40]
        placeholders = ", ".join("?" * len(chunk))
        if community_id is None:
            rows = db.query(
                f"SELECT text FROM tweet_queue WHERE text IN ({placeholders}) "
                f"UNION SELECT text FROM post_history WHERE text IN ({placeholders})",
                chunk + chunk,
            )
        else:
            rows = db.query(
                f"SELECT text FROM community_tweet_queue WHERE community_id = ? AND text IN ({placeholders}) "
                f"UNION SELECT text FROM post_history WHERE text IN ({placeholders})",
                [community_id] + chunk + chunk,
            )
        existing.update(row["text"] for row in rows)
    fresh = [t for t in unique if t not in existing]
    return fresh, len(texts) - len(fresh)


def add_tweets_to_queue(tweets):
    now = db.utcnow_str()
    return db.insert_many("tweet_queue", ("text", "created_at"), [(t, now) for t in tweets])


def get_next_tweet():
    """Next unposted tweet: random or oldest-first, per Settings."""
    order = "RANDOM()" if setting("queue_order") == "random" else "created_at ASC, id ASC"
    return db.query_one(f"SELECT id, text FROM tweet_queue WHERE posted = 0 ORDER BY {order} LIMIT 1")


def mark_tweet_posted(tweet_id, tweet_text):
    """Move a tweet from the queue to history. tweet_id may be None for ad-hoc posts."""
    record_post(tweet_text, "profile")
    if tweet_id is not None:
        db.execute("DELETE FROM tweet_queue WHERE id = ?", (tweet_id,))


def schedule_rows(items):
    """Insert scheduled posts. items: list of (text, aware_utc_datetime)."""
    now = db.utcnow_str()
    rows = [(text, db.to_db_str(when), now) for text, when in items]
    return db.insert_many("scheduled_posts", ("text", "scheduled_at", "created_at"), rows)


# ---------------------------------------------------------------------------
# Community queue
# ---------------------------------------------------------------------------

def get_community(community_id):
    if not community_id:
        return None
    return db.query_one(
        "SELECT id, name, auto_posting_active FROM communities WHERE id = ?",
        (community_id,),
    )


def add_tweets_to_community_queue(tweets, community_id):
    now = db.utcnow_str()
    rows = [(t, community_id, now) for t in tweets]
    return db.insert_many("community_tweet_queue", ("text", "community_id", "created_at"), rows)


def get_next_community_tweet(community_id):
    """Get the oldest unposted tweet for a specific community."""
    return db.query_one(
        "SELECT id, text, community_id FROM community_tweet_queue "
        "WHERE community_id = ? AND posted = 0 ORDER BY created_at ASC, id ASC LIMIT 1",
        (community_id,),
    )


def mark_community_tweet_posted(tweet_id, tweet_text):
    record_post(tweet_text, "community", count_in_stats=False)
    db.execute("DELETE FROM community_tweet_queue WHERE id = ?", (tweet_id,))


# ---------------------------------------------------------------------------
# X API calls
# ---------------------------------------------------------------------------

def explain_x_error(response):
    """Turn an X API error response into one plain-English sentence."""
    status = response.status_code
    body = response.text or ""
    try:
        data = response.json()
    except ValueError:
        data = {}
    detail = ""
    if isinstance(data, dict):
        detail = data.get("detail") or data.get("title") or ""
        errors = data.get("errors")
        if not detail and isinstance(errors, list) and errors:
            detail = errors[0].get("message", "")
    lowered = body.lower()
    if status == 402:
        return "X API credits are used up. Add credits in the X developer console, then try again."
    if status == 403 and "client-not-enrolled" in lowered:
        return "This X app isn't attached to a Project in the developer portal."
    if status == 403 and "duplicate" in lowered:
        return "X rejected this as a duplicate of one of your recent posts."
    if status == 401:
        return "X rejected the login token. Run authorize.py to authorize the app again."
    if status == 429:
        return "X rate limit reached. The bot will try again later."
    return f"X returned HTTP {status}: {(detail or body)[:200]}"


def _create_post(payload):
    """POST /2/tweets. Returns (ok, message, new_tweet_id)."""
    try:
        token = get_valid_token()
        if not token:
            return False, "No X access token found. Run authorize.py to authorize the app.", None
        response = requests.post(
            X_TWEETS_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
    except Exception as e:
        return False, f"Could not reach X: {e}", None
    if response.status_code == 201:
        return True, "Posted", response.json().get("data", {}).get("id")
    return False, explain_x_error(response), None


def post_to_profile(caption):
    ok, message, _ = _create_post({"text": caption})
    return ok, message


def post_to_community(caption, community_id):
    if not community_id:
        return False, "A community ID is required for community posting.", None
    return _create_post({"text": caption, "community_id": str(community_id)})


def post_tweet(caption):
    """Backward-compatible alias for posting to profile."""
    return post_to_profile(caption)


def retweet(tweet_id):
    """Retweet a tweet by its ID as the connected account."""
    try:
        token_info = get_valid_token(return_full_response=True)
        if not token_info or "access_token" not in token_info:
            return False, "Could not get a valid access token."
        user_id = token_info.get("user_id")
        if not user_id:
            return False, "Could not look up the connected account to retweet."
        response = requests.post(
            f"https://api.x.com/2/users/{user_id}/retweets",
            headers={"Authorization": f"Bearer {token_info['access_token']}",
                     "Content-Type": "application/json"},
            json={"tweet_id": tweet_id},
            timeout=30,
        )
    except Exception as e:
        return False, f"Could not reach X: {e}"
    if response.status_code == 200:
        if response.json().get("data", {}).get("retweeted"):
            return True, "Retweet successful"
        return False, "X did not confirm the retweet."
    if response.status_code == 403 and "already retweeted" in response.text.lower():
        return True, "Already retweeted"
    return False, explain_x_error(response)


def publish_profile_item(item):
    """Post a queued profile tweet; record success or failure. Returns (ok, message)."""
    ok, message = post_tweet(item["text"])
    if ok:
        mark_tweet_posted(item["id"], item["text"])
    else:
        record_failure(item["text"], message, "profile")
    return ok, message


def publish_community_item(item):
    """Post a community-queue row, then retweet it to the profile if enabled.

    Returns (ok, message, retweet_note). retweet_note is None when no retweet was attempted.
    """
    ok, message, new_tweet_id = post_to_community(item["text"], item["community_id"])
    if not (ok and new_tweet_id):
        record_failure(item["text"], message, "community")
        return False, message, None
    mark_community_tweet_posted(item["id"], item["text"])
    if not setting_bool("retweet_community_posts"):
        return True, message, None
    rt_ok, rt_message = retweet(new_tweet_id)
    if not rt_ok:
        record_failure(item["text"], f"Posted to the community, but the retweet failed: {rt_message}", "community")
    return True, message, (rt_ok, rt_message)


# ---------------------------------------------------------------------------
# Background workers
#
# Each worker owns a threading.Event. Stopping sets the event, which ends the
# loop and wakes the worker out of its sleep immediately, so a stopped worker
# can never resume alongside a newly started one. The posting interval is read
# from Settings on every loop, so changes apply without a restart.
# ---------------------------------------------------------------------------

_worker_lock = threading.Lock()
workers = {
    "profile": {"active": False, "stop": threading.Event(), "state": "Stopped",
                "next_at": None, "community_id": None, "community_name": None},
    "community": {"active": False, "stop": threading.Event(), "state": "Stopped",
                  "next_at": None, "community_id": None, "community_name": None},
}


def _set_state(kind, stop, state, next_at=None):
    # Ignore updates from a worker that has already been replaced.
    if workers[kind]["stop"] is stop:
        workers[kind]["state"] = state
        workers[kind]["next_at"] = next_at


def _backoff(failures):
    """1, 2, 4, 8, 16, then 30 minutes between retries."""
    return timedelta(minutes=min(30, 2 ** max(0, failures - 1)))


def _run_worker(kind, stop, fetch_next, publish, label):
    print(f"[{label}] Worker started.")
    post_type = "community" if kind == "community" else "profile"
    try:
        last_post = last_post_time(post_type)
    except Exception as e:
        print(f"[{label}] Could not read last post time: {e}")
        last_post = None
    retry_at = None
    failures = 0

    while not stop.is_set():
        try:
            now = utcnow()
            due = last_post + timedelta(minutes=interval_minutes(kind)) if last_post else now
            retrying = bool(retry_at and retry_at > due)
            if retrying:
                due = retry_at
            if now < due:
                _set_state(kind, stop, "Retrying after an error" if retrying else "Waiting for next post", due)
                stop.wait(min(30, (due - now).total_seconds()))
                continue

            quiet_end = quiet_until(now)
            if quiet_end:
                _set_state(kind, stop, "Paused for quiet hours", quiet_end)
                stop.wait(min(60, (quiet_end - now).total_seconds()))
                continue

            item = fetch_next()
            if not item:
                _set_state(kind, stop, "Queue is empty", None)
                stop.wait(IDLE_POLL)
                continue

            _set_state(kind, stop, "Posting", now)
            ok, message = publish(item)
            if ok:
                last_post = utcnow()
                retry_at, failures = None, 0
                print(f"[{label}] Posted: {item['text']}")
            else:
                failures += 1
                retry_at = utcnow() + _backoff(failures)
                print(f"[{label}] Failed ({failures} in a row): {message}")
        except Exception as e:
            print(f"[{label}] Error in worker: {e}")
            failures += 1
            retry_at = utcnow() + _backoff(failures)
            stop.wait(RETRY_INTERVAL)

    _set_state(kind, stop, "Stopped", None)
    print(f"[{label}] Worker stopped.")


def _community_publish_for_worker(item):
    ok, message, _ = publish_community_item(item)
    return ok, message


def start_worker(kind, community=None):
    """Start a worker if it isn't running. Returns True if it was started."""
    with _worker_lock:
        worker = workers[kind]
        if worker["active"]:
            return False
        stop = threading.Event()
        worker.update(active=True, stop=stop, state="Starting", next_at=None,
                      community_id=community["id"] if community else None,
                      community_name=community["name"] if community else None)
        if kind == "profile":
            args = (kind, stop, get_next_tweet, publish_profile_item, "auto-post")
        else:
            community_id = community["id"]
            args = (kind, stop, lambda: get_next_community_tweet(community_id),
                    _community_publish_for_worker, "community-auto-post")
        threading.Thread(target=_run_worker, args=args, daemon=True).start()
        return True


def stop_worker(kind):
    """Stop a worker if it is running. Returns True if it was stopped."""
    with _worker_lock:
        worker = workers[kind]
        if not worker["active"]:
            return False
        worker["stop"].set()
        worker.update(active=False, state="Stopped", next_at=None)
        return True


# Scheduler: posts scheduled tweets when they come due. A failing item is
# retried with the same backoff as the auto-post workers, tracked in memory.
scheduler_active = False
scheduler_stop = threading.Event()
_scheduler_retry = {}   # scheduled_post id -> (failures, retry_at)


def scheduler_worker(stop):
    print("[scheduler] Worker started.")
    while not stop.is_set():
        try:
            now = utcnow()
            due = db.query(
                "SELECT id, text FROM scheduled_posts WHERE posted = 0 AND scheduled_at <= ? "
                "ORDER BY scheduled_at ASC, id ASC",
                (db.to_db_str(now),),
            )
            for doc in due:
                failures, retry_at = _scheduler_retry.get(doc["id"], (0, None))
                if retry_at and retry_at > now:
                    continue
                success, message = post_tweet(doc["text"])
                if success:
                    db.execute("UPDATE scheduled_posts SET posted = 1 WHERE id = ?", (doc["id"],))
                    record_post(doc["text"], "profile")
                    _scheduler_retry.pop(doc["id"], None)
                    print(f"[scheduler] Posted scheduled tweet: {doc['text']}")
                else:
                    failures += 1
                    _scheduler_retry[doc["id"]] = (failures, utcnow() + _backoff(failures))
                    record_failure(doc["text"], message, "scheduled")
                    print(f"[scheduler] Failed scheduled post: {message}")
        except Exception as e:
            print(f"[scheduler] Error: {e}")
        stop.wait(SCHEDULER_POLL)


def start_scheduler_if_needed():
    """Start the scheduled-post worker once."""
    global scheduler_active
    with _worker_lock:
        if scheduler_active:
            return
        scheduler_active = True
    threading.Thread(target=scheduler_worker, args=(scheduler_stop,), daemon=True).start()


_resumed = False
_resume_lock = threading.Lock()


def resume_workers():
    """Start the scheduler, and restart any auto-posting that was on before a restart."""
    global _resumed
    with _resume_lock:
        if _resumed:
            return
        _resumed = True
    start_scheduler_if_needed()
    try:
        if setting_bool("auto_post_enabled"):
            start_worker("profile")
            print("[startup] Resumed profile auto-posting.")
        if setting_bool("community_auto_post_enabled"):
            community = get_community(setting("community_auto_post_id"))
            if community:
                start_worker("community", community)
                print(f"[startup] Resumed community auto-posting for {community['name']}.")
    except Exception as e:
        print(f"[startup] Could not resume auto-posting: {e}")


@app.before_request
def _ensure_workers_running():
    resume_workers()


# ---------------------------------------------------------------------------
# Page data builders
# ---------------------------------------------------------------------------

def _rows_with_times(rows, *fields):
    for row in rows:
        for field in fields:
            if row.get(field):
                row[field] = db.parse_utc(row[field])
    return rows


def worker_status(kind):
    worker = workers[kind]
    return {
        "active": worker["active"],
        "state": worker["state"],
        "next_at": iso(worker["next_at"]),
        "community_name": worker["community_name"],
        "interval": humanize_minutes(interval_minutes(kind)),
    }


def build_analytics(queue_count):
    tz = user_tz()
    now = utcnow()
    today = now.astimezone(tz).date()
    days = [today - timedelta(days=offset) for offset in range(29, -1, -1)]
    start_utc = datetime.combine(days[0], dtime.min, tzinfo=tz).astimezone(timezone.utc)
    week_start = today - timedelta(days=6)

    posts = db.query(
        "SELECT text, posted_at, type FROM post_history WHERE posted_at >= ?",
        (db.to_db_str(start_utc),),
    )
    failures_30 = db.query_one(
        "SELECT COUNT(*) AS n FROM post_failures WHERE failed_at >= ?",
        (db.to_db_str(start_utc),),
    )["n"]
    all_time = db.query_one(
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(CASE WHEN type = 'community' THEN 1 ELSE 0 END), 0) AS community "
        "FROM post_history"
    )

    per_day = {day: 0 for day in days}
    per_hour = [0] * 24
    profile_30 = community_30 = links_30 = last_7 = today_count = 0
    for post in posts:
        local = db.parse_utc(post["posted_at"]).astimezone(tz)
        day = local.date()
        if day in per_day:
            per_day[day] += 1
        per_hour[local.hour] += 1
        if post["type"] == "community":
            community_30 += 1
        else:
            profile_30 += 1
        if has_link(post["text"]):
            links_30 += 1
        if day >= week_start:
            last_7 += 1
        if day == today:
            today_count += 1

    total_30 = len(posts)
    attempts = total_30 + failures_30
    cost_post = setting_float("cost_per_post")
    cost_link = setting_float("cost_per_link_post")
    retweets = community_30 if setting_bool("retweet_community_posts") else 0
    spend_30 = (total_30 - links_30) * cost_post + links_30 * cost_link + retweets * cost_post

    interval = interval_minutes("profile")
    runway_minutes = queue_count * interval
    per_day_rate = 24 * 60 / interval

    return {
        "timezone": str(tz),
        "today": today_count,
        "last_7": last_7,
        "last_30": total_30,
        "profile_30": profile_30,
        "community_30": community_30,
        "links_30": links_30,
        "failures_30": failures_30,
        "success_rate": round(100 * total_30 / attempts) if attempts else None,
        "all_time": all_time["n"],
        "all_time_community": all_time["community"],
        "spend_30": spend_30,
        "monthly_projection": per_day_rate * 30 * cost_post,
        "runway": humanize_minutes(runway_minutes) if queue_count else None,
        "daily": [{"label": d.strftime("%b %d"), "short": d.strftime("%d"), "value": per_day[d]} for d in days],
        "hourly": [{"label": f"{h:02d}:00", "short": f"{h:02d}", "value": per_hour[h]} for h in range(24)],
    }


def build_calendar_items():
    since = db.to_db_str(utcnow() - timedelta(days=90))
    rows = db.query(
        "SELECT id, text, scheduled_at, posted FROM scheduled_posts "
        "WHERE posted = 0 OR scheduled_at >= ? ORDER BY scheduled_at ASC, id ASC",
        (since,),
    )
    return [{
        "id": row["id"],
        "text": row["text"],
        "at": iso(db.parse_utc(row["scheduled_at"])),
        "posted": bool(row["posted"]),
    } for row in rows]


# ---------------------------------------------------------------------------
# Routes: pages and APIs
# ---------------------------------------------------------------------------

@app.route('/')
@require_admin
def index():
    queue = db.query(
        "SELECT id, text, created_at FROM tweet_queue WHERE posted = 0 ORDER BY created_at ASC, id ASC"
    )
    history = _rows_with_times(db.query(
        "SELECT id, text, posted_at, type FROM post_history ORDER BY posted_at DESC, id DESC LIMIT 50"
    ), "posted_at")
    scheduled = _rows_with_times(db.query(
        "SELECT id, text, scheduled_at FROM scheduled_posts WHERE posted = 0 ORDER BY scheduled_at ASC, id ASC"
    ), "scheduled_at")
    communities = db.query(
        "SELECT c.id, c.name, "
        "(SELECT COUNT(*) FROM community_tweet_queue q WHERE q.community_id = c.id AND q.posted = 0) AS queued "
        "FROM communities c ORDER BY c.created_at ASC, c.id ASC"
    )
    failures = _rows_with_times(db.query(
        "SELECT text, error, failed_at, type FROM post_failures ORDER BY failed_at DESC, id DESC LIMIT 15"
    ), "failed_at")

    active_community = get_community(session.get("active_community_id"))
    community_queue = []
    if active_community:
        community_queue = db.query(
            "SELECT id, text, created_at FROM community_tweet_queue "
            "WHERE community_id = ? AND posted = 0 ORDER BY created_at ASC, id ASC",
            (active_community["id"],),
        )
    community_post_history = _rows_with_times(db.query(
        "SELECT id, text, posted_at FROM post_history WHERE type = 'community' "
        "ORDER BY posted_at DESC, id DESC LIMIT 50"
    ), "posted_at")

    stats = get_queue_stats()
    analytics = build_analytics(len(queue))
    tokens = rest.get_tokens()

    settings_view = {key: setting(key) for key in DEFAULT_SETTINGS}
    page_data = {
        "daily": analytics["daily"],
        "hourly": analytics["hourly"],
        "calendar": build_calendar_items(),
        "settingsTimezone": settings_view["timezone"],
        "tweetMax": TWEET_MAX_CHARS,
        "costPerPost": setting_float("cost_per_post"),
        "costPerLinkPost": setting_float("cost_per_link_post"),
    }

    resp = make_response(render_template(
        'index.html',
        queue=queue,
        history=history,
        stats=stats,
        scheduled=scheduled,
        scheduled_count=len(scheduled),
        next_scheduled=scheduled[0] if scheduled else None,
        db_connected=db.connected,
        db_backend=db.backend_name,
        posting_active=workers["profile"]["active"],
        community_posting_active=workers["community"]["active"],
        profile_worker=worker_status("profile"),
        community_worker=worker_status("community"),
        communities=communities,
        active_community=active_community,
        community_queue=community_queue,
        community_post_history=community_post_history,
        failures=failures,
        problem=latest_problem(),
        analytics=analytics,
        settings=settings_view,
        interval_choices=[(m, humanize_minutes(m)) for m in INTERVAL_CHOICES],
        timezones=known_timezones(),
        account=rest.get_account_info(),
        token_updated_at=tokens["updated_at"] if tokens else None,
        admin_protected=bool(ADMIN_TOKEN),
        page_data=page_data,
    ))

    # Persist admin token in a cookie if provided in the query string
    token = request.args.get('admin_token')
    if token and ADMIN_TOKEN and token == ADMIN_TOKEN:
        resp.set_cookie('admin_token', token, expires=datetime.now() + timedelta(days=30),
                        httponly=True, samesite='Lax')
    return resp


@app.route('/keepalive')
def keepalive():
    """An endpoint to keep the server alive on free hosting services."""
    return jsonify({"status": "alive", "timestamp": utcnow().isoformat()})


@app.route('/api/stats')
def api_stats():
    return jsonify(get_queue_stats())


@app.route('/api/status')
@require_admin
def api_status():
    return jsonify({
        "stats": get_queue_stats(),
        "profile": worker_status("profile"),
        "community": worker_status("community"),
    })


# ---- Profile queue routes --------------------------------------------------

@app.route('/add_tweets', methods=['POST'])
@require_admin
def add_tweets():
    action = request.form.get('action')
    tweet_texts = parse_tweets_from_input(request.form.get('tweets_input', ''))
    if not tweet_texts:
        flash('No valid tweets found. Please enclose each tweet in [square brackets].', 'error')
        return redirect(url_for('index'))

    if action == 'post':
        # Post the first tweet immediately; queue the rest
        first = tweet_texts.pop(0)
        success, message = post_tweet(first)
        if success:
            mark_tweet_posted(None, first)
            flash('Tweet posted successfully!', 'success')
        else:
            record_failure(first, message, "profile")
            flash(f'Could not post: {message} The tweet was added to the queue instead.', 'error')
            tweet_texts.insert(0, first)

    if tweet_texts:
        fresh, skipped = drop_duplicates(tweet_texts)
        added = add_tweets_to_queue(fresh)
        note = f' Skipped {skipped} duplicate(s).' if skipped else ''
        flash(f'{added} tweet(s) added to the queue.{note}', 'success' if added else 'info')

    return redirect(url_for('index'))


@app.route('/post_next', methods=['POST'])
@require_admin
def post_next():
    """Manually post the next tweet in the queue."""
    next_tweet = get_next_tweet()
    if not next_tweet:
        flash('No tweets in queue to post!', 'error')
        return redirect(url_for('index'))
    ok, message = publish_profile_item(next_tweet)
    if ok:
        flash(f'Posted: "{next_tweet["text"]}"', 'success')
    else:
        flash(f'Could not post: {message}', 'error')
    return redirect(url_for('index'))


@app.route('/post_tweet/<tweet_id>', methods=['POST'])
@require_admin
def post_specific_tweet(tweet_id):
    tid = parse_int_id(tweet_id)
    tweet = db.query_one("SELECT id, text FROM tweet_queue WHERE id = ?", (tid,)) if tid is not None else None
    if not tweet:
        flash('Tweet not found in queue.', 'error')
        return redirect(url_for('index'))
    ok, message = publish_profile_item(tweet)
    if ok:
        flash('Tweet posted successfully!', 'success')
    else:
        flash(f'Could not post: {message}', 'error')
    return redirect(url_for('index'))


@app.route('/delete_tweet/<tweet_id>', methods=['POST'])
@require_admin
def delete_tweet(tweet_id):
    tid = parse_int_id(tweet_id)
    deleted = db.query("DELETE FROM tweet_queue WHERE id = ? RETURNING id", (tid,)) if tid is not None else []
    if deleted:
        flash('Tweet successfully deleted.', 'success')
    else:
        flash('Tweet not found or already deleted.', 'error')
    return redirect(url_for('index'))


@app.route('/update_tweet/<tweet_id>', methods=['POST'])
@require_admin
def update_tweet(tweet_id):
    new_text = request.form.get('new_text', '').strip()
    if not new_text:
        flash('Tweet text cannot be empty.', 'error')
        return redirect(url_for('index'))
    tid = parse_int_id(tweet_id)
    updated = []
    if tid is not None:
        updated = db.query("UPDATE tweet_queue SET text = ? WHERE id = ? RETURNING id", (new_text, tid))
    if updated:
        flash('Tweet updated successfully.', 'success')
    else:
        flash('Tweet not found.', 'error')
    return redirect(url_for('index'))


@app.route('/clear_queue', methods=['POST'])
@require_admin
def clear_queue():
    count = db.query_one("SELECT COUNT(*) AS n FROM tweet_queue")["n"]
    db.execute("DELETE FROM tweet_queue")
    flash(f'Cleared {count} tweets from queue', 'info')
    return redirect(url_for('index'))


@app.route('/clear_history', methods=['POST'])
@require_admin
def clear_history():
    count = db.query_one("SELECT COUNT(*) AS n FROM post_history")["n"]
    db.execute("DELETE FROM post_history")
    flash(f'Cleared {count} tweets from history.', 'info')
    return redirect(url_for('index'))


@app.route('/clear_stats', methods=['POST'])
@require_admin
def clear_stats():
    db.execute(
        "INSERT INTO bot_stats (key, value) VALUES ('posted_count', 0) "
        "ON CONFLICT(key) DO UPDATE SET value = 0"
    )
    flash('Statistics have been reset.', 'info')
    return redirect(url_for('index'))


@app.route('/clear_failures', methods=['POST'])
@require_admin
def clear_failures():
    count = db.query_one("SELECT COUNT(*) AS n FROM post_failures")["n"]
    db.execute("DELETE FROM post_failures")
    flash(f'Cleared {count} logged error(s).', 'info')
    return redirect(url_for('index'))


@app.route('/start_auto_posting', methods=['POST'])
@require_admin
def start_auto_posting():
    db.set_setting("auto_post_enabled", "1")
    if start_worker("profile"):
        flash(f'Auto-posting started. Posting every {humanize_minutes(interval_minutes("profile"))}.', 'success')
    else:
        flash('Auto-posting is already running!', 'info')
    return redirect(url_for('index'))


@app.route('/stop_auto_posting', methods=['POST'])
@require_admin
def stop_auto_posting():
    db.set_setting("auto_post_enabled", "0")
    stop_worker("profile")
    flash('Auto-posting stopped.', 'info')
    return redirect(url_for('index'))


# ---- Scheduled posts --------------------------------------------------------

@app.route('/schedule', methods=['POST'])
@require_admin
def schedule():
    """Schedule a single tweet at a specified datetime."""
    scheduled_text = request.form.get('scheduled_text', '').strip()
    scheduled_at_str = request.form.get('scheduled_at', '').strip()
    scheduled_at_utc = request.form.get('scheduled_at_utc', '').strip()

    if not scheduled_text:
        flash('Please enter the tweet text to schedule.', 'error')
        return redirect(url_for('index'))
    if not scheduled_at_str and not scheduled_at_utc:
        flash('Please select a schedule date/time.', 'error')
        return redirect(url_for('index'))
    try:
        scheduled_dt = parse_schedule_datetime(scheduled_at_str, scheduled_at_utc)
    except ValueError:
        flash('Invalid date/time format.', 'error')
        return redirect(url_for('index'))

    schedule_rows([(scheduled_text, scheduled_dt)])
    flash('Scheduled 1 tweet.', 'success')
    start_scheduler_if_needed()
    return redirect(url_for('index'))


@app.route('/schedule_bulk', methods=['POST'])
@require_admin
def schedule_bulk():
    """Schedule several bracketed tweets, spaced out from a start time."""
    texts = parse_tweets_from_input(request.form.get('bulk_input', ''))
    if not texts:
        flash('No valid tweets found. Please enclose each tweet in [square brackets].', 'error')
        return redirect(url_for('index'))
    try:
        start = parse_schedule_datetime(request.form.get('start_at', '').strip(),
                                        request.form.get('start_at_utc', '').strip())
        every = int(request.form.get('every_minutes', '120'))
    except ValueError:
        flash('Invalid start time or spacing.', 'error')
        return redirect(url_for('index'))
    if start is None:
        flash('Please pick a start time.', 'error')
        return redirect(url_for('index'))
    if start < utcnow() - timedelta(minutes=1):
        flash('The start time is in the past.', 'error')
        return redirect(url_for('index'))
    every = max(5, min(7 * 24 * 60, every))

    items = [(text, start + timedelta(minutes=every * i)) for i, text in enumerate(texts)]
    count = schedule_rows(items)
    flash(f'Scheduled {count} tweet(s), one every {humanize_minutes(every)}.', 'success')
    start_scheduler_if_needed()
    return redirect(url_for('index'))


@app.route('/scheduled/delete/<sid>', methods=['POST'])
@require_admin
def delete_scheduled(sid):
    """Cancel a scheduled tweet (delete it)."""
    item_id = parse_int_id(sid)
    deleted = []
    if item_id is not None:
        deleted = db.query("DELETE FROM scheduled_posts WHERE id = ? RETURNING id", (item_id,))
    if deleted:
        _scheduler_retry.pop(item_id, None)
        flash('Scheduled tweet canceled.', 'info')
    else:
        flash('Scheduled tweet not found.', 'error')
    return redirect(url_for('index'))


@app.route('/scheduled/update/<sid>', methods=['POST'])
@require_admin
def update_scheduled(sid):
    """Edit the text and/or scheduled time of a scheduled tweet."""
    new_text = request.form.get('scheduled_text', '').strip()
    new_at_str = request.form.get('scheduled_at', '').strip()
    new_at_utc = request.form.get('scheduled_at_utc', '').strip()

    if not new_text:
        flash('Tweet text cannot be empty.', 'error')
        return redirect(url_for('index'))
    try:
        new_dt = parse_schedule_datetime(new_at_str, new_at_utc)
    except ValueError:
        flash('Invalid date/time.', 'error')
        return redirect(url_for('index'))

    item_id = parse_int_id(sid)
    if item_id is None:
        flash('Scheduled tweet not found.', 'error')
        return redirect(url_for('index'))

    if new_dt is not None:
        updated = db.query(
            "UPDATE scheduled_posts SET text = ?, scheduled_at = ? WHERE id = ? AND posted = 0 RETURNING id",
            (new_text, db.to_db_str(new_dt), item_id),
        )
    else:
        updated = db.query(
            "UPDATE scheduled_posts SET text = ? WHERE id = ? AND posted = 0 RETURNING id", (new_text, item_id)
        )

    if updated:
        _scheduler_retry.pop(item_id, None)
        flash('Scheduled tweet updated.', 'success')
    else:
        flash('Scheduled tweet not found.', 'error')
    return redirect(url_for('index'))


# ---- Settings, account, export ---------------------------------------------

@app.route('/settings', methods=['POST'])
@require_admin
def save_settings():
    form = request.form
    problems = []

    def save_interval(key):
        try:
            minutes = int(form.get(key, ''))
            if not 5 <= minutes <= 7 * 24 * 60:
                raise ValueError
            db.set_setting(key, minutes)
        except ValueError:
            problems.append('posting interval')

    save_interval("post_interval_minutes")
    save_interval("community_interval_minutes")

    if form.get("queue_order") in ("random", "fifo"):
        db.set_setting("queue_order", form["queue_order"])
    for key in ("skip_duplicates", "retweet_community_posts", "quiet_hours_enabled"):
        db.set_setting(key, "1" if form.get(key) == "1" else "0")

    for key in ("quiet_start", "quiet_end"):
        value = form.get(key, '').strip()
        try:
            _parse_hhmm(value)
            db.set_setting(key, value)
        except ValueError:
            problems.append('quiet hours time')

    tz_name = form.get("timezone", '').strip()
    if tz_name in known_timezones():
        db.set_setting("timezone", tz_name)
    else:
        problems.append('timezone')

    for key in ("cost_per_post", "cost_per_link_post"):
        try:
            value = float(form.get(key, ''))
            if not 0 <= value <= 10:
                raise ValueError
            db.set_setting(key, f"{value:g}")
        except ValueError:
            problems.append('cost')

    if problems:
        flash(f'Saved, except these invalid values: {", ".join(sorted(set(problems)))}.', 'error')
    else:
        flash('Settings saved.', 'success')
    return redirect(url_for('index'))


@app.route('/account/refresh', methods=['POST'])
@require_admin
def refresh_account():
    try:
        account = rest.refresh_account_info()
        flash(f'Connected X account: @{account.get("username")}', 'success')
    except Exception as e:
        flash(f'Could not check the X account: {e}', 'error')
    return redirect(url_for('index'))


def _csv_safe(value):
    """Stop spreadsheet apps from treating a tweet as a formula."""
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


@app.route('/export/history.csv')
@require_admin
def export_history():
    rows = db.query("SELECT posted_at, type, text FROM post_history ORDER BY posted_at DESC, id DESC")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["posted_at_utc", "type", "text"])
    for row in rows:
        writer.writerow([row["posted_at"], row["type"], _csv_safe(row["text"])])
    return Response(buffer.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=tweppy-history.csv"})


@app.route('/export/queue.txt')
@require_admin
def export_queue():
    rows = db.query("SELECT text FROM tweet_queue WHERE posted = 0 ORDER BY created_at ASC, id ASC")
    body = "\n".join(f"[{row['text']}]" for row in rows) + ("\n" if rows else "")
    return Response(body, mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=tweppy-queue.txt"})


# ---- Communities ------------------------------------------------------------

@app.route('/set_active_community', methods=['POST'])
@require_admin
def set_active_community():
    """Add or update a community and set it as active in the session."""
    community_id = request.form.get('community_id', '').strip()
    provided_name = request.form.get('community_name', '').strip()
    if not community_id or not community_id.isdigit():
        flash('Please provide a valid numerical Community ID.', 'error')
        return redirect(url_for('index'))

    existing = get_community(community_id)
    if existing:
        community_name = provided_name or existing['name']
        if provided_name and provided_name != existing['name']:
            db.execute("UPDATE communities SET name = ? WHERE id = ?", (provided_name, community_id))
    else:
        community_name = provided_name or f"Community {community_id}"
        db.execute(
            "INSERT INTO communities (id, name, auto_posting_active, created_at) VALUES (?, ?, 0, ?)",
            (community_id, community_name, db.utcnow_str()),
        )

    session['active_community_id'] = community_id
    flash(f'Active community set to {community_name}', 'success')
    return redirect(url_for('index'))


@app.route('/delete_community/<community_id>', methods=['POST'])
@require_admin
def delete_community(community_id):
    """Remove a saved community and its queued tweets."""
    community = get_community(community_id)
    if not community:
        flash('Community not found.', 'error')
        return redirect(url_for('index'))
    if workers["community"]["active"] and workers["community"]["community_id"] == community_id:
        stop_worker("community")
        db.set_setting("community_auto_post_enabled", "0")
    if setting("community_auto_post_id") == community_id:
        db.set_setting("community_auto_post_id", "")
    removed = db.query_one(
        "SELECT COUNT(*) AS n FROM community_tweet_queue WHERE community_id = ?", (community_id,)
    )["n"]
    db.execute("DELETE FROM community_tweet_queue WHERE community_id = ?", (community_id,))
    db.execute("DELETE FROM communities WHERE id = ?", (community_id,))
    if session.get('active_community_id') == community_id:
        session.pop('active_community_id', None)
    flash(f'Removed {community["name"]} and {removed} queued tweet(s).', 'info')
    return redirect(url_for('index'))


@app.route('/add_community_tweets', methods=['POST'])
@require_admin
def add_community_tweets():
    """Add tweets to the active community's queue."""
    tweet_texts = parse_tweets_from_input(request.form.get('community_tweets_input', ''))
    if not tweet_texts:
        flash('No valid tweets found. Please enclose each tweet in [square brackets].', 'error')
        return redirect(url_for('index'))

    active_community = get_community(session.get('active_community_id'))
    if not active_community:
        flash('No active community set. Please set one before adding tweets.', 'error')
        return redirect(url_for('index'))

    fresh, skipped = drop_duplicates(tweet_texts, community_id=active_community['id'])
    added = add_tweets_to_community_queue(fresh, active_community['id'])
    note = f' Skipped {skipped} duplicate(s).' if skipped else ''
    flash(f'{added} tweet(s) added to the community queue.{note}', 'success' if added else 'info')
    return redirect(url_for('index'))


def _flash_community_result(ok, message, retweet_note):
    if not ok:
        flash(f'Could not post to the community: {message}', 'error')
        return
    flash('Community tweet posted successfully!', 'success')
    if retweet_note is not None:
        rt_ok, rt_message = retweet_note
        if rt_ok:
            flash('Post was retweeted to your profile.', 'info')
        else:
            flash(f'Could not retweet to profile: {rt_message}', 'error')


@app.route('/post_next_community', methods=['POST'])
@require_admin
def post_next_community():
    """Manually post the next tweet in the active community queue."""
    active_community = get_community(session.get('active_community_id'))
    if not active_community:
        flash('No active community set. Please set one before posting.', 'error')
        return redirect(url_for('index'))
    next_tweet = get_next_community_tweet(active_community['id'])
    if not next_tweet:
        flash('No tweets in the community queue to post!', 'error')
        return redirect(url_for('index'))
    _flash_community_result(*publish_community_item(next_tweet))
    return redirect(url_for('index'))


@app.route('/post_community_tweet/<tweet_id>', methods=['POST'])
@require_admin
def post_specific_community_tweet(tweet_id):
    """Post a specific tweet from the community queue by ID."""
    tid = parse_int_id(tweet_id)
    tweet = None
    if tid is not None:
        tweet = db.query_one(
            "SELECT id, text, community_id FROM community_tweet_queue WHERE id = ?", (tid,)
        )
    if not tweet:
        flash('Community tweet not found in queue.', 'error')
        return redirect(url_for('index'))
    _flash_community_result(*publish_community_item(tweet))
    return redirect(url_for('index'))


@app.route('/delete_community_tweet/<tweet_id>', methods=['POST'])
@require_admin
def delete_community_tweet(tweet_id):
    tid = parse_int_id(tweet_id)
    deleted = []
    if tid is not None:
        deleted = db.query("DELETE FROM community_tweet_queue WHERE id = ? RETURNING id", (tid,))
    if deleted:
        flash('Community tweet successfully deleted.', 'success')
    else:
        flash('Community tweet not found or already deleted.', 'error')
    return redirect(url_for('index'))


@app.route('/update_community_tweet/<tweet_id>', methods=['POST'])
@require_admin
def update_community_tweet(tweet_id):
    new_text = request.form.get('new_text', '').strip()
    if not new_text:
        flash('Tweet text cannot be empty.', 'error')
        return redirect(url_for('index'))
    tid = parse_int_id(tweet_id)
    updated = []
    if tid is not None:
        updated = db.query(
            "UPDATE community_tweet_queue SET text = ? WHERE id = ? RETURNING id", (new_text, tid)
        )
    if updated:
        flash('Community tweet updated successfully.', 'success')
    else:
        flash('Community tweet not found.', 'error')
    return redirect(url_for('index'))


@app.route('/clear_community_queue', methods=['POST'])
@require_admin
def clear_community_queue():
    active_community = get_community(session.get('active_community_id'))
    if not active_community:
        flash('No active community set. Please set one before clearing queue.', 'error')
        return redirect(url_for('index'))
    count = db.query_one(
        "SELECT COUNT(*) AS n FROM community_tweet_queue WHERE community_id = ?",
        (active_community['id'],),
    )["n"]
    db.execute("DELETE FROM community_tweet_queue WHERE community_id = ?", (active_community['id'],))
    flash(f'Cleared {count} tweets from community queue', 'info')
    return redirect(url_for('index'))


@app.route('/start_community_auto_post', methods=['POST'])
@require_admin
def start_community_auto_post():
    if workers["community"]["active"]:
        flash('Community auto-posting is already running.', 'info')
        return redirect(url_for('index'))
    active_community = get_community(session.get('active_community_id'))
    if not active_community:
        flash('Set an active community before starting auto-posting.', 'error')
        return redirect(url_for('index'))
    db.set_setting("community_auto_post_enabled", "1")
    db.set_setting("community_auto_post_id", active_community["id"])
    start_worker("community", active_community)
    flash(f'Community auto-posting started for {active_community["name"]}.', 'success')
    return redirect(url_for('index'))


@app.route('/pause_community_auto_post', methods=['POST'])
@require_admin
def pause_community_auto_post():
    db.set_setting("community_auto_post_enabled", "0")
    if stop_worker("community"):
        flash('Community auto-posting paused.', 'success')
    else:
        flash('Community auto-posting is not running.', 'info')
    return redirect(url_for('index'))


@app.route('/stop_community_auto_posting', methods=['POST'])
@require_admin
def stop_community_auto_posting():
    """Alias for pausing community auto-posting to match the frontend route."""
    return pause_community_auto_post()


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
