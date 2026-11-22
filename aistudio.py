#!/usr/bin/env python3
"""
AI Studio: composes posts with a text model, makes images for them, and can
run on its own ("autopilot").

How a post travels:

    compose (text model)  ->  ai_drafts  ->  [image model]  ->  approve
                                                                   |
                                    scheduled_posts (a time was picked)
                                    tweet_queue     (no time: the queue decides)

Everything goes through ai_drafts. With "review before posting" on, drafts wait
for the user. With it off, autopilot drafts are approved automatically as soon
as their image (if any) is ready. A draft whose image failed is never posted
automatically.

One background thread does all slow work (model calls), so page requests return
at once and nothing runs twice in parallel.
"""
import random
import re
import threading
from datetime import datetime, timedelta, timezone

from flask import Response, flash, jsonify, redirect, request, url_for

import ai
import db

core = None     # the webapp module, set by register()

TONES = ["casual", "professional", "witty", "inspirational", "educational", "bold", "friendly", "minimal"]
FORMAT_MODES = {
    "text_only": "Text only",
    "image_only": "Always image + caption",
    "mixed": "Mix (use the share below)",
    "auto": "Let the AI decide per post",
}
TIMING_MODES = {
    "ai": "AI picks the best times",
    "even": "Spread evenly through the day",
    "queue": "Add to the queue (auto-post interval decides)",
}
HASHTAG_MODES = {"none": "No hashtags", "few": "One or two", "many": "Several"}
EMOJI_MODES = {"none": "No emoji", "some": "A few", "lots": "Plenty"}
QUALITIES = ["low", "medium", "high"]

# Built-in image categories. The hint is added to the image prompt.
CATEGORIES = {
    "animals": ("Animals", "The subject is an animal or animals, shown naturally and in sharp detail."),
    "humans": ("Humans", "The subject is a person or people, with natural poses, realistic anatomy and believable faces."),
    "places": ("Places", "The subject is a place: a city, landmark, street, interior or landscape with a strong sense of location."),
    "nature": ("Nature", "The subject is the natural world: plants, weather, water, mountains or sky."),
    "food": ("Food & drink", "The subject is food or drink, styled appetisingly."),
    "technology": ("Technology", "The subject is technology: devices, machines, code or futuristic concepts."),
    "objects": ("Objects & products", "The subject is a single object or product, cleanly composed."),
    "abstract": ("Abstract", "An abstract composition of shape, colour and texture rather than a literal scene."),
    "illustration": ("Illustration", "A drawn or painted illustration rather than a photograph."),
}

AI_DEFAULTS = {
    "ai_text_provider": "openai",
    "ai_text_model": "",
    "ai_image_provider": "openai",
    "ai_image_model": "",
    "ai_image_quality": "medium",
    "ai_image_aspect": "square",
    "ai_image_style": "High quality. No text, captions, watermarks or logos in the image.",
    "ai_persona": "",
    "ai_topics": "",
    "ai_tone": "casual",
    "ai_language": "English",
    "ai_hashtags": "none",
    "ai_emoji": "some",
    "ai_avoid": "",
    "ai_instructions": "",
    "ai_max_chars": "260",
    "ai_format_mode": "mixed",
    "ai_image_ratio": "30",
    "ai_posts_per_day": "3",
    "ai_timing_mode": "ai",
    "ai_window_start": "08:00",
    "ai_window_end": "21:00",
    "ai_require_approval": "1",
    "ai_autopilot_enabled": "0",
    # State
    "ai_planned_for": "",
    "ai_last_error": "",
    "ai_last_error_at": "",
    "ai_last_run_at": "",
    "ai_models_openai": "",
    "ai_models_gemini": "",
    "x_media_upload": "",        # "ok" or "failed", set by the first image post
}
PREF_KEYS = [k for k in AI_DEFAULTS if k not in (
    "ai_planned_for", "ai_last_error", "ai_last_error_at", "ai_last_run_at",
    "ai_models_openai", "ai_models_gemini", "x_media_upload", "ai_autopilot_enabled")]

MAX_POSTS_PER_DAY = 12
MAX_COMPOSE = 10
MIN_GAP = timedelta(minutes=20)
SERVICE_POLL = 20
RETRY_AFTER_ERROR = timedelta(minutes=30)
MEDIA_SWEEP_EVERY = timedelta(minutes=30)
TEXT_FIELD_LIMIT = 2000


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def x_length(text):
    """Length the way X counts it: links are 23, CJK and emoji are 2."""
    text = core.URL_RE.sub("x" * 23, text or "")
    total = 0
    for ch in text:
        c = ord(ch)
        light = c <= 4351 or 8192 <= c <= 8205 or 8208 <= c <= 8223 or 8242 <= c <= 8247
        total += 1 if light else 2
    return total


def clean_text(text):
    text = str(text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    # The queue export wraps tweets in [brackets]; keep them out of the text.
    return text.replace("[", "(").replace("]", ")")


def pref(key):
    return core.setting(key)


def pref_int(key, low, high):
    return core.setting_int(key, low, high)


def _bump(counter):
    try:
        db.execute(
            "INSERT INTO bot_stats (key, value) VALUES (?, 1) "
            "ON CONFLICT(key) DO UPDATE SET value = value + 1", (counter,))
    except Exception:
        pass


def _counter(name):
    row = db.query_one("SELECT value FROM bot_stats WHERE key = ?", (name,))
    return row["value"] if row else 0


def get_presets(enabled_only=False):
    where = "WHERE enabled = 1 " if enabled_only else ""
    return db.query(f"SELECT id, name, category, prompt, enabled FROM image_presets {where}ORDER BY id ASC")


def get_draft(draft_id):
    return db.query_one("SELECT * FROM ai_drafts WHERE id = ?", (draft_id,))


# ---------------------------------------------------------------------------
# Posting window and times
# ---------------------------------------------------------------------------

def _window(day, tz):
    """The allowed posting window on a local date, as aware local datetimes."""
    try:
        start = core._parse_hhmm(pref("ai_window_start"))
        end = core._parse_hhmm(pref("ai_window_end"))
    except ValueError:
        start, end = 8 * 60, 21 * 60
    if start >= end:
        start, end = 0, 23 * 60 + 59
    base = datetime(day.year, day.month, day.day, tzinfo=tz)
    return base + timedelta(minutes=start), base + timedelta(minutes=end)


def plan_target(now_utc=None):
    """Which day to plan and how much of its window is left.

    Returns (local_date, earliest, end, fraction_of_window_left).
    """
    tz = core.user_tz()
    now_local = (now_utc or core.utcnow()).astimezone(tz)
    start, end = _window(now_local.date(), tz)
    earliest = max(now_local + timedelta(minutes=10), start)
    if earliest <= end - timedelta(minutes=30):
        fraction = (end - earliest) / (end - start)
        return now_local.date(), earliest, end, fraction
    tomorrow = now_local.date() + timedelta(days=1)
    start, end = _window(tomorrow, tz)
    return tomorrow, start, end, 1.0


def even_slots(count, earliest, end):
    span = (end - earliest) / count
    jitter = min(10.0, span.total_seconds() / 60 / 4)
    return [earliest + span * (i + 0.5) + timedelta(minutes=random.uniform(-jitter, jitter))
            for i in range(count)]


def assign_times(posts, earliest, end, use_model_times):
    """Give every post a local datetime inside the window, at least MIN_GAP apart."""
    fallback = even_slots(len(posts), earliest, end)
    times = []
    for post, slot in zip(posts, fallback):
        when = None
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(post.get("time") or "")) if use_model_times else None
        if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
            candidate = earliest.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
            if earliest <= candidate <= end:
                when = candidate
        times.append(when or slot)
    order = sorted(range(len(posts)), key=lambda i: times[i])
    previous = None
    for i in order:
        if previous and times[i] < previous + MIN_GAP:
            times[i] = previous + MIN_GAP
        previous = times[i]
        posts[i]["when"] = times[i].replace(second=0, microsecond=0)
    return [posts[i] for i in order]


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def pick_formats(count, mode):
    """Decide per post: 'text', 'image', or 'auto' (the model chooses)."""
    if mode == "text_only":
        return ["text"] * count
    if mode == "image_only":
        return ["image"] * count
    if mode == "auto":
        return ["auto"] * count
    exact = count * pref_int("ai_image_ratio", 0, 100) / 100
    images = int(exact) + (1 if random.random() < exact - int(exact) else 0)
    chosen = set(random.sample(range(count), min(count, images)))
    return ["image" if i in chosen else "text" for i in range(count)]


def build_system_prompt():
    lines = [
        "You write posts for an X (Twitter) account. You write as the account owner, never as an assistant.",
        f"Write in {pref('ai_language') or 'English'}.",
        f"Tone: {pref('ai_tone')}.",
        f"Every post must be at most {pref_int('ai_max_chars', 50, 280)} characters. Links count as 23.",
    ]
    if pref("ai_persona"):
        lines.append(f"About the account and its audience: {pref('ai_persona')}")
    if pref("ai_topics"):
        lines.append(f"Topics to post about: {pref('ai_topics')}")
    lines.append({
        "none": "Do not use hashtags.",
        "few": "Use at most one or two relevant hashtags, and only when they add reach.",
        "many": "Use three to five relevant hashtags.",
    }[pref("ai_hashtags") if pref("ai_hashtags") in HASHTAG_MODES else "none"])
    lines.append({
        "none": "Do not use emoji.",
        "some": "Emoji are fine in moderation.",
        "lots": "Use emoji freely.",
    }[pref("ai_emoji") if pref("ai_emoji") in EMOJI_MODES else "some"])
    if pref("ai_avoid"):
        lines.append(f"Never do or mention: {pref('ai_avoid')}")
    if pref("ai_instructions"):
        lines.append(f"The owner's own instructions, which override the defaults above: {pref('ai_instructions')}")
    lines += [
        "Each post stands alone, sounds like a real person, and is different in angle and wording from the others "
        "and from the recent posts you are shown. Do not invent facts, statistics, quotes or news. "
        "Do not use square brackets.",
        "Reply with one JSON object and nothing else.",
    ]
    return "\n".join(lines)


def build_user_prompt(formats, brief, presets, forced_preset, recent, timing):
    count = len(formats)
    lines = [f"Write {count} post{'s' if count != 1 else ''}."]
    if brief:
        lines.append(f"What the owner wants this time: {brief}")
    else:
        lines.append("Choose the subjects yourself from the account's topics. Vary them.")

    lines.append("Format of each post:")
    for i, fmt in enumerate(formats, 1):
        lines.append(f"  {i}. " + {
            "text": "text only (image: false).",
            "image": "an image with a caption (image: true). The text is the caption.",
            "auto": "your choice. Set image true only if a picture would clearly make the post better.",
        }[fmt])

    if any(f != "text" for f in formats):
        lines.append("For a post with an image, image_prompt describes the picture in one or two concrete "
                     "sentences: subject, setting, mood. Describe a picture, not the post.")
        if forced_preset:
            lines.append(f"Every image uses this preset, so the caption must suit it: "
                         f"{describe_preset(forced_preset)} Set preset_id to {forced_preset['id']}.")
        elif presets:
            lines.append("The owner only wants these kinds of image. Pick one preset per image, set preset_id "
                         "to its id, and make image_prompt fit it:")
            lines += [f"  id {p['id']}: {describe_preset(p)}" for p in presets]

    if timing:
        day, earliest, end, quiet = timing
        lines.append(f"Also choose when each post should go out on {day.strftime('%A %d %B %Y')} "
                     f"(timezone {core.user_tz()}). Allowed window: {earliest.strftime('%H:%M')} to "
                     f"{end.strftime('%H:%M')}. Pick the times this audience is most likely to be scrolling, "
                     "keep posts at least an hour apart where the window allows, and say why in reason. "
                     "Give time as 24-hour HH:MM.")
        if quiet:
            lines.append(f"Do not pick a time between {quiet[0]} and {quiet[1]}.")

    if recent:
        lines.append("Recent and upcoming posts. Do not repeat or closely paraphrase them:")
        lines += [f"  - {text[:200]}" for text in recent]

    lines.append('JSON shape: {"posts": [{"text": string, "image": boolean, "image_prompt": string, '
                 '"preset_id": number or null, "time": "HH:MM" or null, "reason": string}]}. '
                 f"Return exactly {count} item{'s' if count != 1 else ''}, in the order listed above.")
    return "\n".join(lines)


def describe_preset(preset):
    parts = [f"\"{preset['name']}\"."]
    if preset["category"] in CATEGORIES:
        parts.append(f"Category: {CATEGORIES[preset['category']][0]}.")
    if preset["prompt"]:
        parts.append(f"Look: {preset['prompt']}")
    return " ".join(parts)


def build_image_prompt(image_prompt, preset):
    parts = [image_prompt.strip()]
    if preset:
        if preset["category"] in CATEGORIES:
            parts.append(CATEGORIES[preset["category"]][1])
        if preset["prompt"]:
            parts.append(preset["prompt"].strip())
    if pref("ai_image_style"):
        parts.append(pref("ai_image_style").strip())
    return "\n".join(p for p in parts if p)


def recent_texts():
    rows = db.query("SELECT text FROM post_history ORDER BY posted_at DESC, id DESC LIMIT 25")
    rows += db.query("SELECT text FROM tweet_queue WHERE posted = 0 ORDER BY id DESC LIMIT 15")
    rows += db.query("SELECT text FROM scheduled_posts WHERE posted = 0 ORDER BY id DESC LIMIT 15")
    rows += db.query("SELECT text FROM ai_drafts ORDER BY id DESC LIMIT 15")
    return list(dict.fromkeys(row["text"] for row in rows))


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------

def compose(count, brief="", format_mode=None, preset_id=None, origin="manual",
            auto_approve=False, with_times=True, target=None):
    """Write `count` posts and store them as drafts. Returns how many were stored.

    Raises ai.AIError when the text model fails or returns nothing usable.
    """
    count = max(1, min(MAX_POSTS_PER_DAY, count))
    mode = format_mode if format_mode in FORMAT_MODES else pref("ai_format_mode")
    formats = pick_formats(count, mode)

    presets = get_presets(enabled_only=True)
    forced = None
    if preset_id:
        forced = db.query_one("SELECT id, name, category, prompt, enabled FROM image_presets WHERE id = ?", (preset_id,))
    by_id = {p["id"]: p for p in presets}

    timing = None
    day, earliest, end, _ = target or plan_target()
    use_model_times = with_times and pref("ai_timing_mode") == "ai"
    if use_model_times:
        quiet = (pref("quiet_start"), pref("quiet_end")) if core.setting_bool("quiet_hours_enabled") else None
        timing = (day, earliest, end, quiet)

    recent = recent_texts()
    provider = ai.resolve_provider(pref("ai_text_provider"))
    result = ai.generate_json(
        provider, pref("ai_text_model"),
        build_system_prompt(),
        build_user_prompt(formats, brief, presets, forced, recent[:40], timing),
    )
    _bump("ai_text_calls")

    raw_posts = result.get("posts") if isinstance(result, dict) else None
    if not isinstance(raw_posts, list):
        raise ai.AIError("The model's reply had no posts in it. Try again.")

    seen = set(recent)
    posts = []
    for fmt, raw in zip(formats, raw_posts):
        if not isinstance(raw, dict):
            continue
        text = clean_text(raw.get("text"))
        if not text or x_length(text) > core.TWEET_MAX_CHARS or text in seen:
            continue
        seen.add(text)
        wants_image = fmt == "image" or (fmt == "auto" and bool(raw.get("image")))
        preset = None
        if wants_image:
            preset = forced
            if preset is None and presets:
                try:
                    preset = by_id.get(int(raw.get("preset_id")))
                except (TypeError, ValueError):
                    preset = None
                preset = preset or random.choice(presets)
        posts.append({
            "text": text,
            "wants_image": wants_image,
            "image_prompt": str(raw.get("image_prompt") or "").strip()[:1000] or text,
            "preset_id": preset["id"] if preset else None,
            "time": raw.get("time"),
            "reason": str(raw.get("reason") or "").strip()[:300],
        })
    if not posts:
        raise ai.AIError("The model wrote nothing usable (too long, empty, or repeats of earlier posts). Try again.")

    if with_times and pref("ai_timing_mode") != "queue":
        posts = assign_times(posts, earliest, end, use_model_times)

    now = db.utcnow_str()
    for post in posts:
        when = post.get("when")
        db.execute(
            "INSERT INTO ai_drafts (text, wants_image, image_prompt, preset_id, image_status, suggested_at, "
            "reason, origin, auto_approve, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (post["text"], 1 if post["wants_image"] else 0, post["image_prompt"] if post["wants_image"] else "",
             post["preset_id"], "pending" if post["wants_image"] else "none",
             db.to_db_str(when) if when else None, post["reason"], origin, 1 if auto_approve else 0, now),
        )
    return len(posts)


# ---------------------------------------------------------------------------
# Images and approval
# ---------------------------------------------------------------------------

def make_image(draft):
    """Generate the image for one draft and record the result on the draft."""
    preset = None
    if draft.get("preset_id"):
        preset = db.query_one("SELECT id, name, category, prompt FROM image_presets WHERE id = ?", (draft["preset_id"],))
    prompt = build_image_prompt(draft["image_prompt"] or draft["text"], preset)
    try:
        provider = ai.resolve_provider(pref("ai_image_provider"))
        raw, mime = ai.generate_image(provider, pref("ai_image_model"), prompt,
                                      pref("ai_image_aspect"), pref("ai_image_quality"))
        _bump("ai_image_calls")
        media_id = db.save_media(raw, mime, prompt)
    except Exception as e:
        message = str(e) if isinstance(e, ai.AIError) else f"Could not store the image: {type(e).__name__}"
        db.execute("UPDATE ai_drafts SET image_status = 'failed', image_error = ? WHERE id = ?",
                   (message[:400], draft["id"]))
        print(f"[ai] Image failed for draft {draft['id']}: {message}")
        return False
    old = draft.get("media_id")
    db.execute("UPDATE ai_drafts SET image_status = 'ready', image_error = '', media_id = ? WHERE id = ?",
               (media_id, draft["id"]))
    if old:
        core.discard_media(old)
    return True


def approve_draft(draft, how):
    """Move a draft to the schedule or the queue, or post it now. Returns (ok, message)."""
    if draft["image_status"] == "pending":
        return False, "The image is still being made."
    if draft["image_status"] == "failed":
        return False, "The image failed. Retry it or remove it first."
    media_id = draft["media_id"] if draft["image_status"] == "ready" else None
    now = core.utcnow()

    if how == "now":
        ok, message = core.post_tweet(draft["text"], media_id)
        if not ok:
            core.record_failure(draft["text"], message, "profile")
            return False, message
        core.mark_tweet_posted(None, draft["text"], media_id)
        message = "Posted."
    elif how == "schedule":
        when = db.parse_utc(draft["suggested_at"]) if draft.get("suggested_at") else None
        if when is None or when < now + timedelta(minutes=1):
            when = now + timedelta(minutes=2)
        db.execute(
            "INSERT INTO scheduled_posts (text, scheduled_at, created_at, media_id) VALUES (?, ?, ?, ?)",
            (draft["text"], db.to_db_str(when), db.utcnow_str(), media_id),
        )
        core.start_scheduler_if_needed()
        message = "Scheduled."
    else:
        db.execute("INSERT INTO tweet_queue (text, created_at, media_id) VALUES (?, ?, ?)",
                   (draft["text"], db.utcnow_str(), media_id))
        message = "Added to the queue."
    db.execute("DELETE FROM ai_drafts WHERE id = ?", (draft["id"],))
    return True, message


def default_destination(draft):
    return "schedule" if draft.get("suggested_at") else "queue"


def delete_draft(draft):
    db.execute("DELETE FROM ai_drafts WHERE id = ?", (draft["id"],))
    core.discard_media(draft.get("media_id"))


def sweep_orphan_media():
    """Delete stored images no draft, queued or scheduled post points at any more."""
    cutoff = db.to_db_str(core.utcnow() - timedelta(hours=1))
    orphans = (
        "SELECT id FROM media WHERE created_at < ? "
        "AND id NOT IN (SELECT media_id FROM tweet_queue WHERE media_id IS NOT NULL) "
        "AND id NOT IN (SELECT media_id FROM scheduled_posts WHERE media_id IS NOT NULL) "
        "AND id NOT IN (SELECT media_id FROM ai_drafts WHERE media_id IS NOT NULL)"
    )
    ids = [row["id"] for row in db.query(orphans, (cutoff,))]
    for media_id in ids:
        db.delete_media(media_id)
    return len(ids)


# ---------------------------------------------------------------------------
# Background service
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_wake = threading.Event()
_jobs = []
_state = {"started": False, "busy": None, "retry_after": None, "last_sweep": None}


def _note_error(message):
    db.set_setting("ai_last_error", message[:400])
    db.set_setting("ai_last_error_at", db.utcnow_str())
    print(f"[ai] {message}")


def _note_success():
    db.set_setting("ai_last_run_at", db.utcnow_str())
    if pref("ai_last_error"):
        db.set_setting("ai_last_error", "")


def enqueue(job):
    with _lock:
        _jobs.append(job)
    _wake.set()


def run_plan(force=False):
    """Plan the target day's posts. Returns how many drafts were made."""
    target = plan_target()
    day, _, _, fraction = target
    if not force and (pref("ai_planned_for") or "") >= day.isoformat():
        return 0
    count = max(1, round(pref_int("ai_posts_per_day", 1, MAX_POSTS_PER_DAY) * fraction))
    made = compose(count, origin="autopilot", auto_approve=not core.setting_bool("ai_require_approval"),
                   target=target)
    db.set_setting("ai_planned_for", day.isoformat())
    return made


def _run_job(job):
    try:
        if job["kind"] == "plan":
            run_plan(force=True)
        else:
            compose(job["count"], brief=job.get("brief", ""), format_mode=job.get("format_mode"),
                    preset_id=job.get("preset_id"), with_times=job.get("with_times", True))
        _note_success()
    except ai.AIError as e:
        _note_error(str(e))
    except Exception as e:
        _note_error(f"Unexpected error while composing: {type(e).__name__}: {e}")


def _tick():
    while True:
        with _lock:
            job = _jobs.pop(0) if _jobs else None
        if not job:
            break
        _state["busy"] = "Planning the day" if job["kind"] == "plan" else "Writing posts"
        _run_job(job)

    now = core.utcnow()
    if core.setting_bool("ai_autopilot_enabled") and not (_state["retry_after"] and now < _state["retry_after"]):
        day = plan_target(now)[0]
        if (pref("ai_planned_for") or "") < day.isoformat():
            _state["busy"] = "Planning the day"
            try:
                run_plan()
                _note_success()
                _state["retry_after"] = None
            except ai.AIError as e:
                _note_error(f"Autopilot could not plan: {e}")
                _state["retry_after"] = now + RETRY_AFTER_ERROR
            except Exception as e:
                _note_error(f"Autopilot hit an unexpected error: {type(e).__name__}: {e}")
                _state["retry_after"] = now + RETRY_AFTER_ERROR

    pending = db.query("SELECT * FROM ai_drafts WHERE image_status = 'pending' ORDER BY id ASC LIMIT 4")
    for draft in pending:
        _state["busy"] = "Making images"
        make_image(draft)

    ready = db.query(
        "SELECT * FROM ai_drafts WHERE auto_approve = 1 AND image_status IN ('none', 'ready') ORDER BY id ASC")
    for draft in ready:
        approve_draft(draft, default_destination(draft))

    if not _state["last_sweep"] or now - _state["last_sweep"] > MEDIA_SWEEP_EVERY:
        _state["last_sweep"] = now
        sweep_orphan_media()

    _state["busy"] = None
    return bool(pending)


def _service():
    print("[ai] Service started.")
    while True:
        more = False
        try:
            more = _tick()
        except Exception as e:
            _state["busy"] = None
            print(f"[ai] Service error: {e}")
        if not more:
            _wake.wait(SERVICE_POLL)
        _wake.clear()


def start_service():
    with _lock:
        if _state["started"]:
            return
        _state["started"] = True
    threading.Thread(target=_service, daemon=True).start()


def status():
    with _lock:
        queued_jobs = len(_jobs)
    counts = db.query_one(
        "SELECT COUNT(*) AS drafts, "
        "COALESCE(SUM(CASE WHEN image_status = 'pending' THEN 1 ELSE 0 END), 0) AS pending FROM ai_drafts")
    return {
        "busy": _state["busy"] or ("Waiting to start" if queued_jobs else None),
        "drafts": counts["drafts"],
        "pendingImages": counts["pending"],
    }


# ---------------------------------------------------------------------------
# Page data
# ---------------------------------------------------------------------------

def page_context():
    prefs = {key: pref(key) for key in AI_DEFAULTS}
    drafts = db.query("SELECT * FROM ai_drafts ORDER BY COALESCE(suggested_at, '9999') ASC, id ASC")
    presets = get_presets()
    preset_names = {p["id"]: p["name"] for p in presets}
    for draft in drafts:
        draft["suggested_at"] = db.parse_utc(draft["suggested_at"]) if draft["suggested_at"] else None
        draft["preset_name"] = preset_names.get(draft["preset_id"])
        draft["length"] = x_length(draft["text"])
    keys = ai.available()
    try:
        text_provider = ai.resolve_provider(prefs["ai_text_provider"])
        image_provider = ai.resolve_provider(prefs["ai_image_provider"])
    except ai.AIError:
        text_provider = image_provider = None
    day, earliest, end, fraction = plan_target()
    per_day = pref_int("ai_posts_per_day", 1, MAX_POSTS_PER_DAY)

    def known(provider, kind):
        return [m for m in (prefs[f"ai_models_{provider}"] or "").split("|") if m.startswith(kind + ":")]

    models = {
        kind: sorted({m.split(":", 1)[1] for p in ai.PROVIDERS for m in known(p, kind)}
                     | {ai.default_model(p, kind) for p in ai.PROVIDERS})
        for kind in ("text", "image")
    }
    return {
        "prefs": prefs,
        "drafts": drafts,
        "presets": presets,
        "categories": {key: label for key, (label, _) in CATEGORIES.items()},
        "have_keys": keys,
        "any_key": any(keys.values()),
        "providers": ai.PROVIDER_LABELS,
        "text_provider": text_provider,
        "image_provider": image_provider,
        "text_model": prefs["ai_text_model"] or (ai.default_model(text_provider, "text") if text_provider else ""),
        "image_model": prefs["ai_image_model"] or (ai.default_model(image_provider, "image") if image_provider else ""),
        "models": models,
        "tones": TONES,
        "format_modes": FORMAT_MODES,
        "timing_modes": TIMING_MODES,
        "hashtag_modes": HASHTAG_MODES,
        "emoji_modes": EMOJI_MODES,
        "qualities": QUALITIES,
        "aspects": list(ai.ASPECTS),
        "autopilot": prefs["ai_autopilot_enabled"] == "1",
        "review": prefs["ai_require_approval"] == "1",
        "last_error": prefs["ai_last_error"],
        "last_error_at": db.parse_utc(prefs["ai_last_error_at"]) if prefs["ai_last_error_at"] else None,
        "last_run_at": db.parse_utc(prefs["ai_last_run_at"]) if prefs["ai_last_run_at"] else None,
        "planned_for": prefs["ai_planned_for"],
        "next_plan_day": day.isoformat(),
        "next_plan_count": max(1, round(per_day * fraction)),
        "text_calls": _counter("ai_text_calls"),
        "image_calls": _counter("ai_image_calls"),
        "x_media_upload": prefs["x_media_upload"],
        "max_compose": MAX_COMPOSE,
        "max_per_day": MAX_POSTS_PER_DAY,
        "status": status(),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _back():
    return redirect(url_for("index") + "#ai")


def _draft_or_flash(draft_id):
    draft = get_draft(draft_id)
    if not draft:
        flash("That draft no longer exists.", "error")
    return draft


def register(app, core_module):
    global core
    core = core_module
    core.DEFAULT_SETTINGS.update(AI_DEFAULTS)
    admin = core.require_admin

    @app.route("/media/<int:media_id>")
    @admin
    def ai_media(media_id):
        stored = db.load_media(media_id)
        if not stored:
            return Response("Not found", status=404)
        raw, mime = stored
        return Response(raw, mimetype=mime, headers={"Cache-Control": "private, max-age=86400"})

    @app.route("/api/ai/status")
    @admin
    def ai_status():
        return jsonify(status())

    @app.route("/ai/settings", methods=["POST"])
    @admin
    def ai_save_settings():
        form = request.form
        problems = []

        def choice(key, allowed):
            if form.get(key) in allowed:
                db.set_setting(key, form[key])
            else:
                problems.append(key.replace("ai_", "").replace("_", " "))

        def number(key, low, high):
            try:
                value = int(form.get(key, ""))
                if not low <= value <= high:
                    raise ValueError
                db.set_setting(key, value)
            except ValueError:
                problems.append(key.replace("ai_", "").replace("_", " "))

        choice("ai_text_provider", ai.PROVIDERS)
        choice("ai_image_provider", ai.PROVIDERS)
        choice("ai_image_quality", QUALITIES)
        choice("ai_image_aspect", ai.ASPECTS)
        choice("ai_tone", TONES)
        choice("ai_hashtags", HASHTAG_MODES)
        choice("ai_emoji", EMOJI_MODES)
        choice("ai_format_mode", FORMAT_MODES)
        choice("ai_timing_mode", TIMING_MODES)
        number("ai_max_chars", 50, 280)
        number("ai_image_ratio", 0, 100)
        number("ai_posts_per_day", 1, MAX_POSTS_PER_DAY)
        for key in ("ai_text_model", "ai_image_model"):
            value = form.get(key, "").strip()
            if re.fullmatch(r"[A-Za-z0-9._:\-/]{0,100}", value):
                db.set_setting(key, value)
            else:
                problems.append("model name")
        for key in ("ai_persona", "ai_topics", "ai_avoid", "ai_instructions", "ai_image_style"):
            db.set_setting(key, form.get(key, "").strip()[:TEXT_FIELD_LIMIT])
        db.set_setting("ai_language", form.get("ai_language", "").strip()[:60] or "English")
        for key in ("ai_window_start", "ai_window_end"):
            value = form.get(key, "").strip()
            try:
                core._parse_hhmm(value)
                db.set_setting(key, value)
            except ValueError:
                problems.append("posting window")
        db.set_setting("ai_require_approval", "1" if form.get("ai_require_approval") == "1" else "0")

        if problems:
            flash(f'Saved, except these invalid values: {", ".join(sorted(set(problems)))}.', "error")
        else:
            flash("AI preferences saved.", "success")
        return _back()

    @app.route("/ai/autopilot", methods=["POST"])
    @admin
    def ai_autopilot():
        turn_on = request.form.get("enabled") == "1"
        if turn_on and not any(ai.available().values()):
            flash("Add OPENAI_API_KEY or GEMINI_API_KEY to the environment before turning autopilot on.", "error")
            return _back()
        db.set_setting("ai_autopilot_enabled", "1" if turn_on else "0")
        _state["retry_after"] = None
        if turn_on:
            _wake.set()
            review = "Drafts will wait for your review." if core.setting_bool("ai_require_approval") \
                else "Posts will go out without review."
            flash(f"Autopilot is on. {review}", "success")
        else:
            flash("Autopilot is off. Posts already scheduled or queued are not affected.", "info")
        return _back()

    @app.route("/ai/plan", methods=["POST"])
    @admin
    def ai_plan_now():
        if not any(ai.available().values()):
            flash("No AI key is set yet.", "error")
            return _back()
        enqueue({"kind": "plan"})
        flash("Planning the day. Drafts will appear here shortly.", "info")
        return _back()

    @app.route("/ai/compose", methods=["POST"])
    @admin
    def ai_compose():
        if not any(ai.available().values()):
            flash("No AI key is set yet. Add OPENAI_API_KEY or GEMINI_API_KEY and restart.", "error")
            return _back()
        try:
            count = max(1, min(MAX_COMPOSE, int(request.form.get("count", "1"))))
        except ValueError:
            count = 1
        format_mode = request.form.get("format_mode")
        enqueue({
            "kind": "compose",
            "count": count,
            "brief": request.form.get("brief", "").strip()[:TEXT_FIELD_LIMIT],
            "format_mode": format_mode if format_mode in FORMAT_MODES else None,
            "preset_id": core.parse_int_id(request.form.get("preset_id")),
            "with_times": request.form.get("with_times") == "1",
        })
        flash(f"Writing {count} post{'s' if count != 1 else ''}. Drafts will appear here shortly.", "info")
        return _back()

    @app.route("/ai/test/<provider>", methods=["POST"])
    @admin
    def ai_test(provider):
        if provider not in ai.PROVIDERS:
            return _back()
        label = ai.PROVIDER_LABELS[provider]
        try:
            models = ai.list_models(provider)
        except ai.AIError as e:
            flash(str(e), "error")
            return _back()
        stored = [f"text:{m}" for m in models["text"]] + [f"image:{m}" for m in models["image"]]
        db.set_setting(f"ai_models_{provider}", "|".join(stored)[:20000])
        flash(f"{label} is connected: {len(models['text'])} text and {len(models['image'])} image models "
              "available. They are now suggested in the model fields.", "success")
        for kind in ("text", "image"):
            wanted = pref(f"ai_{kind}_model") or ai.default_model(provider, kind)
            if ai.resolve_provider(pref(f"ai_{kind}_provider")) == provider and wanted not in models[kind]:
                flash(f'{label} does not list the {kind} model "{wanted}". Pick one from the suggestions '
                      "in AI preferences.", "error")
        return _back()

    # ---- Drafts ----

    @app.route("/ai/drafts/<int:draft_id>/approve", methods=["POST"])
    @admin
    def ai_draft_approve(draft_id):
        draft = _draft_or_flash(draft_id)
        if draft:
            how = request.form.get("how")
            how = how if how in ("schedule", "queue", "now") else default_destination(draft)
            ok, message = approve_draft(draft, how)
            flash(message if ok else f"Could not do that: {message}", "success" if ok else "error")
        return _back()

    @app.route("/ai/drafts/approve_all", methods=["POST"])
    @admin
    def ai_drafts_approve_all():
        done = skipped = 0
        for draft in db.query("SELECT * FROM ai_drafts ORDER BY id ASC"):
            ok, _ = approve_draft(draft, default_destination(draft))
            done, skipped = done + ok, skipped + (not ok)
        note = f" {skipped} still need attention (image pending or failed)." if skipped else ""
        flash(f"Approved {done} draft(s).{note}", "success" if done else "info")
        return _back()

    @app.route("/ai/drafts/clear", methods=["POST"])
    @admin
    def ai_drafts_clear():
        drafts = db.query("SELECT id, media_id FROM ai_drafts")
        for draft in drafts:
            delete_draft(draft)
        flash(f"Discarded {len(drafts)} draft(s).", "info")
        return _back()

    @app.route("/ai/drafts/<int:draft_id>/update", methods=["POST"])
    @admin
    def ai_draft_update(draft_id):
        draft = _draft_or_flash(draft_id)
        if not draft:
            return _back()
        text = request.form.get("text", "").strip()
        if not text:
            flash("The post text cannot be empty.", "error")
            return _back()
        try:
            when = core.parse_schedule_datetime(request.form.get("suggested_at", "").strip(),
                                                request.form.get("suggested_at_utc", "").strip())
        except ValueError:
            flash("Invalid date/time.", "error")
            return _back()
        db.execute("UPDATE ai_drafts SET text = ?, suggested_at = ? WHERE id = ?",
                   (text, db.to_db_str(when) if when else None, draft_id))
        flash("Draft updated.", "success")
        return _back()

    @app.route("/ai/drafts/<int:draft_id>/image", methods=["POST"])
    @admin
    def ai_draft_image(draft_id):
        """Make, remake or remove the image on a draft."""
        draft = _draft_or_flash(draft_id)
        if not draft:
            return _back()
        if request.form.get("action") == "remove":
            db.execute("UPDATE ai_drafts SET wants_image = 0, image_status = 'none', image_error = '', "
                       "media_id = NULL WHERE id = ?", (draft_id,))
            core.discard_media(draft.get("media_id"))
            flash("Image removed. This is now a text post.", "info")
            return _back()
        prompt = request.form.get("image_prompt", "").strip()[:1000] or draft["image_prompt"] or draft["text"]
        preset_id = core.parse_int_id(request.form.get("preset_id"))
        if "preset_id" not in request.form:
            preset_id = draft.get("preset_id")
        db.execute("UPDATE ai_drafts SET wants_image = 1, image_status = 'pending', image_error = '', "
                   "image_prompt = ?, preset_id = ? WHERE id = ?", (prompt, preset_id, draft_id))
        _wake.set()
        flash("Making the image. It will appear on the draft shortly.", "info")
        return _back()

    @app.route("/ai/drafts/<int:draft_id>/delete", methods=["POST"])
    @admin
    def ai_draft_delete(draft_id):
        draft = _draft_or_flash(draft_id)
        if draft:
            delete_draft(draft)
            flash("Draft discarded.", "info")
        return _back()

    # ---- Image presets ----

    @app.route("/ai/presets/save", methods=["POST"])
    @admin
    def ai_preset_save():
        name = request.form.get("name", "").strip()[:80]
        category = request.form.get("category", "")
        category = category if category in CATEGORIES else ""
        prompt = request.form.get("prompt", "").strip()[:TEXT_FIELD_LIMIT]
        if not category and not prompt:
            flash("Pick a category, write a prompt, or both.", "error")
            return _back()
        name = name or (CATEGORIES[category][0] if category else "Custom")
        preset_id = core.parse_int_id(request.form.get("preset_id"))
        if preset_id:
            db.execute("UPDATE image_presets SET name = ?, category = ?, prompt = ? WHERE id = ?",
                       (name, category, prompt, preset_id))
            flash(f'Preset "{name}" updated.', "success")
        else:
            db.execute("INSERT INTO image_presets (name, category, prompt, enabled, created_at) VALUES (?, ?, ?, 1, ?)",
                       (name, category, prompt, db.utcnow_str()))
            flash(f'Preset "{name}" added.', "success")
        return _back()

    @app.route("/ai/presets/<int:preset_id>/toggle", methods=["POST"])
    @admin
    def ai_preset_toggle(preset_id):
        db.execute("UPDATE image_presets SET enabled = 1 - enabled WHERE id = ?", (preset_id,))
        return _back()

    @app.route("/ai/presets/<int:preset_id>/delete", methods=["POST"])
    @admin
    def ai_preset_delete(preset_id):
        db.execute("DELETE FROM image_presets WHERE id = ?", (preset_id,))
        db.execute("UPDATE ai_drafts SET preset_id = NULL WHERE preset_id = ?", (preset_id,))
        flash("Preset removed.", "info")
        return _back()
