"""Rate limiting helpers for football-data.org API requests."""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_DELAY_SECONDS = 120  # free tier: ~10 requests/minute; 2 min is safe between leagues


def delay_seconds() -> int:
    raw = os.getenv("FOOTBALL_DATA_API_DELAY_SECONDS", str(DEFAULT_DELAY_SECONDS)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_DELAY_SECONDS


def wait_between_competition_requests(competition_name: str, *, is_first: bool) -> None:
    """Pause between per-competition API calls to avoid 429 rate limits."""
    if is_first:
        return
    seconds = delay_seconds()
    if seconds <= 0:
        return
    label = str(competition_name or "").strip() or "next competition"
    print(f"[football-data.org] waiting {seconds}s before {label}...")
    time.sleep(seconds)


def fetch_json(
    url: str,
    headers: dict | None = None,
    *,
    timeout: int = 45,
    competition_name: str = "",
) -> dict[str, Any]:
    """Fetch JSON from football-data.org with one retry after rate-limit delays."""
    request = urllib.request.Request(url, headers=headers or {})
    attempts = 2
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                import json

                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt + 1 < attempts:
                seconds = delay_seconds()
                label = competition_name or "request"
                print(
                    f"[football-data.org] 429 Too Many Requests for {label}; "
                    f"waiting {seconds}s and retrying..."
                )
                time.sleep(seconds)
                continue
            raise
    return {}
