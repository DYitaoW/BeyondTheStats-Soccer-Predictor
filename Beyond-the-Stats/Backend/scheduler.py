"""
Scheduling helpers for the backend.

Two schedules are exposed:

1. ``next_run_after(now, hour, minute, tz_name)`` -- compute the next
   datetime matching the daily refresh wall-clock (default 02:00 America/New_York,
   configurable). This is timezone-aware so DST transitions are handled
   correctly.

2. ``FutureGamesWatcher`` -- a polling thread that watches the upcoming-fixture
   CSVs in the project tree. When the file size / row count / mtime / first
   line of content changes (i.e. new fixtures landed in the source feed), it
   runs the upcoming-matchweek script to re-predict them and writes back the
   new CSV. The watcher only triggers light refreshes -- never a full daily
   pipeline -- so it stays cheap between daily refreshes.

The scheduler sleeps in 30-second ticks so :py:meth:`BackendServer.stop` can
wake it up and shut everything down quickly.
"""

from __future__ import annotations

import csv
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

LOG = logging.getLogger("backend.scheduler")


def _coerce_tz(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except Exception:
        LOG.warning("[scheduler] unknown tz %r; falling back to UTC", tz_name)
        return ZoneInfo("UTC")


def next_run_after(
    now: Optional[datetime] = None,
    hour: int = 2,
    minute: int = 0,
    tz_name: str = "America/New_York",
) -> datetime:
    """Return the next ``datetime`` at or after ``now`` matching HH:MM in ``tz_name``.

    If ``now`` is None, ``datetime.now(tz)`` is used. The returned datetime
    is timezone-aware.
    """
    tz = _coerce_tz(tz_name)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return target


def seconds_until(target: datetime) -> float:
    """Seconds from now until ``target``. Negative if past."""
    tz = target.tzinfo
    now = datetime.now(tz) if tz else datetime.now()
    return (target - now).total_seconds()


@dataclass
class _WatchedFile:
    path: Path
    signature: tuple


class FutureGamesWatcher:
    """Poll upcoming-fixture CSVs and trigger a light refresh on change.

    Args:
        project_root: Absolute path to the project root (where Beyond-the-Stats
            / MLS / Extra-leagues live).
        upcoming_paths: Explicit list of ``Path`` objects to watch. If empty,
            the default set is used (global/MLS/extra upcoming_matchweek,
            global upcoming_cups, global upcoming_national_team).
        poll_interval_s: Seconds between polls. Default 300 (5 min).
        refresh_callback: Called with the path that changed (or None on the
            first poll) when a refresh should be triggered. The callback should
            run the upcoming-matchweek script in a background process.
        bootstrap: If True, ``refresh_callback`` is invoked once at start with
            ``path=None`` so the watcher can prime the cache without waiting
            for a real change.
    """

    DEFAULT_PATHS = (
        "Beyond-the-Stats/Data/Predictions/upcoming_matchweek_predictions.csv",
        "Beyond-the-Stats/Data/Predictions/upcoming_cup_predictions.csv",
        "Beyond-the-Stats/Data/Predictions/upcoming_national_team_predictions.csv",
        "Beyond-the-Stats/MLS/Data/Predictions/upcoming_matchweek_predictions.csv",
        "Beyond-the-Stats/Extra-leagues/Data/Predictions/upcoming_matchweek_predictions.csv",
    )

    def __init__(
        self,
        project_root: Path,
        upcoming_paths: Optional[Iterable[Path]] = None,
        poll_interval_s: float = 300.0,
        refresh_callback: Optional[Callable[[Optional[Path]], None]] = None,
        bootstrap: bool = True,
    ) -> None:
        self.project_root = Path(project_root)
        self.poll_interval_s = float(poll_interval_s)
        self.refresh_callback = refresh_callback
        self.bootstrap = bootstrap
        if upcoming_paths is None:
            self._paths = [self.project_root / p for p in self.DEFAULT_PATHS]
        else:
            self._paths = [self.project_root / p for p in upcoming_paths]
        self._stop = False
        self._thread = None
        self._last_signatures: dict[Path, tuple] = {}

    # -- public API -------------------------------------------------------

    def start(self) -> None:
        import threading

        if self._thread is not None and self._thread.is_alive():
            return
        # Prime signatures so the first poll doesn't fire a spurious refresh.
        for path in self._paths:
            self._last_signatures[path] = self._signature(path)
        self._thread = threading.Thread(
            target=self._run, name="future-games-watcher", daemon=True
        )
        self._thread.start()
        LOG.info(
            "[watcher] started: %d path(s), interval=%.1fs",
            len(self._paths),
            self.poll_interval_s,
        )
        if self.bootstrap and self.refresh_callback is not None:
            try:
                self.refresh_callback(None)
            except Exception as exc:
                LOG.exception("[watcher] bootstrap refresh failed: %s", exc)

    def stop(self) -> None:
        self._stop = True

    def poll_once(self) -> list[Path]:
        """Inspect watched files; return the list of paths whose signature changed."""
        changed: list[Path] = []
        for path in self._paths:
            sig = self._signature(path)
            previous = self._last_signatures.get(path)
            if previous is None:
                self._last_signatures[path] = sig
                continue
            if sig != previous:
                changed.append(path)
                self._last_signatures[path] = sig
        return changed

    # -- internals --------------------------------------------------------

    @staticmethod
    def _signature(path: Path) -> tuple:
        """Return a cheap signature for a CSV: mtime, size, first non-header row."""
        try:
            stat = path.stat()
        except FileNotFoundError:
            return (0, 0, "")
        # Cheap content fingerprint: first match row's (home, away, date).
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.reader(fh)
                next(reader, None)  # skip header
                first_row = next(reader, None)
        except Exception:
            first_row = None
        first_key = ",".join(first_row) if first_row else ""
        return (int(stat.st_mtime), int(stat.st_size), first_key)

    def _run(self) -> None:
        while not self._stop:
            time.sleep(self.poll_interval_s)
            if self._stop:
                break
            try:
                changed = self.poll_once()
            except Exception as exc:
                LOG.exception("[watcher] poll failed: %s", exc)
                continue
            if not changed:
                continue
            LOG.info("[watcher] detected change in %d file(s): %s", len(changed), changed)
            if self.refresh_callback is not None:
                for path in changed:
                    try:
                        self.refresh_callback(path)
                    except Exception as exc:
                        LOG.exception(
                            "[watcher] refresh callback failed for %s: %s",
                            path,
                            exc,
                        )
                        continue
                    # Only fire once per poll cycle even if multiple files
                    # changed in the same cycle -- the upcoming script picks
                    # up everything anyway.
                    break


def run_subprocess_with_logging(cmd: list[str], cwd: Path, log_path: Optional[Path] = None) -> int:
    """Run ``cmd`` in ``cwd`` and stream stdout/stderr to the log. Returns rc."""
    LOG.info("[subprocess] %s", " ".join(str(c) for c in cmd))
    log_fh = None
    stdout = subprocess.PIPE
    stderr = subprocess.STDOUT
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
        stdout = log_fh
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=stdout,
            stderr=stderr,
            text=True,
            bufsize=1,
        )
        rc = proc.wait()
        return rc
    finally:
        if log_fh is not None:
            log_fh.close()
