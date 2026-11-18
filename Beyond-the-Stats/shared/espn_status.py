"""ESPN event status helpers shared by the website and the pipelines.

ESPN reports postponed / cancelled / abandoned / suspended games with
``state = "post"`` and a 0-0 placeholder score. Treating ``state == "post"`` as
"finished" archived those as real 0-0 results (an extra game and a point for
both clubs in the tables, a fake draw in team form and accuracy stats).
"""
from __future__ import annotations

from typing import Any, Mapping

NOT_PLAYED_STATUS_WORDS = (
    "POSTPONED", "CANCELED", "CANCELLED", "ABANDONED", "SUSPENDED", "DELAYED", "FORFEIT",
)


def status_type_of(event_or_status: Mapping[str, Any] | None) -> dict:
    """Return the ESPN ``status.type`` dict from an event, competition or status."""
    obj = event_or_status or {}
    if "competitions" in obj:
        comps = obj.get("competitions") or []
        obj = (comps[0] or {}) if comps else {}
    if "status" in obj:
        obj = obj.get("status") or {}
    if "type" in obj and isinstance(obj.get("type"), Mapping):
        obj = obj.get("type") or {}
    return dict(obj)


def is_not_played_status(status_type: Mapping[str, Any] | None) -> bool:
    """True for postponed / cancelled / abandoned / suspended / delayed games."""
    st = status_type or {}
    text = " ".join(
        str(st.get(k) or "") for k in ("name", "description", "detail", "shortDetail")
    ).upper()
    return any(word in text for word in NOT_PLAYED_STATUS_WORDS)


def is_played_final(status_type: Mapping[str, Any] | None) -> bool:
    """True only for a game that was actually played to a result."""
    st = status_type or {}
    if is_not_played_status(st):
        return False
    state = str(st.get("state") or "").strip().lower()
    return bool(st.get("completed")) or state in {"post", "final"}
