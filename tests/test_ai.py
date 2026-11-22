"""
Tests for AI Studio. No network: the AI providers and X are replaced with fakes.

Run with:  python -m pytest tests
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Point the app at a throwaway SQLite file before anything imports db.
for name in ("CF_ACCOUNT_ID", "CF_D1_DATABASE_ID", "CF_API_TOKEN", "ADMIN_TOKEN"):
    os.environ[name] = ""
os.environ["LOCAL_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import ai  # noqa: E402
import aistudio  # noqa: E402
import db  # noqa: E402
import webapp  # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n" + b"0" * 64)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for table in ("ai_drafts", "image_presets", "media", "media_chunks", "tweet_queue",
                  "scheduled_posts", "post_history", "post_failures", "settings", "bot_stats"):
        db.execute(f"DELETE FROM {table}")
    db._load_settings_cache()
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ai, "GEMINI_API_KEY", "")
    yield


def fake_text(posts, calls=None):
    def _generate(provider, model, system, user):
        if calls is not None:
            calls.append({"provider": provider, "system": system, "user": user})
        return {"posts": posts}
    return _generate


def drafts():
    return db.query("SELECT * FROM ai_drafts ORDER BY id ASC")


def test_x_length_counts_links_and_emoji():
    assert aistudio.x_length("hello") == 5
    assert aistudio.x_length("see https://example.com/a/very/long/path/that/goes/on") == 4 + 23
    assert aistudio.x_length("hi 🎉") == 5


def test_parse_json_tolerates_fences_and_chatter():
    assert ai.parse_json('```json\n{"posts": []}\n```') == {"posts": []}
    assert ai.parse_json('Sure! {"posts": [1]} hope that helps') == {"posts": [1]}
    with pytest.raises(ai.AIError):
        ai.parse_json("no json here")


def test_format_modes(monkeypatch):
    assert aistudio.pick_formats(4, "text_only") == ["text"] * 4
    assert aistudio.pick_formats(4, "image_only") == ["image"] * 4
    assert aistudio.pick_formats(4, "auto") == ["auto"] * 4
    db.set_setting("ai_image_ratio", "50")
    assert sorted(aistudio.pick_formats(4, "mixed")) == ["image", "image", "text", "text"]
    db.set_setting("ai_image_ratio", "0")
    assert aistudio.pick_formats(4, "mixed") == ["text"] * 4


def test_compose_applies_preferences_and_stores_drafts(monkeypatch):
    db.set_setting("ai_persona", "A coffee roaster")
    db.set_setting("ai_avoid", "politics")
    db.set_setting("ai_format_mode", "text_only")
    calls = []
    monkeypatch.setattr(ai, "generate_json", fake_text([
        {"text": "First post", "image": True, "image_prompt": "a cup", "time": "10:00", "reason": "morning"},
        {"text": "[Second] post", "time": "25:99"},
        {"text": "x" * 400},
        {"text": "First post"},
    ], calls))

    made = aistudio.compose(4)

    assert made == 2                      # over-length and duplicate posts are dropped
    rows = drafts()
    assert {r["text"] for r in rows} == {"First post", "(Second) post"}
    assert all(r["wants_image"] == 0 and r["image_status"] == "none" for r in rows)   # text_only wins
    assert all(r["suggested_at"] for r in rows)
    assert "A coffee roaster" in calls[0]["system"] and "politics" in calls[0]["system"]


def test_times_stay_inside_window_and_apart(monkeypatch):
    tz = webapp.user_tz()
    day = (datetime.now(tz) + timedelta(days=1)).date()
    start, end = aistudio._window(day, tz)
    posts = [{"time": "09:00"}, {"time": "09:05"}, {"time": "03:00"}, {"time": "nonsense"}]
    ordered = aistudio.assign_times(posts, start, end, use_model_times=True)
    times = [p["when"] for p in ordered]
    assert times == sorted(times)
    assert all(start <= t <= end + timedelta(hours=1) for t in times)
    assert all(b - a >= aistudio.MIN_GAP for a, b in zip(times, times[1:]))
    assert times[0].hour == 9 and times[0].minute == 0


def test_plan_target_rolls_to_tomorrow_after_the_window():
    db.set_setting("ai_window_start", "08:00")
    db.set_setting("ai_window_end", "21:00")
    late = datetime(2026, 5, 1, 22, 0, tzinfo=timezone.utc)
    day, earliest, end, fraction = aistudio.plan_target(late)
    assert day.isoformat() == "2026-05-02" and fraction == 1.0
    midday = datetime(2026, 5, 1, 14, 30, tzinfo=timezone.utc)
    day, earliest, end, fraction = aistudio.plan_target(midday)
    assert day.isoformat() == "2026-05-01" and 0.4 < fraction < 0.6


def test_image_draft_uses_preset_and_can_be_queued(monkeypatch):
    db.set_setting("ai_format_mode", "image_only")
    db.execute("INSERT INTO image_presets (name, category, prompt, enabled, created_at) VALUES (?, ?, ?, 1, ?)",
               ("Wildlife", "animals", "telephoto, golden hour", db.utcnow_str()))
    monkeypatch.setattr(ai, "generate_json", fake_text([{"text": "Look at this", "image_prompt": "a fox"}]))
    prompts = []

    def fake_image(provider, model, prompt, aspect, quality):
        prompts.append(prompt)
        return PNG, "image/png"
    monkeypatch.setattr(ai, "generate_image", fake_image)

    aistudio.compose(1, with_times=False)
    draft = drafts()[0]
    assert draft["image_status"] == "pending" and draft["preset_id"]
    assert aistudio.approve_draft(draft, "queue")[0] is False      # not while the image is pending

    assert aistudio.make_image(draft) is True
    assert "a fox" in prompts[0] and "telephoto" in prompts[0] and "animal" in prompts[0]
    draft = drafts()[0]
    assert draft["image_status"] == "ready"
    assert db.load_media(draft["media_id"]) == (PNG, "image/png")

    ok, _ = aistudio.approve_draft(draft, "queue")
    assert ok and not drafts()
    queued = db.query_one("SELECT text, media_id FROM tweet_queue")
    assert queued == {"text": "Look at this", "media_id": draft["media_id"]}


def test_failed_image_blocks_approval(monkeypatch):
    db.set_setting("ai_format_mode", "image_only")
    monkeypatch.setattr(ai, "generate_json", fake_text([{"text": "Needs a picture"}]))

    def broken(*args):
        raise ai.AIError("quota reached")
    monkeypatch.setattr(ai, "generate_image", broken)

    aistudio.compose(1, auto_approve=True)
    aistudio._tick()

    draft = drafts()[0]
    assert draft["image_status"] == "failed" and "quota" in draft["image_error"]
    assert db.query_one("SELECT COUNT(*) AS n FROM scheduled_posts")["n"] == 0


def test_autopilot_plans_once_a_day_and_auto_schedules(monkeypatch):
    db.set_setting("ai_autopilot_enabled", "1")
    db.set_setting("ai_require_approval", "0")
    db.set_setting("ai_format_mode", "text_only")
    db.set_setting("ai_posts_per_day", "2")
    db.set_setting("ai_window_start", "00:00")
    db.set_setting("ai_window_end", "00:00")        # whole day
    calls = []
    counter = iter(range(100))
    monkeypatch.setattr(ai, "generate_json", lambda *a: (calls.append(1), {"posts": [
        {"text": f"Autopilot post {next(counter)}"}, {"text": f"Autopilot post {next(counter)}"}]})[1])
    aistudio._state["retry_after"] = None

    aistudio._tick()
    aistudio._tick()

    assert len(calls) == 1                           # planned once, not on every tick
    assert not drafts()
    scheduled = db.query("SELECT text, scheduled_at FROM scheduled_posts")
    assert 1 <= len(scheduled) <= 2
    assert all(db.parse_utc(r["scheduled_at"]) > datetime.now(timezone.utc) for r in scheduled)


def test_autopilot_with_review_keeps_drafts(monkeypatch):
    db.set_setting("ai_autopilot_enabled", "1")
    db.set_setting("ai_format_mode", "text_only")
    monkeypatch.setattr(ai, "generate_json", fake_text([{"text": "Please review me"}]))
    aistudio._state["retry_after"] = None

    aistudio._tick()

    assert [d["origin"] for d in drafts()] == ["autopilot"]
    assert db.query_one("SELECT COUNT(*) AS n FROM scheduled_posts")["n"] == 0


def test_autopilot_backs_off_after_an_error(monkeypatch):
    db.set_setting("ai_autopilot_enabled", "1")
    calls = []

    def broken(*args):
        calls.append(1)
        raise ai.AIError("OpenAI rejected the API key (401)")
    monkeypatch.setattr(ai, "generate_json", broken)
    aistudio._state["retry_after"] = None

    aistudio._tick()
    aistudio._tick()

    assert len(calls) == 1
    assert "rejected the API key" in db.get_setting("ai_last_error")
    aistudio._state["retry_after"] = None


def test_posting_with_an_image_uploads_then_attaches(monkeypatch):
    media_id = db.save_media(PNG, "image/png")
    sent = []

    class Reply:
        def __init__(self, status, body):
            self.status_code, self._body, self.text = status, body, str(body)

        def json(self):
            return self._body

    def fake_post(url, **kwargs):
        sent.append((url, kwargs))
        if url == webapp.X_MEDIA_UPLOAD_URL:
            return Reply(200, {"data": {"id": "999"}})
        return Reply(201, {"data": {"id": "1"}})
    monkeypatch.setattr(webapp, "get_valid_token", lambda *a, **k: "token")
    monkeypatch.setattr(webapp.requests, "post", fake_post)

    ok, _ = webapp.publish_profile_item({"id": None, "text": "With picture", "media_id": media_id})

    assert ok
    assert sent[0][0] == webapp.X_MEDIA_UPLOAD_URL and sent[0][1]["files"]["media"][1] == PNG
    assert sent[1][1]["json"] == {"text": "With picture", "media": {"media_ids": ["999"]}}
    assert db.load_media(media_id) is None                       # image is dropped once posted
    assert db.query_one("SELECT has_media FROM post_history")["has_media"] == 1


def test_orphan_images_are_swept():
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(db.TIMESTAMP_FORMAT)
    kept = db.save_media(PNG, "image/png")
    orphan = db.save_media(PNG, "image/png")
    db.execute("UPDATE media SET created_at = ?", (old,))
    db.execute("INSERT INTO tweet_queue (text, created_at, media_id) VALUES ('q', ?, ?)", (db.utcnow_str(), kept))
    assert aistudio.sweep_orphan_media() == 1
    assert db.load_media(kept) and db.load_media(orphan) is None


def test_pages_and_routes_render(monkeypatch):
    monkeypatch.setattr(ai, "generate_json", fake_text([{"text": "From the form"}]))
    db.set_setting("ai_format_mode", "text_only")
    client = webapp.app.test_client()

    assert client.post("/ai/presets/save", data={"name": "", "category": "places", "prompt": ""}).status_code == 302
    assert db.query_one("SELECT name FROM image_presets")["name"] == "Places"
    assert client.post("/ai/presets/save", data={"name": "x"}).status_code == 302      # needs category or prompt
    assert db.query_one("SELECT COUNT(*) AS n FROM image_presets")["n"] == 1

    assert client.post("/ai/settings", data={
        "ai_text_provider": "gemini", "ai_image_provider": "openai", "ai_image_quality": "low",
        "ai_image_aspect": "landscape", "ai_tone": "witty", "ai_hashtags": "few", "ai_emoji": "none",
        "ai_format_mode": "auto", "ai_timing_mode": "even", "ai_max_chars": "200", "ai_image_ratio": "40",
        "ai_posts_per_day": "5", "ai_text_model": "", "ai_image_model": "gpt-image-1",
        "ai_window_start": "09:00", "ai_window_end": "18:00", "ai_language": "French",
    }).status_code == 302
    assert db.get_setting("ai_tone") == "witty" and db.get_setting("ai_posts_per_day") == "5"
    assert db.get_setting("ai_require_approval") == "0"

    client.post("/ai/compose", data={"count": "1", "brief": "hello"})
    aistudio._tick()
    page = client.get("/")
    assert page.status_code == 200 and b"From the form" in page.data and b"AI Studio" in page.data
    assert client.get("/api/ai/status").get_json()["drafts"] == 1
