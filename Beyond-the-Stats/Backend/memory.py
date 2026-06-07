"""
Memory monitor for the backend.

Provides a small ``MemoryMonitor`` class that:
- Reads the total resident-set size of the current Python process group
  (i.e. main + any child processes spawned by multiprocessing).
- Polls at a configurable interval.
- Exposes a callback hook that the scheduler / pipeline orchestrator can use
  to pause work or warn the operator.

Uses ``psutil`` when available (cross-platform, accurate per-process numbers)
and falls back to a simple per-process RSS read on Linux/macOS via
``/proc/self/status`` and on Windows via the Win32 ``GetProcessMemoryInfo``
through ``ctypes``.

The 12 GB default ceiling is intentionally conservative — the model cache
pkl files alone are 4.7 GB and the WC projection sim loop is the dominant
spike (peaks around 2-3 GB). 12 GB leaves enough headroom for OS, the Flask
app, and the data pandas is holding.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("backend.memory")

try:
    import psutil  # type: ignore

    _HAS_PSUTIL = True
except Exception:  # pragma: no cover - psutil is optional
    _HAS_PSUTIL = False


def _rss_bytes_fallback() -> int:
    """Best-effort RSS read without psutil. Returns 0 on failure."""
    try:
        if sys.platform.startswith("linux") or sys.platform == "darwin":
            with open("/proc/self/status", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
            return 0
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.kernel32.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return int(counters.WorkingSetSize)
            return 0
    except Exception:
        return 0
    return 0


@dataclass
class MemoryReading:
    rss_bytes: int
    rss_mb: float
    limit_bytes: int
    limit_mb: float
    utilization_pct: float
    source: str


class MemoryMonitor:
    """Periodically sample the current process group's RSS and report on it.

    Args:
        limit_gb: Hard ceiling for the process group in gigabytes. The monitor
            calls :py:meth:`on_exceeded` whenever the reading rises above
            ``limit_gb``. Default 12 GB.
        poll_interval_s: Seconds between samples. Default 30.
        on_exceeded: Optional callback receiving the :class:`MemoryReading`
            whenever the ceiling is crossed. The callback runs in the monitor
            thread, so it must be thread-safe.
        on_warn: Optional callback for soft warnings (90% of limit).
    """

    def __init__(
        self,
        limit_gb: float = 12.0,
        poll_interval_s: float = 30.0,
        on_exceeded=None,
        on_warn=None,
    ) -> None:
        self.limit_bytes = int(limit_gb * (1024 ** 3))
        self.poll_interval_s = float(poll_interval_s)
        self.on_exceeded = on_exceeded
        self.on_warn = on_warn
        self._proc = psutil.Process(os.getpid()) if _HAS_PSUTIL else None
        self._stop = False
        self._last: Optional[MemoryReading] = None
        self._warned = False
        self._exceeded = False

    # -- public API -------------------------------------------------------

    def current(self) -> MemoryReading:
        return self._read_once()

    def start(self) -> None:
        """Start the background polling thread."""
        import threading

        if hasattr(self, "_thread") and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(
            target=self._run, name="memory-monitor", daemon=True
        )
        self._thread.start()
        LOG.info(
            "[memory] monitor started: limit=%.1f GB interval=%.1fs psutil=%s",
            self.limit_bytes / 1024 ** 3,
            self.poll_interval_s,
            _HAS_PSUTIL,
        )

    def stop(self) -> None:
        self._stop = True

    # -- internals --------------------------------------------------------

    def _read_once(self) -> MemoryReading:
        rss = 0
        source = "fallback"
        if self._proc is not None:
            try:
                rss = int(self._proc.memory_info().rss)
                source = "psutil-self"
            except Exception:
                rss = 0
        if rss == 0 and _HAS_PSUTIL:
            try:
                rss = int(psutil.Process(os.getpid()).memory_info().rss)
                source = "psutil-retry"
            except Exception:
                rss = 0
        if rss == 0:
            rss = _rss_bytes_fallback()
            source = "ctypes" if rss else "unknown"

        rss_mb = rss / 1024 ** 2
        limit_mb = self.limit_bytes / 1024 ** 2
        utilization = (rss / self.limit_bytes * 100.0) if self.limit_bytes else 0.0
        reading = MemoryReading(
            rss_bytes=rss,
            rss_mb=rss_mb,
            limit_bytes=self.limit_bytes,
            limit_mb=limit_mb,
            utilization_pct=utilization,
            source=source,
        )
        self._last = reading
        return reading

    def _run(self) -> None:
        while not self._stop:
            reading = self._read_once()
            if reading.utilization_pct >= 100.0:
                if not self._exceeded:
                    LOG.error(
                        "[memory] EXCEEDED limit: %.1f MB / %.1f MB (%.1f%%)",
                        reading.rss_mb,
                        reading.limit_mb,
                        reading.utilization_pct,
                    )
                    self._exceeded = True
                    self._warned = True
                    if self.on_exceeded:
                        try:
                            self.on_exceeded(reading)
                        except Exception as exc:
                            LOG.exception("[memory] on_exceeded callback failed: %s", exc)
            elif reading.utilization_pct >= 90.0:
                if not self._warned:
                    LOG.warning(
                        "[memory] approaching limit: %.1f MB / %.1f MB (%.1f%%)",
                        reading.rss_mb,
                        reading.limit_mb,
                        reading.utilization_pct,
                    )
                    self._warned = True
                    if self.on_warn:
                        try:
                            self.on_warn(reading)
                        except Exception as exc:
                            LOG.exception("[memory] on_warn callback failed: %s", exc)
            else:
                if self._exceeded:
                    LOG.info(
                        "[memory] back under limit: %.1f MB / %.1f MB",
                        reading.rss_mb,
                        reading.limit_mb,
                    )
                self._exceeded = False
                self._warned = False
            time.sleep(self.poll_interval_s)


def is_over_budget_gb(limit_gb: float = 12.0) -> bool:
    """Quick check (no thread): is the process group over the GB limit right now?"""
    monitor = MemoryMonitor(limit_gb=limit_gb, poll_interval_s=1.0)
    return monitor.current().utilization_pct >= 100.0
