"""
Persist pipeline terminal output for API inspection.

All pipeline runners (Run_All_Pipeline, Daily_Pipeline via BackendServer)
append stdout lines here so ``GET /api/pipeline/logs`` can return the same
WARN / ERROR / Skipping messages visible in the terminal.
"""
from __future__ import annotations

import io
import os
import re
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

SP_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_FILE = SP_DIR / "Data" / "pipeline_latest.log"

_LOCK = threading.Lock()
_ACTIVE_TEE: "PipelineStdoutTee | None" = None

_LEVEL_PATTERNS = {
    "error": re.compile(r"\[(ERROR|FAIL|TIMEOUT)\]|failed with exit code|Traceback \(most recent", re.I),
    "warn": re.compile(r"\[(WARN|WARNING)\]|→ Skipping|Skipped\b", re.I),
    "ok": re.compile(r"\[OK\]", re.I),
    "debug": re.compile(r"\[DEBUG\]", re.I),
}


def log_path() -> Path:
    env = os.environ.get("PIPELINE_LOG_FILE", "").strip()
    return Path(env) if env else DEFAULT_LOG_FILE


def _classify_line(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    for level, pattern in _LEVEL_PATTERNS.items():
        if pattern.search(stripped):
            return level
    return None


def start_run(trigger: str = "manual", *, reset: bool = True) -> Path:
    """Open (or truncate) the latest pipeline log for a new run."""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    header = f"=== Pipeline run started trigger={trigger} at {stamp} ===\n"
    with _LOCK:
        if reset:
            path.write_text(header, encoding="utf-8")
        else:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(header)
    return path


def append_line(line: str) -> None:
    """Append one line of terminal output (without adding extra newlines)."""
    if line is None:
        return
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line if line.endswith("\n") else f"{line}\n")


def append_text(text: str) -> None:
    if not text:
        return
    for line in text.splitlines(keepends=True):
        append_line(line.rstrip("\n"))


def log_stats() -> dict:
    path = log_path()
    if not path.exists():
        return {"log_file": str(path), "exists": False, "bytes": 0, "lines": 0}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"log_file": str(path), "exists": True, "bytes": 0, "lines": 0}
    return {
        "log_file": str(path),
        "exists": True,
        "bytes": len(raw.encode("utf-8")),
        "lines": raw.count("\n") + (1 if raw and not raw.endswith("\n") else 0),
    }


def extract_highlights(lines: list[str], *, limit: int = 100) -> list[dict]:
    out: list[dict] = []
    for idx, text in enumerate(lines, start=1):
        level = _classify_line(text)
        if level in {"error", "warn"}:
            out.append({"line_no": idx, "level": level, "text": text.rstrip("\n")})
        if len(out) >= limit:
            break
    return out


def read_log(
    *,
    tail: int = 500,
    level: str = "all",
    grep: str = "",
    highlights_limit: int = 100,
) -> dict:
    """Read tail of the persisted pipeline log with optional filters."""
    path = log_path()
    stats = log_stats()
    if not stats.get("exists"):
        return {
            **stats,
            "returned_lines": 0,
            "truncated": False,
            "lines": [],
            "text": "",
            "highlights": [],
        }

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {
            **stats,
            "ok": False,
            "error": str(exc),
            "returned_lines": 0,
            "truncated": False,
            "lines": [],
            "text": "",
            "highlights": [],
        }

    all_lines = raw.splitlines()
    total_lines = len(all_lines)
    tail = max(1, min(int(tail or 500), 5000))
    sliced = all_lines[-tail:] if total_lines > tail else all_lines
    start_no = total_lines - len(sliced) + 1

    grep_re = re.compile(grep, re.I) if grep else None
    level = (level or "all").strip().lower()

    rows: list[dict] = []
    for offset, text in enumerate(sliced):
        line_no = start_no + offset
        classified = _classify_line(text)
        if level == "error" and classified != "error":
            continue
        if level == "warn" and classified not in {"warn", "error"}:
            continue
        if level == "notable" and classified not in {"warn", "error", "ok"}:
            continue
        if grep_re and not grep_re.search(text):
            continue
        rows.append({
            "line_no": line_no,
            "level": classified,
            "text": text,
        })

    highlights = extract_highlights(all_lines, limit=highlights_limit)
    return {
        **stats,
        "total_lines": total_lines,
        "returned_lines": len(rows),
        "truncated": total_lines > tail,
        "tail_requested": tail,
        "filter_level": level,
        "grep": grep or None,
        "lines": rows,
        "text": "\n".join(r["text"] for r in rows),
        "highlights": highlights,
    }


class PipelineStdoutTee(io.TextIOBase):
    """Mirror stdout to the pipeline log file."""

    def __init__(self, trigger: str = "manual"):
        self._stdout = sys.stdout
        self._path = start_run(trigger=trigger, reset=True)
        self._file = self._path.open("a", encoding="utf-8")

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._stdout.write(data)
        self._file.write(data)
        self._file.flush()
        return len(data)

    def flush(self) -> None:
        self._stdout.flush()
        self._file.flush()

    def close(self) -> None:
        global _ACTIVE_TEE
        try:
            append_line(f"=== Pipeline run ended at {datetime.now(UTC).replace(microsecond=0).isoformat()} ===")
        except Exception:
            pass
        try:
            self._file.close()
        except Exception:
            pass
        sys.stdout = self._stdout
        _ACTIVE_TEE = None


def activate_stdout_tee(trigger: str = "manual") -> PipelineStdoutTee:
    """Replace sys.stdout with a tee that also writes to the pipeline log."""
    global _ACTIVE_TEE
    if _ACTIVE_TEE is not None:
        return _ACTIVE_TEE
    _ACTIVE_TEE = PipelineStdoutTee(trigger=trigger)
    sys.stdout = _ACTIVE_TEE
    return _ACTIVE_TEE


def deactivate_stdout_tee() -> None:
    global _ACTIVE_TEE
    if _ACTIVE_TEE is not None:
        _ACTIVE_TEE.close()
        _ACTIVE_TEE = None
