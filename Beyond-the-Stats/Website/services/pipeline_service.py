"""
Pipeline execution, status tracking, timestamp persistence, and JSON row enrichment services.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd

import config

_last_pipeline_run: datetime | None = None


def _file_mtime_utc(path: str) -> str | None:
    """Return file modification time as ISO-8601 UTC string, or None."""
    try:
        ts = os.path.getmtime(path)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _load_pipeline_timestamps() -> dict:
    """Read last-refresh timestamps from last_refresh.json."""
    path = config.LAST_REFRESH_FILE
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_pipeline_timestamps(data: dict) -> None:
    """Persist pipeline timestamps to last_refresh.json."""
    path = config.LAST_REFRESH_FILE
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


def get_last_pipeline_run() -> datetime | None:
    """Return the timestamp of the most recent pipeline run."""
    global _last_pipeline_run
    if _last_pipeline_run is not None:
        return _last_pipeline_run

    ts_data = _load_pipeline_timestamps()
    raw = ts_data.get("last_refresh_utc") or ts_data.get("timestamp")
    if raw:
        try:
            _last_pipeline_run = datetime.fromisoformat(str(raw))
            return _last_pipeline_run
        except (ValueError, TypeError):
            pass

    for candidate in (
        config.GLOBAL_UPCOMING_FILE,
        config.MLS_UPCOMING_FILE,
        config.EXTRA_UPCOMING_FILE,
    ):
        if candidate and os.path.exists(candidate):
            try:
                mtime = os.path.getmtime(candidate)
                _last_pipeline_run = datetime.fromtimestamp(mtime, tz=timezone.utc)
                return _last_pipeline_run
            except Exception:
                pass
    return None


def set_last_pipeline_run(dt: datetime | None = None) -> None:
    """Record a new pipeline run timestamp."""
    global _last_pipeline_run
    _last_pipeline_run = dt or datetime.now(timezone.utc)
    ts_data = _load_pipeline_timestamps()
    ts_data["last_refresh_utc"] = _last_pipeline_run.isoformat()
    ts_data["last_refresh_et"] = _last_pipeline_run.astimezone(
        ZoneInfo("America/New_York")
    ).isoformat()
    _save_pipeline_timestamps(ts_data)


def _run_full_pipeline_once(full_retrain: bool = False) -> bool:
    """Execute the full prediction pipeline synchronously in a subprocess."""
    pipeline_script = os.path.join(config.PROJECT_DIR, "main", "Run_All_Pipeline.py")
    if not os.path.exists(pipeline_script):
        return False
    cmd = [sys.executable, pipeline_script]
    if full_retrain:
        cmd.append("--force-retrain")
    try:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        res = subprocess.run(
            cmd,
            cwd=config.PROJECT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if res.returncode == 0:
            set_last_pipeline_run()
            return True
        return False
    except Exception:
        return False
