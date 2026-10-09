"""
jalnetra.ee_http — download Earth Engine thumbnail / download URLs with retry.

Earth Engine answers HTTP 429 (Too Many Requests) when several overlay PNGs
are fetched at once (e.g. POST /api/all running many APIs in parallel).
"""
from __future__ import annotations

import random
import time
import urllib.error
import urllib.request

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6
BASE_DELAY_S = 2.0
MAX_DELAY_S = 60.0


def read_url(url: str, timeout: float = 900) -> bytes:
    """GET `url` and return the body, retrying 429/5xx with exponential backoff."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt >= MAX_ATTEMPTS - 1:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = float(retry_after) if retry_after else 0.0
            except ValueError:
                delay = 0.0
            delay = max(delay, BASE_DELAY_S * (2**attempt)) + random.uniform(0, 1.0)
            time.sleep(min(delay, MAX_DELAY_S))
    raise RuntimeError("unreachable")
