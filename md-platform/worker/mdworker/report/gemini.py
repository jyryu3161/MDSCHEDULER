"""Minimal Gemini REST client for report narration (gemini-3.5-flash by default).

Uses only the stdlib (urllib) so the worker gains no new dependency. Reads GEMINI_API_KEY +
GEMINI_MODEL from the provided settings or the process environment. Gemini 3.x has *thinking*
on by default and it consumes output tokens, so we request thinkingLevel=low and a generous
maxOutputTokens, and ask for a JSON object back (responseMimeType=application/json). Every
failure path returns None so the caller falls back to a deterministic template — report
generation, and the job, never fail because of the LLM.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
             "{model}:generateContent?key={key}")
_DEFAULT_MODEL = "gemini-3.5-flash"


def _setting(settings: Any, attr: str, env: str) -> str:
    """Read a value from a Settings object, a dict, or the environment (in that order)."""
    if settings is not None:
        if hasattr(settings, attr):
            v = getattr(settings, attr)
            if v:
                return str(v)
        if isinstance(settings, dict):
            v = settings.get(env) or settings.get(attr)
            if v:
                return str(v)
    return os.environ.get(env, "")


def api_key(settings: Any = None) -> str:
    return _setting(settings, "gemini_api_key", "GEMINI_API_KEY")


def model(settings: Any = None) -> str:
    return _setting(settings, "gemini_model", "GEMINI_MODEL") or _DEFAULT_MODEL


def available(settings: Any = None) -> bool:
    return bool(api_key(settings))


def generate_json(prompt: str, *, settings: Any = None, max_output_tokens: int = 8192,
                  temperature: float = 0.3, timeout: float = 120.0) -> Optional[Dict[str, Any]]:
    """POST ``prompt`` and parse a JSON object from the response. Returns None on any failure.

    thinkingLevel=low keeps latency/cost down; maxOutputTokens is sized for thinking + the JSON
    payload (Gemini 3 thinking spends output tokens, so a small budget yields empty text)."""
    key = api_key(settings)
    if not key:
        return None
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "low"},
            "maxOutputTokens": max_output_tokens,
            "temperature": temperature,
            "responseMimeType": "application/json",
        },
    }
    url = _ENDPOINT.format(model=model(settings), key=key)
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    try:
        cand = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        if not text.strip():
            return None
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None
