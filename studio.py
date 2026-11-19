"""
Create and Engage features for Tweppy.

Create: AI-drafted posts, generated or uploaded images and videos, and a
composer that queues, schedules, or posts text with one attached file.

Engage: a watchlist of X accounts. Tweppy reads their new posts (and posts that
mention you), drafts a reply for each, and shows them in one list. What X's API
allows decides what each button does:

  * Likes and quote posts are not available to self-serve API apps, and a reply
    through the API is only accepted when the other person mentioned or quoted
    you. So Like, Reply and Quote open X itself with the text filled in, and you
    confirm there with one tap.
  * A reply to a post that mentions you can go through the API.

This module is loaded at the end of webapp.py and reaches the app's helpers
through `core`.
"""
import threading
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, flash, jsonify, redirect, request, url_for

import ai
import db
import media as media_store
import rest

bp = Blueprint("studio", __name__)
core = None     # the webapp module, set by init()

ENGAGE_INTERVALS = [15, 30, 60, 120, 240, 720]
ENGAGE_LIST_LIMIT = 60
DRAFTS_PER_CHECK = 15
USERNAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def init(module):
    global core
    core = module
    module.app.register_blueprint(bp)


def admin(fn):
    """Apply the app's admin check. Resolved per request because core is set late."""
    from functools import wraps

    @wraps(fn)
    def wrapped(*args, **kwargs):
        return core.require_admin(fn)(*args, **kwargs)
    return wrapped


def _home(tab):
    return redirect(url_for("index") + "#" + tab)


def _ai_setting(key):
    return core.setting(key) or None


# ---------------------------------------------------------------------------
# Media library
# ---------------------------------------------------------------------------

def _media_view(item):
    return {
        "id": item["id"],
        "kind": item["kind"],
        "status": item["status"],
        "error": item["error"],
        "provider": item["provider"],
        "prompt": item["prompt"],
        "url": url_for("studio.media_file", media_id=item["id"]) if item["status"] == "ready" else None,
    }


@bp.route("/media/<int:media_id>")
@admin
def media_file(media_id):
    item = media_store.load(media_id)
    if not item:
        return jsonify({"error": "not found"}), 404
    return Response(item["data"], mimetype=item["mime"],
                    headers={"Cache-Control": "private, max-age=86400"})


@bp.route("/api/media")
@admin
def media_list():
    return jsonify({"media": [_media_view(item) for item in media_store.recent()]})


@bp.route("/media/upload", methods=["POST"])
@admin
def media_upload():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "Choose a file first."}), 400
    try:
        media_id = media_store.add(upload.read(), upload.mimetype or "", "upload", upload.filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"media": _media_view(media_store.get(media_id))})


@bp.route("/media/<int:media_id>/delete", methods=["POST"])
@admin
def media_delete(media_id):
    media_store.delete(media_id)
    return jsonify({"ok": True})


def _generate_in_background(media_id, make):
    def run():
        try:
            data, mime = make()
            media_store.store(media_id, data, mime)
        except Exception as e:
            print(f"[studio] Generation {media_id} failed: {e}")
            media_store.fail(media_id, e)
    threading.Thread(target=run, daemon=True).start()


@bp.route("/ai/image", methods=["POST"])
@admin
def ai_image():
    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    provider = body.get("provider") or "openai"
    aspect = body.get("aspect") if body.get("aspect") in ai.ASPECTS else "square"
    if not prompt:
        return jsonify({"error": "Describe the image first."}), 400
    if provider == "imagineart":
        if not ai.imagineart_ready():
            return jsonify({"error": "IMAGINEART_API_KEY is not set."}), 400
        style = _ai_setting("imagineart_image_style")
        make = lambda: ai.imagineart_image(prompt, aspect, style)
    else:
        provider = "openai"
        if not ai.openai_ready():
            return jsonify({"error": "OPENAI_API_KEY is not set."}), 400
        model = _ai_setting("ai_image_model")
        make = lambda: ai.openai_image(prompt, aspect, model)
    media_id = media_store.create_pending("image", provider, prompt)
    _generate_in_background(media_id, make)
    return jsonify({"media": _media_view(media_store.get(media_id))})


@bp.route("/ai/video", methods=["POST"])
@admin
def ai_video():
    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    aspect = body.get("aspect") if body.get("aspect") in ai.ASPECTS else "landscape"
    if not prompt:
        return jsonify({"error": "Describe the video first."}), 400
    if not ai.imagineart_ready():
        return jsonify({"error": "IMAGINEART_API_KEY is not set. Video needs an ImagineArt API key."}), 400
    source = None
    if body.get("image_id"):
        item = media_store.load(core.parse_int_id(body["image_id"]))
        if not item or item["kind"] != "image":
            return jsonify({"error": "The starting image is missing."}), 400
        source = (item["data"], item["mime"])
    style = _ai_setting("imagineart_video_style")
    media_id = media_store.create_pending("video", "imagineart", prompt)
    _generate_in_background(media_id, lambda: ai.imagineart_video(prompt, style, source, aspect))
    return jsonify({"media": _media_view(media_store.get(media_id))})


# ---------------------------------------------------------------------------
# AI writing
# ---------------------------------------------------------------------------

@bp.route("/ai/compose", methods=["POST"])
@admin
def ai_compose():
    body = request.get_json(silent=True) or {}
    brief = (body.get("brief") or "").strip()
    if not brief:
        return jsonify({"error": "Say what the posts should be about."}), 400
    try:
        count = int(body.get("count") or 3)
        posts = ai.compose_posts(brief, count, core.setting("ai_voice"), _ai_setting("ai_text_model"))
    except (ai.AIError, ValueError) as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"posts": posts})


@bp.route("/ai/models")
@admin
def ai_models():
    """Lists the models the OpenAI key can use. Doubles as a key check."""
    try:
        return jsonify({"models": ai.list_openai_models()})
    except ai.AIError as e:
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# Composer: one post, optionally with a file, queued / scheduled / posted now
# ---------------------------------------------------------------------------

@bp.route("/studio/post", methods=["POST"])
@admin
def studio_post():
    form = request.form
    text = form.get("text", "").strip()
    action = form.get("action", "queue")
    media_id = core.parse_int_id(form.get("media_id"))
    if not text:
        flash("Write the post first.", "error")
        return _home("create")
    if len(text) > core.TWEET_MAX_CHARS:
        flash(f"That post is {len(text)} characters. The limit is {core.TWEET_MAX_CHARS}.", "error")
        return _home("create")
    if media_id is not None:
        item = media_store.get(media_id)
        if not item or item["status"] != "ready":
            flash("The attached file is not ready or was deleted.", "error")
            return _home("create")

    if action == "post":
        ok, message = core.post_tweet(text, media_id)
        if ok:
            core.mark_tweet_posted(None, text)
            flash("Posted.", "success")
        else:
            core.record_failure(text, message, "profile")
            core.add_tweets_to_queue([text], media_id)
            flash(f"Could not post: {message} It was added to the queue instead.", "error")
    elif action == "schedule":
        try:
            when = core.parse_schedule_datetime(form.get("scheduled_at", "").strip(),
                                                form.get("scheduled_at_utc", "").strip())
        except ValueError:
            when = None
        if when is None:
            flash("Pick a date and time to schedule the post.", "error")
            return _home("create")
        core.schedule_rows([(text, when)], media_id)
        core.start_scheduler_if_needed()
        flash("Scheduled 1 post.", "success")
    else:
        core.add_tweets_to_queue([text], media_id)
        flash("Added to the queue.", "success")
    return _home("create")


# ---------------------------------------------------------------------------
# Engage: watchlist, feed, drafts
# ---------------------------------------------------------------------------

_check_lock = threading.Lock()
_last_errors = {}       # account key -> problems from that account's last check


def _acct():
    """Key of the current X account. The Engage tables are kept per account."""
    return core.acct() or ""


def _acct_setting(name):
    return db.get_setting(f"{name}:{_acct()}")


def _count_reads(n):
    if n > 0:
        db.execute(
            "INSERT INTO bot_stats (key, value) VALUES ('reads_count', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
            (n,),
        )


def _x_time(value):
    """X timestamps look like 2026-11-19T10:00:00.000Z."""
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    return db.to_db_str(parsed)


def _save_items(kind, tweets, authors):
    """Insert posts that are not stored yet. authors: user_id -> username. Returns new rows."""
    fresh = []
    now = db.utcnow_str()
    for tweet in tweets:
        tweet_id = str(tweet.get("id") or "")
        author_id = str(tweet.get("author_id") or "")
        if not tweet_id or db.query_one("SELECT 1 AS x FROM engage_posts WHERE account_id = ? AND tweet_id = ?",
                                           (_acct(), tweet_id)):
            continue
        row = {
            "tweet_id": tweet_id, "kind": kind, "author_id": author_id,
            "author_username": authors.get(author_id, ""), "text": tweet.get("text", ""),
            "posted_at": _x_time(tweet.get("created_at")),
        }
        db.execute(
            "INSERT INTO engage_posts (account_id, tweet_id, kind, author_id, author_username, text, posted_at, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_acct(), row["tweet_id"], kind, author_id, row["author_username"], row["text"], row["posted_at"], now),
        )
        fresh.append(row)
    return fresh


def _draft(item):
    reply = ai.draft_reply(item["author_username"], item["text"],
                           core.setting("ai_voice"), _ai_setting("ai_text_model"))
    db.execute("UPDATE engage_posts SET draft = ? WHERE account_id = ? AND tweet_id = ?",
               (reply, _acct(), item["tweet_id"]))
    return reply


def check_now():
    """Read new posts from the current account's watchlist, and its mentions. Returns (new_count, error)."""
    if not _check_lock.acquire(blocking=False):
        return 0, "A check is already running."
    try:
        fresh, errors = [], []
        watched = db.query("SELECT user_id, username, since_id FROM watched_accounts WHERE account_id = ? "
                           "ORDER BY added_at ASC", (_acct(),))
        watched_ids = {row["user_id"] for row in watched}

        for account in watched:
            params = {"max_results": 5, "exclude": "retweets,replies", "tweet.fields": "created_at,author_id"}
            if account["since_id"]:
                params["since_id"] = account["since_id"]
            data, error = core.x_get(f"/users/{account['user_id']}/tweets", params)
            if error:
                errors.append(f"@{account['username']}: {error}")
                continue
            tweets = data.get("data") or []
            _count_reads(len(tweets))
            for tweet in tweets:
                tweet.setdefault("author_id", account["user_id"])
            fresh += _save_items("post", tweets, {account["user_id"]: account["username"]})
            newest = (data.get("meta") or {}).get("newest_id")
            if newest:
                db.execute("UPDATE watched_accounts SET since_id = ? WHERE account_id = ? AND user_id = ?",
                           (newest, _acct(), account["user_id"]))

        if core.setting_bool("engage_mentions"):
            me = rest.get_account_info()["id"]
            if not me:
                try:
                    me = rest.refresh_account_info().get("id")
                except Exception as e:
                    errors.append(f"Mentions: {e}")
            if me:
                params = {"max_results": 10, "expansions": "author_id",
                          "user.fields": "username", "tweet.fields": "created_at,author_id"}
                since = _acct_setting("engage_mentions_since")
                if since:
                    params["since_id"] = since
                data, error = core.x_get(f"/users/{me}/mentions", params)
                if error:
                    errors.append(f"Mentions: {error}")
                else:
                    tweets = data.get("data") or []
                    _count_reads(len(tweets))
                    users = (data.get("includes") or {}).get("users") or []
                    authors = {str(u.get("id")): u.get("username", "") for u in users}
                    fresh += _save_items("mention", [t for t in tweets if str(t.get("author_id")) != str(me)], authors)
                    newest = (data.get("meta") or {}).get("newest_id")
                    if newest:
                        db.set_setting(f"engage_mentions_since:{_acct()}", newest)

        if ai.openai_ready() and core.setting_bool("engage_auto_draft"):
            for item in fresh[:DRAFTS_PER_CHECK]:
                try:
                    item["draft"] = _draft(item)
                except ai.AIError as e:
                    errors.append(f"Drafting: {e}")
                    break

        # A mention is the one case where X accepts a reply through the API.
        if core.setting_bool("engage_auto_reply_mentions"):
            for item in fresh:
                if item["kind"] == "mention" and item["author_id"] in watched_ids and item.get("draft"):
                    ok, message = _send_reply(item, item["draft"])
                    if not ok:
                        errors.append(f"Reply to @{item['author_username']}: {message}")

        db.set_setting(f"engage_last_check:{_acct()}", db.utcnow_str())
        _last_errors[_acct()] = "; ".join(errors)[:500] if errors else None
        return len(fresh), _last_errors[_acct()]
    finally:
        _check_lock.release()


def _send_reply(item, text):
    ok, message = core.post_to_profile(text, reply_to=item["tweet_id"])
    if ok:
        core.record_post(text, "reply", count_in_stats=False)
        db.execute("UPDATE engage_posts SET status = 'replied', draft = ? WHERE account_id = ? AND tweet_id = ?",
                   (text, _acct(), item["tweet_id"]))
    else:
        core.record_failure(text, message, "reply")
    return ok, message


def _engage_worker(stop):
    print("[engage] Worker started.")
    while not stop.is_set():
        try:
            if core.setting_bool("engage_enabled"):
                interval = timedelta(minutes=core.setting_int("engage_interval_minutes", 15, 24 * 60))
                for account in rest.list_accounts():
                    rest.use_account(account["key"])
                    last = _acct_setting("engage_last_check")
                    if not last or core.utcnow() - db.parse_utc(last) >= interval:
                        count, error = check_now()
                        print(f"[engage] Checked @{account['username'] or account['key']}: {count} new"
                              + (f", problems: {error}" if error else ""))
        except Exception as e:
            print(f"[engage] Error: {e}")
        stop.wait(60)


_started = False
_engage_stop = threading.Event()


def start_background():
    """Called once from the app's startup."""
    global _started
    if _started:
        return
    _started = True
    try:
        media_store.fail_interrupted()
    except Exception as e:
        print(f"[studio] Could not tidy the media library: {e}")
    threading.Thread(target=_engage_worker, args=(_engage_stop,), daemon=True).start()


def _item(tweet_id):
    return db.query_one(
        "SELECT tweet_id, kind, author_id, author_username, text, draft, status FROM engage_posts "
        "WHERE account_id = ? AND tweet_id = ?",
        (_acct(), str(tweet_id)),
    )


@bp.route("/engage/watch", methods=["POST"])
@admin
def engage_watch():
    username = request.form.get("username", "").strip().lstrip("@")
    if "/" in username:                       # a pasted profile link
        username = username.rstrip("/").split("/")[-1].split("?")[0]
    if not username or len(username) > 15 or not set(username) <= USERNAME_CHARS:
        flash("Enter an X handle, like @nasa.", "error")
        return _home("engage")
    if not core.acct():
        flash("Connect an X account first.", "error")
        return _home("engage")
    if db.query_one("SELECT 1 AS x FROM watched_accounts WHERE account_id = ? AND LOWER(username) = LOWER(?)",
                    (_acct(), username)):
        flash(f"@{username} is already on the list.", "info")
        return _home("engage")
    data, error = core.x_get(f"/users/by/username/{username}", {"user.fields": "name"})
    if error or not (data or {}).get("data"):
        flash(f"Could not find @{username}: {error or 'no such account'}", "error")
        return _home("engage")
    _count_reads(1)
    user = data["data"]
    db.execute(
        "INSERT OR REPLACE INTO watched_accounts (account_id, user_id, username, name, since_id, added_at) "
        "VALUES (?, ?, ?, ?, '', ?)",
        (_acct(), str(user["id"]), user.get("username", username), user.get("name", ""), db.utcnow_str()),
    )
    flash(f"Now watching @{user.get('username', username)}.", "success")
    return _home("engage")


@bp.route("/engage/unwatch/<user_id>", methods=["POST"])
@admin
def engage_unwatch(user_id):
    db.execute("DELETE FROM watched_accounts WHERE account_id = ? AND user_id = ?", (_acct(), user_id))
    flash("Removed from the watchlist.", "info")
    return _home("engage")


@bp.route("/engage/check", methods=["POST"])
@admin
def engage_check():
    count, error = check_now()
    if error:
        flash(f"Checked, with problems: {error}", "error")
    flash(f"{count} new post(s) found." if count else "Nothing new.", "success" if count else "info")
    return _home("engage")


@bp.route("/engage/item/<tweet_id>/draft", methods=["POST"])
@admin
def engage_draft(tweet_id):
    item = _item(tweet_id)
    if not item:
        return jsonify({"error": "That post is no longer in the list."}), 404
    try:
        return jsonify({"draft": _draft(item)})
    except ai.AIError as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/engage/item/<tweet_id>/status", methods=["POST"])
@admin
def engage_status(tweet_id):
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in ("new", "done", "dismissed"):
        return jsonify({"error": "bad status"}), 400
    db.execute("UPDATE engage_posts SET status = ?, draft = ? WHERE account_id = ? AND tweet_id = ?",
               (status, (body.get("draft") or "")[:core.TWEET_MAX_CHARS], _acct(), str(tweet_id)))
    return jsonify({"ok": True})


@bp.route("/engage/item/<tweet_id>/reply", methods=["POST"])
@admin
def engage_reply(tweet_id):
    """Reply through the API. X only accepts this for posts that mention or quote you."""
    item = _item(tweet_id)
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not item:
        return jsonify({"error": "That post is no longer in the list."}), 404
    if not text or len(text) > core.TWEET_MAX_CHARS:
        return jsonify({"error": f"A reply needs 1 to {core.TWEET_MAX_CHARS} characters."}), 400
    ok, message = _send_reply(item, text)
    if not ok:
        return jsonify({"error": message}), 502
    return jsonify({"ok": True})


@bp.route("/engage/clear", methods=["POST"])
@admin
def engage_clear():
    db.execute("DELETE FROM engage_posts WHERE account_id = ? AND status != 'new'", (_acct(),))
    flash("Cleared handled posts.", "info")
    return _home("engage")


# ---------------------------------------------------------------------------
# Hooks used by webapp.py
# ---------------------------------------------------------------------------

def save_settings(form, problems):
    """Save this module's fields from the Settings form."""
    core.put_setting("ai_voice", form.get("ai_voice", "").strip()[:2000])
    for key in ("ai_text_model", "ai_image_model", "imagineart_image_style", "imagineart_video_style"):
        db.set_setting(key, form.get(key, "").strip()[:80])
    for key in ("engage_enabled", "engage_mentions", "engage_auto_draft", "engage_auto_reply_mentions"):
        db.set_setting(key, "1" if form.get(key) == "1" else "0")
    try:
        minutes = int(form.get("engage_interval_minutes", ""))
        if not 15 <= minutes <= 24 * 60:
            raise ValueError
        db.set_setting("engage_interval_minutes", minutes)
    except ValueError:
        problems.append("engage check interval")


def page_context():
    """Extra template variables for the Create and Engage tabs."""
    watchlist = db.query("SELECT user_id, username, name FROM watched_accounts WHERE account_id = ? "
                         "ORDER BY added_at ASC", (_acct(),))
    items = db.query(
        "SELECT tweet_id, kind, author_username, text, posted_at, draft, status FROM engage_posts "
        "WHERE account_id = ? AND status = 'new' ORDER BY posted_at DESC LIMIT ?",
        (_acct(), ENGAGE_LIST_LIMIT),
    )
    for item in items:
        item["posted_at"] = db.parse_utc(item["posted_at"])
    handled = db.query_one("SELECT COUNT(*) AS n FROM engage_posts WHERE account_id = ? AND status != 'new'",
                           (_acct(),))["n"]
    reads_row = db.query_one("SELECT value FROM bot_stats WHERE key = 'reads_count'")
    reads = reads_row["value"] if reads_row else 0
    last = _acct_setting("engage_last_check")
    return {
        "ai_openai": ai.openai_ready(),
        "ai_imagineart": ai.imagineart_ready(),
        "ai_defaults": {
            "text": ai.DEFAULT_TEXT_MODEL, "image": ai.DEFAULT_IMAGE_MODEL,
            "ia_image": ai.DEFAULT_IMAGINEART_IMAGE_STYLE, "ia_video": ai.DEFAULT_IMAGINEART_VIDEO_STYLE,
        },
        "media_items": [_media_view(item) for item in media_store.recent()],
        "watchlist": watchlist,
        "engage_items": items,
        "engage_handled": handled,
        "engage_last_check": db.parse_utc(last) if last else None,
        "engage_error": _last_errors.get(_acct()),
        "engage_reads": reads,
        "engage_read_spend": reads * core.setting_float("cost_per_read"),
        "engage_intervals": [(m, core.humanize_minutes(m)) for m in ENGAGE_INTERVALS],
    }
