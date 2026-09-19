"""
Full pipeline orchestrator — runs all data, model, and prediction steps.

Invoked by ``Daily_Pipeline.py`` (scheduled) or directly from the command
line.  Also triggered by the ``/api/refresh`` endpoint on the Flask server.

Execution order
---------------
1. **Real standings** (pre-pipeline) — build real tables from completed CSV results
2. **Sub-pipelines** — always sequential, in a fixed order: global first, then
   MLS, then the other leagues (extra). Only one training run or table
   projection is active at a time; each heavy step gets every CPU except one
   (the core left free for the backend's live score polling and API/file
   serving). Each sub-pipeline runs its steps sequentially: Download → Process
   → Sort → Model Cache → Predict Upcoming → Project League Table →
   (national team / WC when a World Cup is active)
3. **Post-pipeline steps** (after all sub-pipelines finish):
   Settle predictions (update CSVs with real results from ESPN),
   Sync club friendlies, Track cup results
4. **Cups last** — upcoming cup predictions run after every league sub-pipeline

Flags
-----
``--skip-global / --skip-mls / --skip-extra`` — skip entire sub-pipelines
``--skip-model-train`` — skip model retraining on light refresh days; still builds
  the cache automatically when the file is missing or unloadable. Full retrains
  run on Tuesday and Friday via the backend scheduler.
``--continue-on-error`` — keep going even if individual steps fail (default: fail-fast)
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAIN_DIR = Path(__file__).resolve().parent
SP_DIR = MAIN_DIR.parent
ROOT_DIR = SP_DIR.parent
if str(SP_DIR) not in sys.path:
    sys.path.insert(0, str(SP_DIR))
if str(SP_DIR / "shared") not in sys.path:
    sys.path.insert(0, str(SP_DIR / "shared"))

import pandas as pd

import pipeline_log
import model_cache_util
import season_calendar
from shared import paths as _paths

_paths.ensure_output_dirs()

FILES_DIR = _paths.EUROPE_FILES_DIR
MLS_FILES_DIR = _paths.MLS_FILES_DIR
EXTRA_FILES_DIR = _paths.EXTRA_FILES_DIR
LOCAL_KEYS_FILE = FILES_DIR / "local_api_keys.json"

# Sub-pipelines ALWAYS run sequentially (global -> MLS -> extra, cups last) so
# only one training run or table projection is ever active. The --workers flag
# is accepted for backward compatibility but values above 1 are ignored.
DEFAULT_SUBPIPELINE_WORKERS = 1
MAX_SUBPIPELINE_WORKERS = 3

LAST_REFRESH_FILE = _paths.LAST_REFRESH_FILE
PIPELINE_STATUS_FILE = _paths.PIPELINE_STATUS_FILE

_KNOWN_WORLD_CUP_WINDOWS = [
    # 2026 World Cup window closed after the tournament ended (Jul 2026).
    # Re-enable / extend when preparing the next World Cup cycle.
    # (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 7, 25, tzinfo=UTC)),
    (datetime(2030, 5, 1, tzinfo=UTC), datetime(2030, 7, 31, tzinfo=UTC)),
]


def _world_cup_is_active():
    today = datetime.now(UTC)
    for start, end in _KNOWN_WORLD_CUP_WINDOWS:
        if start <= today <= end:
            return True
    return False

# Upcoming CSV paths for archival to past_games.json
GLOBAL_UPCOMING_FILE = _paths.GLOBAL_UPCOMING_FILE
MLS_UPCOMING_FILE = _paths.MLS_UPCOMING_FILE
EXTRA_UPCOMING_FILE = _paths.EXTRA_UPCOMING_FILE
CUP_UPCOMING_FILE = _paths.CUP_UPCOMING_FILE
NATIONAL_UPCOMING_FILE = _paths.NATIONAL_UPCOMING_FILE
PAST_GAMES_FILE = _paths.PAST_GAMES_FILE

# Monotonic timestamp set by run_full_pipeline so run_step can log elapsed time.
_pipeline_start_global: float = 0.0

# Last run_step outcome, exposed for downstream diagnostics (exit code of a
# failed step: e.g. -9/137 means the kernel OOM killer killed the process).
_last_step_result = {"rc": None, "success": False}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run full soccer pipeline: data pull -> processing -> model cache -> predictions -> tables -> settle."
    )
    parser.add_argument(
        "--skip-mls",
        action="store_true",
        help="Skip MLS pipeline steps.",
    )
    parser.add_argument(
        "--skip-extra",
        action="store_true",
        help="Skip extra-leagues pipeline steps.",
    )
    parser.add_argument(
        "--skip-global",
        action="store_true",
        help="Skip European/global pipeline steps.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=365,
        help="Legacy fixture window days (league scripts use season-aware Jul–May / Jan–Dec bounds).",
    )
    parser.add_argument(
        "--cup-window-days",
        type=int,
        default=season_calendar.DEFAULT_CUP_LOOKAHEAD_DAYS,
        help="Rolling lookahead in days for cup upcoming fixture scripts (default: 180).",
    )
    parser.add_argument(
        "--national-window-days",
        type=int,
        default=90,
        help="Fixture window days for national-team and World Cup prediction scripts.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue remaining steps if one step fails.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_SUBPIPELINE_WORKERS,
        help=(
            "Sub-pipelines always run sequentially (global -> MLS -> extra) so only "
            "one training run or table projection is active at a time. This flag is "
            "accepted for backwards compatibility but values above 1 are ignored."
        ),
    )
    parser.add_argument(
        "--competition-workers",
        type=int,
        default=0,
        help=(
            "Worker count passed to Project_League_Table.py for per-competition parallel projection. "
            "0 = auto (all CPUs minus 1 reserved for the backend, then RAM-capped); 1 = serial; N = use N processes."
        ),
    )
    parser.add_argument(
        "--skip-model-train",
        action="store_true",
        help="Skip model retraining on light days; still builds cache if missing. Also implies --skip-squad-values.",
    )
    return parser.parse_args()


def load_api_token():
    env_token = os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip()
    if env_token:
        return env_token
    if LOCAL_KEYS_FILE.exists():
        try:
            payload = json.loads(LOCAL_KEYS_FILE.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        token = str(payload.get("FOOTBALL_DATA_API_TOKEN", "")).strip()
        if token:
            return token
    return ""


def _should_build_model_cache(args, label: str, predict_script: Path) -> tuple[bool, str]:
    if not args.skip_model_train:
        return True, "scheduled model retrain (Tue/Fri)"
    pm_mod = model_cache_util.import_predict_match_module(str(predict_script))
    needs, reason = model_cache_util.model_cache_missing_or_broken(pm_mod)
    if needs:
        print(f"[pipeline] [{label}] building model cache (required): {reason}")
        return True, reason
    _fresh, detail = model_cache_util.model_cache_status(pm_mod)
    print(f"[pipeline] [{label}] skipping model cache build ({detail})")
    return False, detail


def _projected_tables_are_mostly_zeroed(output_csv_path: str, *, min_fraction: float = 0.85) -> bool:
    """True when projected_league_tables.csv is missing usable Monte Carlo rows.

    Light-day skips must not leave leagues parked on ``sim_runs=0`` placeholders
    into the season — force a rebuild when the CSV is absent or mostly zeroed.
    """
    if not output_csv_path or not os.path.exists(output_csv_path):
        return True
    try:
        import csv

        with open(output_csv_path, "r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            return True
        zeroed = 0
        for row in rows:
            try:
                if float(row.get("sim_runs") or 0) <= 0:
                    zeroed += 1
            except Exception:
                zeroed += 1
        return (zeroed / max(1, len(rows))) >= float(min_fraction)
    except Exception:
        return True


def _should_run_league_tables(args, output_csv_path: str, processed_dirs: list[str], extra_inputs: list[str] | None = None) -> bool:
    """Projected league tables always run on every scheduled pipeline run.

    The season projections are meant to be redone every day regardless of
    whether it is a full model-retrain day or a light refresh.  The mtime-based
    light-day skip was removed because it made the projected tables sticky:
    once ``projected_league_tables.csv`` became the newest file it stayed
    newest, so the table projection was skipped day after day and the season
    tables never updated on the backend.

    The legacy parameters (``processed_dirs`` / ``extra_inputs``) are kept for
    call-site compatibility; they no longer influence the decision.
    """
    return True


def _tables_only():
    return os.environ.get("BTS_TABLES_ONLY", "").strip().lower() in {"1", "true", "yes"}


# Projected league tables can be CPU/network heavy (Monte Carlo + ESPN crawls).
# Bound them so a hung/slow projection cannot pin the daily pipeline for hours.
PROJECTED_TABLE_TIMEOUT_S = {
    "global": 3600,  # many competitions
    "mls": 2700,
    # Extra PATH B can still be heavy; prefer finishing over parallel kill (-9).
    "extra": 3600,
}
# Upcoming matchweek steps (ESPN + football-data.org). Without a cap, Extra's
# day-walk can run until the backend's 6h wall clock kills the whole pipeline.
UPCOMING_MATCHWEEK_TIMEOUT_S = 3600


class _StepError(Exception):
    """Raised by run_step when a step fails and continue_on_error is False."""

    def __init__(self, name, rc):
        super().__init__(f"'{name}' failed (rc={rc})")
        self.name = name
        self.rc = rc


def run_step(name, cmd, continue_on_error=False, input_text=None, timeout=None, log_file=None):
    global _last_step_result
    _last_step_result = {"rc": None, "success": False}
    print(f"\n=== {name} ===", flush=True)
    print(" ".join(str(c) for c in cmd), flush=True)
    started = time.monotonic()
    print(f"[DEBUG] run_step starting '{name}' at T+{started - _pipeline_start_global:.0f}s "
          f"timeout={timeout}s", flush=True)
    # Optional full-output capture. Set STEP_LOG_DIR to tee every step's stdout/
    # stderr to a file (sub-pipeline output is otherwise lost on Windows spawn).
    if log_file is None:
        log_dir_env = os.environ.get("STEP_LOG_DIR", "").strip()
        if log_dir_env:
            _safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or "step"
            log_file = os.path.join(log_dir_env, f"{_safe}.log")
    log_fh = None
    if log_file:
        try:
            log_fh = open(log_file, "a", encoding="utf-8")
        except Exception:
            log_fh = None
    # Own process group so timeouts can kill Project_League_Table worker pools
    # (grandchildren) instead of leaving them orphaned on the host.
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"[ERROR] {name}: failed to start: {exc} (after {elapsed:.1f}s)", flush=True)
        print(f"  -> Stopping pipeline here: '{name}' failed (set --continue-on-error to keep going)", flush=True)
        if not continue_on_error:
            raise _StepError(name, -1) from exc
        return False

    def _drain(stream):
        try:
            for line in iter(stream.readline, ""):
                print(line, end="" if line.endswith("\n") else "\n", flush=True)
                if log_fh is not None:
                    try:
                        log_fh.write(line)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    if input_text is not None:
        try:
            proc.stdin.write(input_text)
        except Exception:
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass

    readers = []
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            t = threading.Thread(target=_drain, args=(stream,), daemon=True)
            t.start()
            readers.append(t)

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        print(f"[TIMEOUT] {name} exceeded {timeout}s timeout (after {elapsed:.1f}s)", flush=True)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        _last_step_result = {"rc": -1, "success": False}
        print(f"  -> Stopping pipeline here: '{name}' timed out (set --continue-on-error to keep going)", flush=True)
        if not continue_on_error:
            raise _StepError(name, -1)
        return False
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"[ERROR] {name}: {exc} (after {elapsed:.1f}s)", flush=True)
        _last_step_result = {"rc": None, "success": False}
        print(f"  -> Stopping pipeline here: '{name}' failed (set --continue-on-error to keep going)", flush=True)
        if not continue_on_error:
            raise _StepError(name, -1) from exc
        return False

    for t in readers:
        t.join(timeout=10)

    if log_fh is not None:
        try:
            log_fh.close()
        except Exception:
            pass

    elapsed = time.monotonic() - started
    print(f"[DEBUG] run_step finished '{name}' rc={proc.returncode} elapsed={elapsed:.1f}s "
          f"at T+{time.monotonic() - _pipeline_start_global:.0f}s", flush=True)
    if proc.returncode != 0:
        print(f"[ERROR] {name} failed with exit code {proc.returncode} (after {elapsed:.1f}s)", flush=True)
        _last_step_result = {"rc": proc.returncode, "success": False}
        print(f"  -> Stopping pipeline here: '{name}' failed (set --continue-on-error to keep going)", flush=True)
        if not continue_on_error:
            raise _StepError(name, proc.returncode)
        return False

    print(f"[OK] {name} ({elapsed:.1f}s)", flush=True)
    _last_step_result = {"rc": proc.returncode, "success": True}
    return True


def _system_ram_gb():
    """Total system RAM in GB, or 0.0 when it cannot be determined.

    Honored override: ``BTS_RAM_GB`` (e.g. ``BTS_RAM_GB=8``) for hosts where
    the real total cannot be detected (containers, cgroup limits).
    """
    override = os.environ.get("BTS_RAM_GB", "").strip()
    if override:
        try:
            return float(override)
        except Exception:
            pass
    try:
        import psutil  # noqa: PLC0415

        return float(psutil.virtual_memory().total) / (1024 ** 3)
    except Exception:
        pass
    if sys.platform.startswith("linux"):
        try:
            return (
                os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3)
            )
        except Exception:
            return 0.0
    if sys.platform.startswith("win"):
        try:
            import ctypes  # noqa: PLC0415

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = _MEMORYSTATUSEX()
            ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return float(ms.ullTotalPhys) / (1024 ** 3)
        except Exception:
            return 0.0
    return 0.0


def _cgroup_memory_limit_gb():
    """Read container/systemd MemoryMax from cgroup v2/v1 when present."""
    candidates = (
        "/sys/fs/cgroup/memory.max",  # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    )
    for path in candidates:
        try:
            raw = open(path, "r", encoding="utf-8").read().strip()
        except OSError:
            continue
        if not raw or raw.lower() == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # Ignore absurd "unlimited" sentinels from older kernels.
        if value <= 0 or value >= (1 << 60):
            continue
        return float(value) / (1024 ** 3)
    return 0.0


def _effective_memory_budget_gb():
    """RAM available to this process for sizing competition workers.

    Prefer the backend/systemd ceiling over bare-metal total RAM. A host with
    64 GB physical but ``MemoryMax=14G`` / ``--memory-limit-gb 14`` must size
    workers for 14 GB, not 64 — otherwise league-table pools stack model copies
    until the memory monitor kills the pipeline (~36 GB observed).
    """
    candidates = []
    for env_key in ("BTS_MEMORY_LIMIT_GB", "BTS_RAM_GB"):
        raw = os.environ.get(env_key, "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 0:
            candidates.append(value)
    cgroup = _cgroup_memory_limit_gb()
    if cgroup > 0:
        candidates.append(cgroup)
    physical = _system_ram_gb()
    if physical > 0:
        candidates.append(physical)
    return min(candidates) if candidates else 0.0


def _resolve_competition_workers(args):
    """Choose per-competition projection workers for a single active build.

    Sub-pipelines always run sequentially, so only one training or table
    projection is ever active at a time. Projection workers historically
    re-pickled the multi-GB model cache into every child; even with fork CoW
    they still dirty large scratch pages during Monte Carlo. Cap workers from
    the *effective* memory budget (cgroup / ``BTS_MEMORY_LIMIT_GB`` / RAM),
    not just physical RAM. Override with ``BTS_COMPETITION_WORKERS`` or
    ``--competition-workers N``.
    """
    requested = int(getattr(args, "competition_workers", 0) or 0)
    override = os.environ.get("BTS_COMPETITION_WORKERS", "").strip()
    if override:
        try:
            forced = max(1, int(override))
            return min(forced, max(1, os.cpu_count() or 1))
        except Exception:
            pass
    cpu = os.cpu_count() or 1
    budget_gb = _effective_memory_budget_gb()
    if requested <= 0:
        # Reserve cores for the backend (live polling / API / gunicorn).
        reserve = 2 if cpu >= 4 else 1
        auto = max(1, cpu - reserve)
    else:
        auto = max(1, int(requested))
    # Europe model_cache.pkl is multi-GB. Under a 14 GB MemoryMax the only safe
    # default is sequential (1). Slightly larger budgets may run 2–3 workers
    # once CoW inheritance is used without re-pickling initargs.
    if budget_gb:
        if budget_gb < 16:
            auto = min(auto, 1)
        elif budget_gb < 24:
            auto = min(auto, 2)
        elif budget_gb < 32:
            auto = min(auto, 3)
        else:
            auto = min(auto, 4)
    reserve = 2 if cpu >= 4 else 1
    auto = min(auto, max(1, cpu - reserve))
    return auto


def _project_table_cmd(files_dir, comp_workers, base_name):
    """Build the Project_League_Table command with --competition-workers when >0."""
    cmd = [sys.executable, str(files_dir / base_name)]
    if comp_workers and comp_workers > 0:
        cmd += ["--competition-workers", str(comp_workers)]
    return cmd


def _probe_projected_tables(csv_path):
    """Return a diagnostic dict describing the state of a projected tables CSV."""
    import csv as _csv

    diag = {
        "path": csv_path,
        "exists": False,
        "mtime": None,
        "rows": 0,
        "zeroed_rows": 0,
        "usable_fraction": 0.0,
        "usable": False,
        "reason": "file missing",
    }
    if not csv_path or not os.path.exists(csv_path):
        return diag
    try:
        diag["exists"] = True
        diag["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(csv_path)))
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
            rows = list(_csv.DictReader(fh))
    except Exception as exc:
        diag["reason"] = f"read error: {exc}"
        return diag
    diag["rows"] = len(rows)
    if not rows:
        diag["reason"] = "empty CSV (0 rows)"
        return diag
    zeroed = 0
    for row in rows:
        try:
            if float(row.get("sim_runs") or 0) <= 0:
                zeroed += 1
        except Exception:
            zeroed += 1
    diag["zeroed_rows"] = zeroed
    usable_fraction = (len(rows) - zeroed) / len(rows)
    diag["usable_fraction"] = round(usable_fraction, 4)
    diag["usable"] = usable_fraction >= 0.5
    diag["reason"] = (
        f"{len(rows)} rows, {zeroed} zeroed (sim_runs=0), "
        f"{round(usable_fraction * 100.0, 1)}% usable"
    )
    return diag


def _write_tables_diagnostics(diagnostics):
    """Persist a compact health report so the tables state is visible on the host.

    Written to ``Output/Predictions/shared/projected_tables_diagnostics.json`` after
    every pipeline run. Shows per-pipeline path, mtime, row counts, zeroed
    rows, and the reason a CSV was unusable.
    """
    try:
        out_path = _paths.OUTPUT_PRED_SHARED / "projected_tables_diagnostics.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "written_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "host": {"platform": sys.platform, "ram_gb": round(_system_ram_gb(), 1) or None},
            "pipelines": {k: v for k, v in sorted(diagnostics.items())},
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"[tables] diagnostics written: {out_path}")
    except Exception as exc:
        print(f"[WARN] could not write projected_tables_diagnostics.json: {exc}")


def _ensure_projected_tables(args):
    """Guarantee projected league tables exist after the sub-pipelines.

    The tables are normally written inside each sub-pipeline child process.
    If a child died before reaching that step (e.g. OOM or an uncaught
    fork/executor exception on the host), the website would be left without
    season projections for the day. Re-run the projection in the parent and
    record a clean before/after health report.
    """
    targets = []
    if not args.skip_global:
        targets.append(
            ("global", FILES_DIR, str(_paths.GLOBAL_PROJECTED_TABLE_FILE))
        )
    if not args.skip_mls:
        targets.append(
            ("mls", MLS_FILES_DIR, str(_paths.MLS_PROJECTED_TABLE_FILE))
        )
    if not args.skip_extra:
        targets.append(
            (
                "extra",
                EXTRA_FILES_DIR,
                str(_paths.EXTRA_PROJECTED_TABLE_FILE),
            )
        )
    comp_workers = _resolve_competition_workers(args)
    results = {}
    diagnostics = {}
    for key, files_dir, csv_path in targets:
        before = _probe_projected_tables(csv_path)
        diagnostics[key] = {"before": before, "after": None, "reprojected": None}
        if before["usable"]:
            print(f"[tables] {key}: OK ({before['reason']})")
            results[f"{key}_projected_league_tables"] = True
            continue
        print(
            f"[tables] {key}: UNUSABLE ({before['reason']}) -- "
            "forcing re-projection in parent process"
        )
        reprojected = run_step(
            f"[{key}] Projected league tables (fallback)",
            _project_table_cmd(files_dir, comp_workers, "Project_League_Table.py"),
            continue_on_error=True,
        )
        after = _probe_projected_tables(csv_path)
        diagnostics[key]["after"] = after
        diagnostics[key]["reprojected"] = reprojected
        if after["usable"]:
            print(f"[tables] {key}: {('RECOVERED' if reprojected else 'OK')} ({after['reason']})")
            results[f"{key}_projected_league_tables"] = True
        else:
            rc = _last_step_result.get("rc")
            print(
                f"[tables] {key}: FAILED -- fallback step rc={rc} ({after['reason']})"
            )
            results[f"{key}_projected_league_tables"] = False
    _write_tables_diagnostics(diagnostics)
    return results


def _run_global_subpipeline(args, api_token):
    """Run all global (European) + national + WC steps. Returns dict of results."""
    py = sys.executable
    sub = {}
    comp_workers = _resolve_competition_workers(args)

    if _tables_only():
        sub["global_projected_league_tables"] = run_step(
            "[global] Projected league tables",
            _project_table_cmd(FILES_DIR, comp_workers, "Project_League_Table.py"),
            continue_on_error=args.continue_on_error,
            timeout=PROJECTED_TABLE_TIMEOUT_S["global"],
        )
        return sub

    sub["global_download_process_sort"] = run_step(
        "[global] Download, process and sort latest data",
        [py, str(FILES_DIR / "Download_Latest_Data.py")],
        continue_on_error=args.continue_on_error,
        timeout=3600,
    )
    sub["global_build_historical_tables"] = run_step(
        "[global] Build historical season tables",
        [py, str(FILES_DIR / "Build_Historical_Tables.py")],
        continue_on_error=args.continue_on_error,
        timeout=600,
    )
    if _should_build_model_cache(args, "global", FILES_DIR / "Predict_Match.py")[0]:
        sub["global_build_model_cache"] = run_step(
            "[global] Build model cache (non-interactive)",
            [py, str(FILES_DIR / "Predict_Match.py"), "--build-cache-only"],
            continue_on_error=args.continue_on_error,
            timeout=3600,
        )
    upcoming_cmd = [py, str(FILES_DIR / "Predict_Upcoming_Matchweek.py"), "--window-days", str(args.window_days)]
    if api_token:
        upcoming_cmd += ["--api-token", api_token]
    sub["global_upcoming_matchweek"] = run_step(
        "[global] Upcoming matchweek predictions",
        upcoming_cmd,
        continue_on_error=args.continue_on_error,
        timeout=UPCOMING_MATCHWEEK_TIMEOUT_S,
    )
    global_out_csv = str(_paths.GLOBAL_PROJECTED_TABLE_FILE)
    global_proc_dirs = [str(_paths.DATA_DIR / "Processed_Data")]
    global_roster_inputs = [
        str(_paths.CURRENT_SEASON_TEAMS_FILE),
        str(_paths.LEAGUE_TEAMS_FILE),
        str(FILES_DIR / "preseason" / "2026_27_league_team_fallback.json"),
    ]
    if _should_run_league_tables(args, global_out_csv, global_proc_dirs, global_roster_inputs):
        sub["global_projected_league_tables"] = run_step(
            "[global] Projected league tables",
            _project_table_cmd(FILES_DIR, comp_workers, "Project_League_Table.py"),
            continue_on_error=args.continue_on_error,
            timeout=PROJECTED_TABLE_TIMEOUT_S["global"],
        )
    else:
        print("[skip] No processed data changes — skipping global league table projection")
    if _world_cup_is_active():
        national_process_cmd = [py, str(FILES_DIR / "Process_National_Team_Data.py"), "--world-cup-only"]
        if args.skip_model_train:
            national_process_cmd.append("--skip-squad-values")
        sub["national_world_cup_model"] = run_step(
            "[global] National team World Cup model",
            national_process_cmd,
            continue_on_error=args.continue_on_error,
        )
        national_upcoming_cmd = [
            py,
            str(FILES_DIR / "Predict_Upcoming_National_Team_Games.py"),
            "--world-cup-only",
            "--window-days",
            str(args.national_window_days),
        ]
        if api_token:
            national_upcoming_cmd += ["--api-token", api_token]
        sub["upcoming_world_cup_predictions"] = run_step(
            "[global] Upcoming World Cup predictions",
            national_upcoming_cmd,
            continue_on_error=args.continue_on_error,
        )
        world_cup_project_cmd = [py, str(FILES_DIR / "Project_World_Cup.py")]
        if api_token:
            world_cup_project_cmd += ["--api-token", api_token]
        sub["projected_world_cup"] = run_step(
            "[global] Projected World Cup groups and bracket",
            world_cup_project_cmd,
            continue_on_error=args.continue_on_error,
        )
    else:
        print("[skip] No active World Cup window — skipping World Cup steps")
    return sub


def _run_mls_subpipeline(args, api_token):
    """Run the MLS sub-pipeline. Returns dict of results."""
    py = sys.executable
    sub = {}
    comp_workers = _resolve_competition_workers(args)

    if _tables_only():
        sub["mls_projected_league_tables"] = run_step(
            "[mls] Projected league tables",
            _project_table_cmd(MLS_FILES_DIR, comp_workers, "Project_League_Table.py"),
            continue_on_error=args.continue_on_error,
            timeout=PROJECTED_TABLE_TIMEOUT_S["mls"],
        )
        return sub

    mls_dl_cmd = [py, str(MLS_FILES_DIR / "Download_Latest_Data.py")]
    if args.skip_model_train:
        mls_dl_cmd.append("--skip-squad-values")
    sub["mls_download_process_sort"] = run_step(
        "[mls] Download/process/sort latest data",
        mls_dl_cmd,
        continue_on_error=args.continue_on_error,
        timeout=1200,
    )
    sub["mls_build_historical_tables"] = run_step(
        "[mls] Build historical season tables",
        [py, str(MLS_FILES_DIR / "Build_Historical_Tables.py")],
        continue_on_error=args.continue_on_error,
        timeout=600,
    )
    sub["mls_build_model_cache"] = run_step(
        "[mls] Build model cache (non-interactive)",
        [py, str(MLS_FILES_DIR / "Predict_Match.py"), "--build-cache-only"],
        continue_on_error=args.continue_on_error,
        timeout=3600,
    )
    mls_upcoming_cmd = [py, str(MLS_FILES_DIR / "Predict_Upcoming_Matchweek.py"), "--window-days", str(args.window_days)]
    if api_token:
        mls_upcoming_cmd += ["--api-token", api_token]
    sub["mls_upcoming_matchweek"] = run_step(
        "[mls] Upcoming matchweek predictions",
        mls_upcoming_cmd,
        continue_on_error=args.continue_on_error,
        timeout=UPCOMING_MATCHWEEK_TIMEOUT_S,
    )
    mls_out_csv = str(_paths.MLS_PROJECTED_TABLE_FILE)
    mls_proc_dirs = [str(_paths.MLS_DATA_DIR / "Processed_Data")]
    mls_roster_inputs = [
        str(_paths.CURRENT_SEASON_TEAMS_FILE),
        str(_paths.LEAGUE_TEAMS_FILE),
    ]
    if _should_run_league_tables(args, mls_out_csv, mls_proc_dirs, mls_roster_inputs):
        sub["mls_projected_league_tables"] = run_step(
            "[mls] Projected league tables",
            _project_table_cmd(MLS_FILES_DIR, comp_workers, "Project_League_Table.py"),
            continue_on_error=args.continue_on_error,
            timeout=PROJECTED_TABLE_TIMEOUT_S["mls"],
        )
    else:
        print("[skip] No processed data changes — skipping MLS league table projection")
    return sub


def _run_extra_subpipeline(args, api_token):
    """Run the extra-leagues sub-pipeline (smaller European / S. American / Asian leagues)."""
    py = sys.executable
    sub = {}
    # Extra league tables use the same single-build worker auto as global/MLS:
    # sub-pipelines always run sequentially here, so the full machine minus the
    # one reserved backend core can work on this build alone. (RAM caps in
    # _resolve_competition_workers still protect against stack-on-stack cache
    # loads, which is what caused the old -9 SIGKILLs.)
    comp_workers = _resolve_competition_workers(args)
    if _tables_only():
        sub["extra_projected_league_tables"] = run_step(
            "[extra] Projected league tables",
            _project_table_cmd(EXTRA_FILES_DIR, comp_workers, "Project_League_Table.py"),
            continue_on_error=args.continue_on_error,
            timeout=PROJECTED_TABLE_TIMEOUT_S["extra"],
        )
        return sub
    sub["extra_download_process_sort"] = run_step(
        "[extra] Download/process/sort latest data",
        [py, str(EXTRA_FILES_DIR / "Download_Latest_Data.py")],
        continue_on_error=args.continue_on_error,
        timeout=1200,
    )
    sub["extra_build_historical_tables"] = run_step(
        "[extra] Build historical season tables",
        [py, str(EXTRA_FILES_DIR / "Build_Historical_Tables.py")],
        continue_on_error=args.continue_on_error,
        timeout=600,
    )
    if _should_build_model_cache(args, "extra", EXTRA_FILES_DIR / "Predict_Match.py")[0]:
        sub["extra_build_model_cache"] = run_step(
            "[extra] Build model cache (non-interactive)",
            [py, str(EXTRA_FILES_DIR / "Predict_Match.py"), "--build-cache-only"],
            continue_on_error=args.continue_on_error,
            timeout=3600,
        )
    sub["extra_upcoming_matchweek"] = run_step(
        "[extra] Upcoming matchweek predictions",
        [py, str(EXTRA_FILES_DIR / "Predict_Upcoming_Matchweek.py"), "--window-days", str(args.window_days)],
        continue_on_error=args.continue_on_error,
        timeout=UPCOMING_MATCHWEEK_TIMEOUT_S,
    )
    extra_out_csv = str(_paths.EXTRA_PROJECTED_TABLE_FILE)
    extra_proc_dirs = [
        str(_paths.EXTRA_DATA_DIR / "Processed_Data"),
        str(_paths.DATA_DIR / "Processed_Data"),  # extra uses shared Processed_Data too
    ]
    extra_roster_inputs = [
        str(_paths.CURRENT_SEASON_TEAMS_FILE),
        str(_paths.LEAGUE_TEAMS_FILE),
        str(FILES_DIR / "preseason" / "2026_27_league_team_fallback.json"),
    ]
    if _should_run_league_tables(args, extra_out_csv, extra_proc_dirs, extra_roster_inputs):
        sub["extra_projected_league_tables"] = run_step(
            "[extra] Projected league tables",
            _project_table_cmd(EXTRA_FILES_DIR, comp_workers, "Project_League_Table.py"),
            continue_on_error=args.continue_on_error,
            timeout=PROJECTED_TABLE_TIMEOUT_S["extra"],
        )
    else:
        print("[skip] No processed data changes — skipping extra league table projection")
    return sub


def _run_sub_pipeline(name, fn, args, api_token):
    """Run one sub-pipeline, converting a hard step failure into a result entry.

    With fail-fast (continue_on_error=False), run_step raises _StepError so no
    later step in the sub-pipeline can run on top of an incomplete earlier one.
    """
    try:
        return fn(args, api_token)
    except _StepError as exc:
        print(
            f"\n[barrier] {name} sub-pipeline stopped after '{exc.name}' failed "
            f"(set --continue-on-error to continue past failures)",
            flush=True,
        )
        return {exc.name: False}


def _run_shared_post_steps(args, api_token):
    """Run the steps that depend on all sub-pipelines having finished.

    Settle/friendlies are league-dependent and still respect the fail-fast
    barrier. Cup tracking was moved to ``_run_cups_last`` so the cup steps can
    run even when an earlier sub-pipeline failed (see main()).
    """
    py = sys.executable
    sub = {}

    if not _tables_only():
        sub["settle_predictions"] = run_step(
            "Settle predictions with live/final results",
            [py, str(FILES_DIR / "Update_Live_Prediction_Results.py")],
            continue_on_error=args.continue_on_error,
        )
        sub["sync_club_friendlies"] = run_step(
            "Sync club friendlies schedule and Chelsea predictions",
            [py, str(FILES_DIR / "Update_Club_Friendlies.py")],
            continue_on_error=args.continue_on_error,
        )
    return sub


def _run_cups_last(args):
    """Run the cup steps last, after every league sub-pipeline.

    Order matters: ``Predict_Upcoming_Cups.py`` refreshes the upcoming cup
    predictions first, then ``Track_Cup_Results.py`` settles them and rebuilds
    the projected cup tables/brackets from the freshest data.

    Always runs — including under the shortened backend (``BTS_TABLES_ONLY=1``),
    which otherwise only projects league tables.

    Cups still run after league failures so outputs do not go stale, but cup
    step failures are never swallowed (audit #15): they always use fail-fast
    so a broken Predict/Track stops the cup sequence and marks the pipeline.
    """
    py = sys.executable
    sub = {}

    def _inner():
        out = {}
        if not args.skip_global:
            if _tables_only():
                print("[cups] tables-only / shortened backend: still running cup upcoming + track")
            # Always fail-fast for cups — do not inherit league continue_on_error.
            out["global_upcoming_cups"] = run_step(
                "[global] Upcoming cup predictions (last)",
                [py, str(FILES_DIR / "Predict_Upcoming_Cups.py"), "--window-days", str(args.cup_window_days)],
                continue_on_error=False,
                timeout=3600,
            )
            out["track_cup_results"] = run_step(
                "Track completed cup predictions and cup projections",
                [py, str(FILES_DIR / "Track_Cup_Results.py")],
                continue_on_error=False,
                timeout=1800,
            )
        return out

    try:
        return _inner()
    except _StepError as exc:
        print(
            f"\n[barrier] cup steps stopped after '{exc.name}' failed "
            f"(cup failures are never ignored)",
            flush=True,
        )
        key = "global_upcoming_cups"
        lowered = str(exc.name or "").lower()
        if "track" in lowered:
            key = "track_cup_results"
        sub[key] = False
        # Still surface any steps that completed before the failure.
        if key == "track_cup_results" and "global_upcoming_cups" not in sub:
            # upcoming may have succeeded before track failed — unknown here;
            # leave only the failed key so summary is honest.
            pass
        return sub


def _check_dependencies():
    """Verify required modules are importable and log their versions."""
    required = {
        "pandas": "pd",
        "numpy": "np",
        "sklearn": "scikit-learn",
        "joblib": "joblib",
        "requests": "requests",
        "bs4": "beautifulsoup4",
    }
    print("\n--- Pre-flight dependency check ---")
    all_ok = True
    for mod_name, pkg_name in required.items():
        try:
            mod = __import__(mod_name)
            ver = getattr(mod, "__version__", "unknown")
            print(f"  [OK] {pkg_name} {ver}")
        except ImportError:
            print(f"  [MISSING] {pkg_name} ({mod_name})")
            all_ok = False
    if not all_ok:
        print("  [WARN] Some dependencies are missing; pipeline may fail.")
    print(f"  Python {sys.version.split()[0]} on {sys.platform}")
    print("--- End pre-flight check ---\n")


def _is_placeholder_game(r):
    """Return True if a game dict is a placeholder (not a real match)."""
    for key in ("home_team", "away_team"):
        val = str(r.get(key, "")).lower()
        if "group" in val or "third place" in val or "winner" in val or "runner" in val:
            return True
    return False


def _archive_completed_games():
    """Archive today's upcoming API rows into past_games.json after pipeline settle."""
    website_dir = SP_DIR / "Website"
    if str(website_dir) not in sys.path:
        sys.path.insert(0, str(website_dir))
    try:
        from predictions import archive_todays_games_to_past_games_file

        archive_todays_games_to_past_games_file()
    except Exception as exc:
        print(f"  [past-games] Archive failed: {exc}")
        import traceback
        traceback.print_exc()


_REAL_STANDINGS_FILE = _paths.STANDINGS_CACHE_FILE

# ── Competitions that are leagues (not cups / national) ──────────────
_REAL_TABLE_COMPETITIONS = {
    "England/Premier League", "England/Championship",
    "Spain/La Liga", "Spain/La Liga 2",
    "Italy/Serie A", "Italy/Serie B",
    "Germany/Bundesliga", "Germany/Bundesliga 2",
    "France/Ligue 1", "France/Ligue 2",
    "Portugal/Liga Portugal", "Netherlands/Eredivisie",
    "United States/MLS",
    "Mexico/Liga MX",
    "Belgium/First Division A", "Scotland/Premiership", "Turkey/Super Lig",
    "Austria/Bundesliga",
    "Greece/Super League", "Norway/Eliteserien",
    "Romania/Liga I", "Sweden/Allsvenskan",
    "Poland/Ekstraklasa",
}

_CUP_COMPETITIONS = {
    "England/FA Cup", "England/League Cup",
    "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
    "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
    "Italy/Coppa Italia", "Spain/Copa del Rey",
    "Germany/DFB-Pokal", "France/Coupe de France",
    "United States/US Open Cup",
    "North America/Leagues Cup",
}


def _build_real_standings():
    """Build real league tables using competition-aware Website standings logic."""
    website_dir = SP_DIR / "Website"
    if str(website_dir) not in sys.path:
        sys.path.insert(0, str(website_dir))

    try:
        from standings import (
            _build_fallback_standings,
            _compute_standings_from_history,
            _sanitize_real_standings,
        )
        from competition_rules import MLS_TABLE_VIEWS
    except ImportError as exc:
        print(f"  [real-standings] Could not import Website standings: {exc}")
        return False

    known_comps = sorted(_REAL_TABLE_COMPETITIONS | _CUP_COMPETITIONS | {"International/World Cup"})
    standings: dict[str, dict] = {}
    for comp_name in known_comps:
        # Daily refresh: CSV-backed history compute + always sanitize so ESPN
        # aliases cannot persist as duplicate rows beside football-data canons.
        table = _compute_standings_from_history(comp_name)
        if table:
            standings[comp_name] = _sanitize_real_standings(table, comp_name) or table
            continue
        fallback = _build_fallback_standings(comp_name)
        if fallback:
            standings[comp_name] = _sanitize_real_standings(fallback, comp_name) or fallback

    for alias in MLS_TABLE_VIEWS:
        if alias in standings:
            continue
        sub = _compute_standings_from_history(alias)
        if sub:
            standings[alias] = _sanitize_real_standings(sub, alias) or sub
        else:
            fallback = _build_fallback_standings(alias)
            if fallback:
                standings[alias] = _sanitize_real_standings(fallback, alias) or fallback

    if standings:
        try:
            _REAL_STANDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _REAL_STANDINGS_FILE.write_text(
                json.dumps(standings, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"  [real-standings] Built and saved {len(standings)} competition(s)")
        except Exception as exc:
            print(f"  [real-standings] Failed to save: {exc}")
            return False
    else:
        print("  [real-standings] No standings computed.")
        return False
    return True


def run_full_pipeline(args, api_token, results=None):
    """Run every pipeline step and record success/failure in `results`.

    Execution order:
    1. **Real standings** — build from completed CSV results (pre-pipeline)
    2. **Sub-pipelines** — global first, MLS next, other leagues (extra) last,
       always sequentially. Only one training run or table projection is ever
       active, and each heavy step gets every CPU except one (kept free for the
       backend's live score polling and API/file serving).
    3. **Post-pipeline steps** — settle, friendlies, track cups
    4. **Cups last** — upcoming cup predictions, after every league sub-pipeline

    Args:
        args: parsed CLI args from `parse_args()`.
        api_token: football-data.org token (or empty string).
        results: optional dict to accumulate step results into. If None, a
            new dict is created. Keys are step names; values are bools.

    Returns:
        The `results` dict (mapping step name -> True/False).
    """
    if results is None:
        results = {}
    global _pipeline_start_global
    _pipeline_start_global = time.monotonic()
    py = sys.executable  # noqa: F841  (kept for backwards-compat with external callers)

    _check_dependencies()

    ram_gb = _system_ram_gb()
    print(
        f"[INFO] host: {sys.platform} | detected RAM: "
        + (f"~{ram_gb:.0f}GB" if ram_gb else "unknown (set BTS_RAM_GB to override)")
        + f" | CPUs: {os.cpu_count() or 'unknown'}"
    )

    # ── Pre-pipeline: build real standings from completed games ──
    results["build_real_standings"] = _build_real_standings()

    # Sub-pipelines ALWAYS run sequentially: global first, MLS next, then the
    # other leagues (extra). Only one training run or table projection is ever
    # active, so the whole machine (minus one core held for the backend's live
    # polling and API/file serving) works on that single build.
    requested_workers = max(1, min(
        int(getattr(args, "workers", 1) or 1), MAX_SUBPIPELINE_WORKERS
    ))
    if requested_workers > 1:
        print(
            f"[NOTE] --workers {requested_workers} ignored: sub-pipelines always run "
            "sequentially (global -> MLS -> extra) so global/MLS/extra training or "
            "table projections never run at the same time."
        )
    sub_tasks = []
    if not args.skip_global:
        sub_tasks.append(("global", _run_global_subpipeline))
    if not args.skip_mls:
        sub_tasks.append(("mls", _run_mls_subpipeline))
    if not args.skip_extra:
        sub_tasks.append(("extra", _run_extra_subpipeline))

    pipeline_start = time.monotonic()

    # Sequential only (no ProcessPoolExecutor across sub-pipelines).
    for name, fn in sub_tasks:
        sub_start = time.monotonic()
        print(f"\n>>> Running {name} sub-pipeline (sequential)")
        sub_result = _run_sub_pipeline(name, fn, args, api_token)
        results.update(sub_result)
        elapsed = time.monotonic() - sub_start
        if sub_result and not all(sub_result.values()):
            failed_steps = [k for k, v in sub_result.items() if not v]
            print(f"  [WARN] {name} sub-pipeline had {len(failed_steps)} failed step(s): {failed_steps}")
        print(f"  [TIMING] {name} sub-pipeline: {elapsed:.1f}s")
        if sub_result and not all(sub_result.values()) and not args.continue_on_error:
            print(
                "\n[barrier] not starting remaining sub-pipelines: "
                f"'{name}' failed (set --continue-on-error to continue past failures)"
            )
            break

    # Ensure tables on disk even if a sub-pipeline child died before reaching
    # its projection step (OOM / uncaught executor exception on the host).
    results.update(_ensure_projected_tables(args))

    if any(not v for v in results.values()) and not args.continue_on_error:
        print(
            "\n[barrier] not starting league-dependent post-pipeline steps: an earlier step failed "
            "(set --continue-on-error to continue past failures)"
        )
        print("[cups] running cup steps anyway so a league failure cannot leave cup outputs stale")
    else:
        # Post-pipeline steps (depend on all sub-pipelines' outputs being on disk).
        post_start = time.monotonic()
        print("\n>>> Running post-pipeline steps")
        results.update(_run_shared_post_steps(args, api_token))

        # Archive completed games AFTER settle (so CSVs have actual_result filled).
        if not _tables_only():
            print("\n=== [past-games] Archive completed games to past_games.json ===")
            _archive_completed_games()
        else:
            print("\n[tables-only] Skipped settle/friendlies/archive steps")
        print(f"  [TIMING] post-pipeline steps: {time.monotonic() - post_start:.1f}s")

    # Cups last, and ALWAYS: after every league sub-pipeline, even when an
    # earlier step failed OR when the shortened backend is in tables-only mode
    # (``BTS_TABLES_ONLY=1``). Tables-only skips download/train/settle for
    # leagues, but cup upcoming + Track_Cup_Results must still refresh
    # ``Output/Predictions/cups/`` so brackets/tables stay current.
    print("\n>>> Running cup predictions (last)")
    cups_start = time.monotonic()
    results.update(_run_cups_last(args))
    print(f"  [TIMING] cup predictions: {time.monotonic() - cups_start:.1f}s")
    print(f"\n[TIMING] full pipeline: {time.monotonic() - pipeline_start:.1f}s")
    print(f"[DEBUG] pipeline wall clock done at T+{time.monotonic() - _pipeline_start_global:.0f}s")

    # Print step summary
    print("\n--- Pipeline Step Summary ---")
    passed = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if not v)
    skipped = sum(1 for k in results.keys() if k.endswith("_failed"))
    for step_name, ok in sorted(results.items()):
        status = "[OK]" if ok else "[FAIL]"
        print(f"  {status} {step_name}")
    print(f"  Total: {len(results)} steps, {passed} passed, {failed} failed"
          + (f" ({skipped} skipped)" if skipped else ""))
    print("--- End Summary ---\n")
    _tables_diag = _paths.OUTPUT_PRED_SHARED / "projected_tables_diagnostics.json"
    if _tables_diag.exists():
        print(f"[tables] full health report: {_tables_diag}")

    _write_pipeline_status(results)
    return results


def _write_pipeline_timestamp() -> None:
    """Write last_refresh.json so /api/last-refresh is current even if
    the parent process (gunicorn/BackendServer) crashed mid-pipeline."""
    try:
        LAST_REFRESH_FILE.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).replace(microsecond=0)
        LAST_REFRESH_FILE.write_text(
            json.dumps({"last_refresh_utc": now.isoformat()}), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[WARN] Could not write {LAST_REFRESH_FILE}: {exc}")


def _write_pipeline_status(results: dict) -> None:
    """Write pipeline step results to Output/Status/pipeline_status.json for the API."""
    try:
        now = datetime.now(UTC).replace(microsecond=0)
        passed = sum(1 for v in results.values() if v)
        failed = sum(1 for v in results.values() if not v)
        failed_steps = sorted(k for k, v in results.items() if not v)
        log_stats = pipeline_log.log_stats()
        log_snapshot = pipeline_log.read_log(tail=2000, level="notable", highlights_limit=80)
        PIPELINE_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PIPELINE_STATUS_FILE.write_text(
            json.dumps({
                "finished_utc": now.isoformat(),
                "total_steps": len(results),
                "passed": passed,
                "failed": failed,
                "ok": failed == 0,
                "failed_steps": failed_steps,
                "steps": {k: bool(v) for k, v in sorted(results.items())},
                "log_file": log_stats.get("log_file"),
                "log_bytes": log_stats.get("bytes", 0),
                "log_lines": log_stats.get("lines", 0),
                "log_highlights": log_snapshot.get("highlights", []),
            }, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[WARN] Could not write {PIPELINE_STATUS_FILE}: {exc}")



def _rebuild_league_data_caches_after_pipeline():
    """Clear sticky LeagueData caches and rebuild from fresh pipeline outputs."""
    website_dir = SP_DIR / "Website"
    if str(website_dir) not in sys.path:
        sys.path.insert(0, str(website_dir))
    try:
        from league_data import rebuild_league_data_caches

        rebuild_league_data_caches(clear_first=True)
    except Exception as exc:
        print(f"[WARN] league-data cache rebuild failed: {exc}")
        import traceback
        traceback.print_exc()


def _rebuild_cup_data_caches_after_pipeline():
    """Clear sticky CupData caches and rebuild from fresh cup projections."""
    website_dir = SP_DIR / "Website"
    if str(website_dir) not in sys.path:
        sys.path.insert(0, str(website_dir))
    try:
        from cup_data import rebuild_cup_data_caches

        rebuild_cup_data_caches(clear_first=True)
    except Exception as exc:
        print(f"[WARN] cup-data cache rebuild failed: {exc}")
        import traceback
        traceback.print_exc()


def main():
    args = parse_args()
    api_token = load_api_token()
    tee = pipeline_log.activate_stdout_tee(trigger="cli")
    try:
        run_full_pipeline(args, api_token)
        _write_pipeline_timestamp()
        # Refresh Output/ Europe/LeagueResult (and other published trees) so the
        # website backends and artifacts pick up fresh projections. Daily_Pipeline
        # does this itself after each loop; the standalone CLI would otherwise
        # leave Output/ stale relative to Output/Predictions/*.csv.
        try:
            if str(MAIN_DIR) not in sys.path:
                sys.path.insert(0, str(MAIN_DIR))
            from Daily_Pipeline import publish_to_output
            publish_to_output()
        except Exception as exc:
            print(f"[WARN] publish_to_output failed: {exc}")
        _rebuild_league_data_caches_after_pipeline()
        _rebuild_cup_data_caches_after_pipeline()
        print("\nPipeline complete.")
    finally:
        pipeline_log.deactivate_stdout_tee()


if __name__ == "__main__":
    main()
