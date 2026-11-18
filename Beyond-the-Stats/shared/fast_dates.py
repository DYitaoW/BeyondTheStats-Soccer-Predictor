"""Fast, memoized parsing of single datetime strings.

``pd.to_datetime(<one string>)`` guesses the format on every call (~100 us);
request paths called it once per row (tens of thousands of times for a cold
``/api/team`` or ``/api/past-games``). ISO-8601 strings, which is what the
store and pipelines write, are parsed with ``datetime.fromisoformat``; anything
else falls back to pandas. Results are memoized (kickoff strings repeat).
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@lru_cache(maxsize=131072)
def parse_utc(text: str) -> Optional[datetime]:
    """Parse ``text`` to an aware UTC datetime (naive = UTC, like ``utc=True``)."""
    raw = str(text or "").strip()
    if not raw or raw.lower() in {"nan", "none", "null", "nat"}:
        return None
    dt: Optional[datetime] = None
    try:
        dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
    except ValueError:
        try:
            import pandas as pd

            ts = pd.to_datetime(raw, utc=True, errors="coerce")
            if ts is not None and not pd.isna(ts):
                dt = ts.to_pydatetime()
        except Exception:
            dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def et_date_str(text: str) -> str:
    """ET calendar date ``YYYY-MM-DD`` of a timestamp string ('' if unparseable)."""
    dt = parse_utc(text)
    return dt.astimezone(ET).strftime("%Y-%m-%d") if dt else ""


def utc_iso_z(text: str) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ`` in UTC ('' if unparseable)."""
    dt = parse_utc(text)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""
