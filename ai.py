"""
AI providers for Tweppy: text and images from OpenAI, images and video from
ImagineArt.

Everything here is a plain HTTPS call with `requests`, so there is no SDK to
install. Keys come from the environment:

    OPENAI_API_KEY       text drafts, reply drafts, and images
    IMAGINEART_API_KEY   images and video (optional)

Model names change often, so none are fixed in code: the defaults below can be
overridden in the dashboard's Settings tab or with the *_MODEL variables.
"""
import base64
import json
import os
import re
import time

import requests
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
IMAGINEART_API_KEY = os.getenv("IMAGINEART_API_KEY", "").strip()
IMAGINEART_BASE_URL = os.getenv("IMAGINEART_BASE_URL", "https://api.vyro.ai/v2").rstrip("/")

DEFAULT_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-6.2-astra")
DEFAULT_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-flare")
DEFAULT_IMAGINEART_IMAGE_STYLE = os.getenv("IMAGINEART_IMAGE_STYLE", "realistic")
DEFAULT_IMAGINEART_VIDEO_STYLE = os.getenv("IMAGINEART_VIDEO_STYLE", "kling-1.0-pro")

TWEET_MAX_CHARS = 280

# aspect name -> (OpenAI size, ImagineArt aspect_ratio)
ASPECTS = {
    "square": ("1024x1024", "1:1"),
    "landscape": ("1536x1024", "16:9"),
    "portrait": ("1024x1536", "9:16"),
}

VIDEO_POLL_SECONDS = 10
VIDEO_TIMEOUT_SECONDS = 15 * 60


class AIError(Exception):
    """A provider problem, already worded for the dashboard."""


def openai_ready():
    return bool(OPENAI_API_KEY)


def imagineart_ready():
    return bool(IMAGINEART_API_KEY)


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

def _explain(provider, response):
    try:
        data = response.json()
    except ValueError:
        data = {}
    detail = ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            detail = error.get("message", "")
        elif isinstance(error, str):
            detail = error
        detail = detail or data.get("message") or data.get("detail") or ""
    detail = str(detail or response.text or "")[:300]
    status = response.status_code
    if status == 401:
        return f"{provider} rejected the API key."
    if status == 402:
        return f"{provider} says the account is out of credits."
    if status == 404 and "model" in detail.lower():
        return f"{provider} does not know that model. Pick another one in Settings. ({detail})"
    if status == 429:
        return f"{provider} rate limit or quota reached. ({detail})"
    return f"{provider} returned HTTP {status}: {detail}"


def _openai(method, path, **kwargs):
    if not OPENAI_API_KEY:
        raise AIError("OPENAI_API_KEY is not set.")
    try:
        response = requests.request(
            method, OPENAI_BASE_URL + path,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            timeout=kwargs.pop("timeout", 120), **kwargs,
        )
    except requests.RequestException as e:
        raise AIError(f"Could not reach OpenAI: {e}")
    if response.status_code != 200:
        raise AIError(_explain("OpenAI", response))
    return response.json()


def list_openai_models():
    """IDs of the models this key can use, newest first."""
    data = _openai("GET", "/models", timeout=30).get("data", [])
    data.sort(key=lambda m: m.get("created", 0), reverse=True)
    return [m["id"] for m in data if m.get("id")]


def _response_text(data):
    """Pull the text out of a Responses API result."""
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]
    parts = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") in ("output_text", "text") and content.get("text"):
                parts.append(content["text"])
    return "".join(parts)


def generate_text(instructions, prompt, model=None):
    data = _openai("POST", "/responses", json={
        "model": model or DEFAULT_TEXT_MODEL,
        "instructions": instructions,
        "input": prompt,
    })
    text = _response_text(data).strip()
    if not text:
        raise AIError("OpenAI returned an empty answer.")
    return text


def _parse_list(text):
    """Read a JSON array of strings out of a model answer, tolerating wrappers."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            items = json.loads(match.group(0))
            if isinstance(items, list):
                return [str(i).strip() for i in items if str(i).strip()]
        except ValueError:
            pass
    lines = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip().strip('"') for line in text.splitlines()]
    return [line for line in lines if line]


WRITER_RULES = (
    "You write posts for X (Twitter) on behalf of one person, in their voice. "
    f"Every post must be at most {TWEET_MAX_CHARS} characters. "
    "Write like a person, not a brand: no hashtags unless asked, no emoji unless the voice calls for it, "
    "no quotation marks around the post, and no links unless the brief includes one. "
    "Never invent facts, numbers, quotes, or events."
)


def compose_posts(brief, count=3, voice="", model=None):
    """Draft `count` different posts about `brief`. Returns a list of strings."""
    count = max(1, min(10, int(count)))
    instructions = WRITER_RULES
    if voice:
        instructions += f"\n\nThe person's voice and background:\n{voice}"
    prompt = (
        f"Write {count} different posts based on this brief. Vary the angle and opening of each.\n\n"
        f"Brief:\n{brief}\n\n"
        "Answer with only a JSON array of strings, one string per post."
    )
    posts = _parse_list(generate_text(instructions, prompt, model))
    if not posts:
        raise AIError("OpenAI did not return any posts.")
    return posts[:count]


def draft_reply(author, post_text, voice="", model=None):
    """Draft one reply to someone else's post."""
    instructions = WRITER_RULES + (
        " You are writing a reply to another person's post. Respond to what they actually said: "
        "add a thought, a question, or a specific reaction. No generic praise, no restating their post, "
        "and do not start with their handle."
    )
    if voice:
        instructions += f"\n\nThe person's voice and background:\n{voice}"
    prompt = f"Post by @{author}:\n{post_text}\n\nWrite one reply. Answer with only the reply text."
    reply = generate_text(instructions, prompt, model).strip().strip('"').strip()
    return reply[:TWEET_MAX_CHARS]


def openai_image(prompt, aspect="square", model=None):
    """Generate one image. Returns (bytes, mime)."""
    size = ASPECTS.get(aspect, ASPECTS["square"])[0]
    data = _openai("POST", "/images/generations", timeout=300, json={
        "model": model or DEFAULT_IMAGE_MODEL,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "output_format": "jpeg",
    })
    item = (data.get("data") or [{}])[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), "image/jpeg"
    if item.get("url"):
        return _download(item["url"])
    raise AIError("OpenAI did not return an image.")


# ---------------------------------------------------------------------------
# ImagineArt (api.vyro.ai). Its API also hosts third-party video models such as
# Kling, selected with the "style" field.
# ---------------------------------------------------------------------------

def _download(url):
    try:
        response = requests.get(url, timeout=300)
    except requests.RequestException as e:
        raise AIError(f"Could not download the generated file: {e}")
    if response.status_code != 200:
        raise AIError(f"Could not download the generated file (HTTP {response.status_code}).")
    mime = response.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
    return response.content, mime


def _imagineart(method, path, **kwargs):
    if not IMAGINEART_API_KEY:
        raise AIError("IMAGINEART_API_KEY is not set.")
    try:
        response = requests.request(
            method, IMAGINEART_BASE_URL + path,
            headers={"Authorization": f"Bearer {IMAGINEART_API_KEY}"},
            timeout=kwargs.pop("timeout", 300), **kwargs,
        )
    except requests.RequestException as e:
        raise AIError(f"Could not reach ImagineArt: {e}")
    if response.status_code not in (200, 201, 202):
        raise AIError(_explain("ImagineArt", response))
    return response


def _find(data, *keys):
    """First value stored under any of `keys`, searching nested dicts and lists."""
    stack = [data]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            for key in keys:
                if node.get(key):
                    return node[key]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _form(fields):
    """multipart/form-data fields, as the ImagineArt API expects."""
    return {key: (None, str(value)) for key, value in fields.items() if value not in (None, "")}


def _imagineart_result(response, what):
    """Turn a generation response into (bytes, mime), waiting for async jobs."""
    mime = response.headers.get("Content-Type", "").split(";")[0].strip()
    if mime.startswith(("image/", "video/")):
        return response.content, mime
    try:
        data = response.json()
    except ValueError:
        raise AIError(f"ImagineArt returned an unexpected answer for the {what}.")

    deadline = time.time() + VIDEO_TIMEOUT_SECONDS
    while True:
        status = str(_find(data, "status", "state") or "").lower()
        url = _find(data, "url", "video_url", "image_url", "output_url", "asset_url")
        if isinstance(url, str) and url.startswith("http") and status not in ("pending", "processing", "queued"):
            return _download(url)
        if status in ("failed", "error", "cancelled", "canceled"):
            raise AIError(f"ImagineArt could not make the {what}: {_find(data, 'error', 'message') or status}")
        job_id = _find(data, "id", "asset_id", "task_id")
        if not job_id:
            raise AIError(f"ImagineArt did not return a {what} or a job to wait for.")
        if time.time() > deadline:
            raise AIError(f"ImagineArt took too long to make the {what}.")
        time.sleep(VIDEO_POLL_SECONDS)
        data = _imagineart("GET", f"/assets/{job_id}/status", timeout=60).json()
        data.setdefault("id", job_id)


def imagineart_image(prompt, aspect="square", style=None):
    """Generate one image. Returns (bytes, mime)."""
    response = _imagineart("POST", "/image/generations", files=_form({
        "prompt": prompt,
        "style": style or DEFAULT_IMAGINEART_IMAGE_STYLE,
        "aspect_ratio": ASPECTS.get(aspect, ASPECTS["square"])[1],
    }))
    return _imagineart_result(response, "image")


def imagineart_video(prompt, style=None, image=None, aspect="landscape"):
    """Generate one video, from text or from a starting image. Returns (bytes, mime).

    image: optional (bytes, mime) to animate. Blocks until the video is ready,
    which can take several minutes, so call it from a background thread.
    """
    fields = _form({
        "prompt": prompt,
        "style": style or DEFAULT_IMAGINEART_VIDEO_STYLE,
        "aspect_ratio": ASPECTS.get(aspect, ASPECTS["landscape"])[1],
    })
    if image:
        data, mime = image
        fields["file"] = ("image." + mime.split("/")[-1], data, mime)
        response = _imagineart("POST", "/video/image-to-video", files=fields)
    else:
        response = _imagineart("POST", "/video/text-to-video", files=fields)
    data, mime = _imagineart_result(response, "video")
    if not mime.startswith("video/"):
        mime = "video/mp4"
    return data, mime
