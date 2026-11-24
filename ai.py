#!/usr/bin/env python3
"""
AI providers for Tweppy: text and image generation over plain HTTPS.

Two providers are supported, OpenAI and Google Gemini. Either one can write
posts and either one can make images; which is used is chosen in the AI Studio
tab. Keys come from the environment only and are never stored in the database:

    OPENAI_API_KEY      https://platform.openai.com/api-keys
    GEMINI_API_KEY      https://aistudio.google.com/apikey  (GOOGLE_API_KEY also works)

Model IDs change often, so they are settings, not constants. The defaults below
are only starting points; "Check connection" in AI Studio lists the models your
key can actually use.
"""
import base64
import io
import json
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()

OPENAI_BASE = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

PROVIDERS = ("openai", "gemini")
PROVIDER_LABELS = {"openai": "OpenAI", "gemini": "Google Gemini"}

DEFAULT_MODELS = {
    ("openai", "text"): "gpt-5-mini",
    ("openai", "image"): "gpt-image-1",
    ("gemini", "text"): "gemini-2.5-flash",
    ("gemini", "image"): "gemini-3.1-flash-image",
}

# aspect -> (OpenAI size, Gemini aspect ratio)
ASPECTS = {
    "square": ("1024x1024", "1:1"),
    "landscape": ("1536x1024", "3:2"),
    "portrait": ("1024x1536", "2:3"),
}

TEXT_TIMEOUT = (10, 120)
IMAGE_TIMEOUT = (10, 240)

# X accepts images up to 5 MB. Anything bigger than this is re-encoded as JPEG,
# which also keeps the copy stored in the database small.
RECOMPRESS_ABOVE = 600_000


class AIError(Exception):
    """A provider call failed. The message is safe to show in the dashboard."""


def has_key(provider):
    return bool(OPENAI_API_KEY if provider == "openai" else GEMINI_API_KEY)


def available():
    return {provider: has_key(provider) for provider in PROVIDERS}


def resolve_provider(preferred):
    """The preferred provider if it has a key, else the other one, else an error."""
    if preferred in PROVIDERS and has_key(preferred):
        return preferred
    for provider in PROVIDERS:
        if has_key(provider):
            return provider
    raise AIError("No AI key is set. Add OPENAI_API_KEY or GEMINI_API_KEY to the environment and restart.")


def default_model(provider, kind):
    return DEFAULT_MODELS[(provider, kind)]


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

def _headers(provider):
    if provider == "openai":
        return {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    return {"x-goog-api-key": GEMINI_API_KEY}


def _error_text(response):
    try:
        error = response.json().get("error")
    except ValueError:
        error = None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return (response.text or "")[:300]


def _call(provider, method, url, timeout, **kwargs):
    label = PROVIDER_LABELS[provider]
    if not has_key(provider):
        raise AIError(f"No {label} API key is set.")
    try:
        response = requests.request(method, url, headers=_headers(provider), timeout=timeout, **kwargs)
    except requests.exceptions.RequestException as e:
        raise AIError(f"Could not reach {label}: {type(e).__name__}")
    if response.status_code >= 400:
        detail = _error_text(response)
        if response.status_code in (401, 403):
            raise AIError(f"{label} rejected the API key ({response.status_code}): {detail}")
        if response.status_code == 404:
            raise AIError(f"{label} does not know that model (404): {detail}")
        if response.status_code == 429:
            raise AIError(f"{label} rate limit or quota reached (429): {detail}")
        raise AIError(f"{label} returned HTTP {response.status_code}: {detail}")
    try:
        return response.json()
    except ValueError:
        raise AIError(f"{label} returned a response that was not JSON.")


def parse_json(text):
    """Pull a JSON object out of model output, tolerating code fences and chatter."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    raise AIError("The model did not return valid JSON. Try again, or pick a different text model.")


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def generate_json(provider, model, system, user):
    """Ask the text model for a JSON object. Returns the parsed dict."""
    model = model or default_model(provider, "text")
    if provider == "openai":
        data = _call(provider, "POST", f"{OPENAI_BASE}/chat/completions", TEXT_TIMEOUT, json={
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        })
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AIError("OpenAI returned no text.")
    else:
        data = _call(provider, "POST", f"{GEMINI_BASE}/models/{model}:generateContent", TEXT_TIMEOUT, json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        })
        text = "".join(part.get("text", "") for part in _gemini_parts(data))
        if not text:
            raise AIError(f"Gemini returned no text.{_gemini_block_reason(data)}")
    return parse_json(text)


def _gemini_parts(data):
    try:
        return data["candidates"][0]["content"]["parts"] or []
    except (KeyError, IndexError, TypeError):
        return []


def _gemini_block_reason(data):
    reason = (data.get("promptFeedback") or {}).get("blockReason")
    if not reason:
        try:
            reason = data["candidates"][0].get("finishReason")
        except (KeyError, IndexError, TypeError):
            reason = None
    return f" Reason: {reason}." if reason and reason != "STOP" else ""


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def generate_image(provider, model, prompt, aspect="square", quality="medium"):
    """Make one image. Returns (bytes, mime), re-encoded to JPEG when large."""
    model = model or default_model(provider, "image")
    size, ratio = ASPECTS.get(aspect, ASPECTS["square"])
    if provider == "openai":
        payload = {"model": model, "prompt": prompt, "n": 1, "size": size}
        if model.startswith("dall-e"):
            payload["response_format"] = "b64_json"
            if model == "dall-e-3":
                payload["size"] = {"1536x1024": "1792x1024", "1024x1536": "1024x1792"}.get(size, size)
        elif quality in ("low", "medium", "high"):
            payload["quality"] = quality
        data = _call(provider, "POST", f"{OPENAI_BASE}/images/generations", IMAGE_TIMEOUT, json=payload)
        try:
            item = data["data"][0]
        except (KeyError, IndexError, TypeError):
            raise AIError("OpenAI returned no image.")
        if item.get("b64_json"):
            raw, mime = base64.b64decode(item["b64_json"]), "image/png"
        elif item.get("url"):
            try:
                fetched = requests.get(item["url"], timeout=60)
                fetched.raise_for_status()
            except requests.exceptions.RequestException:
                raise AIError("OpenAI made the image, but it could not be downloaded.")
            raw, mime = fetched.content, fetched.headers.get("Content-Type", "image/png").split(";")[0]
        else:
            raise AIError("OpenAI returned no image.")
    else:
        url = f"{GEMINI_BASE}/models/{model}:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": ratio}},
        }
        try:
            data = _call(provider, "POST", url, IMAGE_TIMEOUT, json=body)
        except AIError as e:
            # Older image models reject imageConfig; retry once without it.
            if "HTTP 400" not in str(e):
                raise
            del body["generationConfig"]["imageConfig"]
            data = _call(provider, "POST", url, IMAGE_TIMEOUT, json=body)
        inline = None
        for part in _gemini_parts(data):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                break
            inline = None
        if not inline:
            raise AIError(f"Gemini returned no image.{_gemini_block_reason(data)}")
        raw = base64.b64decode(inline["data"])
        mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
    return shrink(raw, mime)


def shrink(raw, mime):
    """Re-encode a large image as JPEG. Returns the input unchanged if Pillow is missing."""
    if len(raw) <= RECOMPRESS_ABOVE:
        return raw, mime
    try:
        from PIL import Image
    except ImportError:
        return raw, mime
    try:
        image = Image.open(io.BytesIO(raw))
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            flat = Image.new("RGB", image.size, (255, 255, 255))
            flat.paste(image, mask=image.split()[-1])
            image = flat
        else:
            image = image.convert("RGB")
        image.thumbnail((2048, 2048))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=88, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return raw, mime


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_MAGIC = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF8": "image/gif", b"RIFF": "image/webp"}


def prepare_upload(raw):
    """Check that uploaded bytes are an image X accepts. Returns (bytes, mime)."""
    if not raw:
        raise AIError("Choose an image file first.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AIError("That image is larger than 10 MB.")
    mime = next((m for magic, m in _MAGIC.items() if raw.startswith(magic)), None)
    if mime is None or (mime == "image/webp" and raw[8:12] != b"WEBP"):
        raise AIError("Only JPEG, PNG, WebP and GIF images can be attached.")
    if mime == "image/gif":
        if len(raw) > 5 * 1024 * 1024:
            raise AIError("GIFs must be 5 MB or smaller.")
        return raw, mime           # re-encoding would drop the animation
    raw, mime = shrink(raw, mime)
    if len(raw) > 5 * 1024 * 1024:
        raise AIError("That image is over X's 5 MB limit and could not be made smaller.")
    return raw, mime


# ---------------------------------------------------------------------------
# Connection check
# ---------------------------------------------------------------------------

def list_models(provider):
    """Model IDs this key can use, as {"text": [...], "image": [...]}."""
    if provider == "openai":
        data = _call(provider, "GET", f"{OPENAI_BASE}/models", (10, 30))
        ids = sorted(item.get("id", "") for item in data.get("data", []))
        image = [i for i in ids if "image" in i or i.startswith("dall-e")]
        skip = ("embedding", "whisper", "tts", "audio", "realtime", "moderation", "transcribe", "sora", "image", "dall-e")
        text = [i for i in ids if i.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")) and not any(s in i for s in skip)]
        return {"text": text, "image": image}
    ids, token = [], None
    for _ in range(5):
        params = {"pageSize": 200}
        if token:
            params["pageToken"] = token
        data = _call(provider, "GET", f"{GEMINI_BASE}/models", (10, 30), params=params)
        for item in data.get("models", []):
            if "generateContent" in (item.get("supportedGenerationMethods") or []):
                ids.append(item.get("name", "").replace("models/", ""))
        token = data.get("nextPageToken")
        if not token:
            break
    ids.sort()
    image = [i for i in ids if "image" in i]
    skip = ("image", "tts", "audio", "embedding", "live", "robotics", "computer-use")
    text = [i for i in ids if i.startswith("gemini") and not any(s in i for s in skip)]
    return {"text": text, "image": image}
