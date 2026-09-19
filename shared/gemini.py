"""Gemini Live (§8 coding round): mint short-lived ephemeral tokens so the browser
can open a realtime Live session (screen-watch + voice) without ever seeing the
real API key. If no key is configured the coding round falls back to snapshot
vision, so this module is entirely optional.
"""
import logging
import time

import requests

from shared.config import get_settings

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://generativelanguage.googleapis.com/v1alpha/auth_tokens"
# Google's ephemeral-token service (v1alpha, still preview) intermittently 503s.
# Retry a few times with backoff so one transient hiccup doesn't kill the round.
_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


def enabled() -> bool:
    return bool(get_settings().gemini_api_key)


def mint_ephemeral_token(uses: int = 2) -> str:
    """Create an ephemeral auth token for client-side Live use; returns the token
    string the browser passes as its apiKey (v1alpha). Retries transient 5xx/429,
    then raises on persistent failure."""
    s = get_settings()
    if not s.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    last_err: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.post(f"{_TOKEN_URL}?key={s.gemini_api_key}",
                                 json={"uses": uses}, timeout=15)
            if resp.status_code in _RETRYABLE:
                raise requests.HTTPError(f"{resp.status_code} {resp.reason}")
            resp.raise_for_status()
            name = resp.json().get("name", "")
            if not name:
                raise RuntimeError("no token returned")
            return name
        except Exception as e:
            last_err = e
            if attempt < _MAX_ATTEMPTS - 1:
                backoff = 0.6 * (2 ** attempt)   # 0.6, 1.2, 2.4s
                logger.warning("gemini token mint attempt %d failed (%s); retrying in %.1fs",
                               attempt + 1, e, backoff)
                time.sleep(backoff)
    raise RuntimeError(f"gemini token mint failed after {_MAX_ATTEMPTS} attempts: {last_err}")
