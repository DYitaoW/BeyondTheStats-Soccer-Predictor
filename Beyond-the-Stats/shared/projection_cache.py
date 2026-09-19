"""Projection skip stamps + daily matchup-probability cache.

Gates are **off by default** so full refreshes keep running until you opt in:

- ``BTS_SKIP_UNCHANGED_PROJECTIONS=1`` — reuse league table rows when the
  season CSV signature is unchanged.
- ``BTS_CUP_SKIP_UNCHANGED=1`` — skip cup table/bracket rebuild when the
  upcoming/completed fixture signature is unchanged.

Daily matchup probs are always available under ``Output/Cache/`` (same UTC day).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from shared import paths as _paths
except Exception:  # pragma: no cover - script bootstrap fallback
    _paths = None  # type: ignore


def _status_dir() -> Path:
    if _paths is not None:
        return _paths.OUTPUT_STATUS_DIR
    return Path(__file__).resolve().parent.parent / "Output" / "Status"


def _cache_dir() -> Path:
    if _paths is not None:
        return _paths.OUTPUT_CACHE_DIR
    return Path(__file__).resolve().parent.parent / "Output" / "Cache"


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def skip_unchanged_projections_enabled() -> bool:
    return env_flag("BTS_SKIP_UNCHANGED_PROJECTIONS", default=False)


def cup_skip_unchanged_enabled() -> bool:
    return env_flag("BTS_CUP_SKIP_UNCHANGED", default=False)


def file_signature(path: str | Path | None) -> str:
    """Stable signature for a season CSV (mtime + size + quick content digest)."""
    if not path:
        return "missing"
    p = Path(path)
    if not p.is_file():
        return f"missing:{p}"
    try:
        st = p.stat()
        digest = hashlib.sha1()
        digest.update(str(st.st_mtime_ns).encode())
        digest.update(b":")
        digest.update(str(st.st_size).encode())
        # Sample head+tail so appended games change the signature cheaply.
        with p.open("rb") as fh:
            head = fh.read(65536)
            if st.st_size > 131072:
                fh.seek(max(0, st.st_size - 65536))
                tail = fh.read(65536)
            else:
                tail = b""
        digest.update(head)
        digest.update(tail)
        return digest.hexdigest()
    except Exception as exc:
        return f"error:{exc.__class__.__name__}"


def _load_json(path: Path) -> dict:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def league_stamp_path() -> Path:
    return _status_dir() / "league_projection_stamps.json"


def cup_stamp_path() -> Path:
    return _status_dir() / "cup_projection_stamps.json"


def get_league_stamp(competition: str) -> str | None:
    data = _load_json(league_stamp_path())
    entry = data.get(competition) or {}
    return entry.get("signature")


def set_league_stamp(competition: str, signature: str, extra: dict | None = None) -> None:
    path = league_stamp_path()
    data = _load_json(path)
    payload = {
        "signature": signature,
        "updated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    if extra:
        payload.update(extra)
    data[competition] = payload
    _save_json(path, data)


def cup_fixture_signature(upcoming_df, completed_df) -> str:
    """Signature from upcoming + completed cup fixture identity (not full odds)."""
    digest = hashlib.sha1()

    def _feed(df, label: str) -> None:
        digest.update(label.encode())
        if df is None or getattr(df, "empty", True):
            digest.update(b":empty")
            return
        cols = [c for c in ("competition", "match_date", "home_team", "away_team", "round", "stage") if c in df.columns]
        if not cols:
            digest.update(str(len(df)).encode())
            return
        try:
            subset = df.loc[:, cols].fillna("").astype(str)
            subset = subset.sort_values(cols)
            digest.update(str(len(subset)).encode())
            for row in subset.itertuples(index=False, name=None):
                digest.update(("|".join(row) + "\n").encode("utf-8", errors="replace"))
        except Exception:
            digest.update(str(len(df)).encode())

    _feed(upcoming_df, "upcoming")
    _feed(completed_df, "completed")
    return digest.hexdigest()


def get_cup_stamp() -> str | None:
    data = _load_json(cup_stamp_path())
    return data.get("signature")


def set_cup_stamp(signature: str, extra: dict | None = None) -> None:
    payload = {
        "signature": signature,
        "updated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    if extra:
        payload.update(extra)
    _save_json(cup_stamp_path(), payload)


# ── Daily matchup probability cache ───────────────────────────────

_MATCHUP_LOCK = threading.Lock()
_MATCHUP_MEM: dict[str, dict[str, dict[str, float]]] = {}
_MATCHUP_DAY: str | None = None


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _matchup_path(day: str | None = None) -> Path:
    return _cache_dir() / f"matchup_probs_{day or _utc_day()}.json"


def _matchup_key(home: str, away: str, competition: str) -> str:
    h = str(home or "").strip().lower()
    a = str(away or "").strip().lower()
    c = str(competition or "").strip().lower()
    return f"{c}|{h}|{a}"


def _ensure_matchup_day() -> str:
    global _MATCHUP_DAY, _MATCHUP_MEM
    day = _utc_day()
    with _MATCHUP_LOCK:
        if _MATCHUP_DAY != day:
            _MATCHUP_DAY = day
            _MATCHUP_MEM = _load_json(_matchup_path(day))
            if not isinstance(_MATCHUP_MEM, dict):
                _MATCHUP_MEM = {}
        return day


def get_matchup_probs(home: str, away: str, competition: str) -> dict[str, float] | None:
    _ensure_matchup_day()
    key = _matchup_key(home, away, competition)
    with _MATCHUP_LOCK:
        entry = _MATCHUP_MEM.get(key)
    if not isinstance(entry, dict):
        return None
    try:
        return {
            "H": float(entry.get("H", 0.0)),
            "D": float(entry.get("D", 0.0)),
            "A": float(entry.get("A", 0.0)),
        }
    except Exception:
        return None


def set_matchup_probs(home: str, away: str, competition: str, probs: dict[str, Any]) -> None:
    day = _ensure_matchup_day()
    key = _matchup_key(home, away, competition)
    payload = {
        "H": float(probs.get("H", 0.0) or 0.0),
        "D": float(probs.get("D", 0.0) or 0.0),
        "A": float(probs.get("A", 0.0) or 0.0),
    }
    with _MATCHUP_LOCK:
        _MATCHUP_MEM[key] = payload
        # Persist opportunistically every 25 writes to keep I/O light.
        if len(_MATCHUP_MEM) % 25 == 0:
            _save_json(_matchup_path(day), _MATCHUP_MEM)


def flush_matchup_probs() -> None:
    day = _ensure_matchup_day()
    with _MATCHUP_LOCK:
        _save_json(_matchup_path(day), _MATCHUP_MEM)
