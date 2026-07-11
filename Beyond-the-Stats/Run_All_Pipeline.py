"""
Full pipeline orchestrator — runs all data, model, and prediction steps.

Invoked by ``Daily_Pipeline.py`` (scheduled) or directly from the command
line.  Also triggered by the ``/api/refresh`` endpoint on the Flask server.

Execution order
---------------
1. **Sub-pipelines** (global / MLS / extra) — run in parallel if ``--workers > 1``
   Each sub-pipeline runs sequentially: Download → Process → Sort → Model Cache
   → Predict Upcoming → Project League Table → (cups / national team / WC)
2. **Post-pipeline steps** (sequential, after all sub-pipelines finish):
   Settle predictions (update CSVs with real results from ESPN)
   Track cup results
   Update website accuracy history

Flags
-----
``--skip-global / --skip-mls / --skip-extra`` — skip entire sub-pipelines
``--skip-model-train`` — skip model retraining on light refresh days; still builds
  the cache automatically when the file is missing or unloadable. Full retrains
  run on Tuesday and Friday via the backend scheduler.
``--continue-on-error`` — keep going even if individual steps fail (default: true)
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

import pipeline_log
import model_cache_util
import season_calendar


SP_DIR = Path(__file__).resolve().parent
ROOT_DIR = SP_DIR.parent
FILES_DIR = SP_DIR / "files"
MLS_FILES_DIR = SP_DIR / "MLS" / "files"
EXTRA_FILES_DIR = SP_DIR / "Extra-leagues" / "files"
LOCAL_KEYS_FILE = FILES_DIR / "local_api_keys.json"

DEFAULT_SUBPIPELINE_WORKERS = 3
MAX_SUBPIPELINE_WORKERS = 3

LAST_REFRESH_FILE = SP_DIR / "Data" / "last_refresh.json"
PIPELINE_STATUS_FILE = SP_DIR / "Data" / "pipeline_status.json"

# Upcoming CSV paths for archival to past_games.json
GLOBAL_UPCOMING_FILE = SP_DIR / "Data" / "Predictions" / "upcoming_matchweek_predictions.csv"
MLS_UPCOMING_FILE = SP_DIR / "MLS" / "Data" / "Predictions" / "upcoming_matchweek_predictions.csv"
EXTRA_UPCOMING_FILE = SP_DIR / "Extra-leagues" / "Data" / "Predictions" / "upcoming_matchweek_predictions.csv"
CUP_UPCOMING_FILE = SP_DIR / "Data" / "Predictions" / "upcoming_cup_predictions.csv"
NATIONAL_UPCOMING_FILE = SP_DIR / "Data" / "Predictions" / "upcoming_national_team_predictions.csv"
PAST_GAMES_FILE = SP_DIR / "Data" / "Predictions" / "past_games.json"

# Monotonic timestamp set by run_full_pipeline so run_step can log elapsed time.
_pipeline_start_global: float = 0.0


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
            f"Number of sub-pipelines (global/MLS/extra) to run concurrently via ProcessPoolExecutor. "
            f"1 = sequential, 3 = run all three sub-pipelines in parallel (default), up to {MAX_SUBPIPELINE_WORKERS} = parallel. "
            f"Per-step multithreading (e.g. Project_League_Table competitions) is controlled by "
            f"--competition-workers."
        ),
    )
    parser.add_argument(
        "--competition-workers",
        type=int,
        default=0,
        help=(
            "Worker count passed to Project_League_Table.py for per-competition parallel projection. "
            "0 = auto (min(CPU_count, 4)); 1 = serial; N = use N processes."
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


def run_step(name, cmd, continue_on_error=False, input_text=None, timeout=None):
    print(f"\n=== {name} ===")
    print(" ".join(str(c) for c in cmd))
    started = time.monotonic()
    print(f"[DEBUG] run_step starting '{name}' at T+{started - _pipeline_start_global:.0f}s "
          f"timeout={timeout}s")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT_DIR),
            text=True,
            input=input_text,
            check=False,
            timeout=timeout,
            capture_output=True,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        print(f"[TIMEOUT] {name} exceeded {timeout}s timeout (after {elapsed:.1f}s)")
        # Kill the hung subprocess — subprocess.run does NOT do this automatically.
        try:
            exc.process.kill()
            exc.process.wait(timeout=5)
        except Exception:
            pass
        print(f"  → Skipping (continue_on_error={continue_on_error})")
        return False
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"[ERROR] {name}: {exc} (after {elapsed:.1f}s)")
        print(f"  → Skipping (continue_on_error={continue_on_error})")
        return False

    elapsed = time.monotonic() - started
    if proc.stdout:
        print(proc.stdout, end="" if str(proc.stdout).endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, end="" if str(proc.stderr).endswith("\n") else "\n")
    print(f"[DEBUG] run_step finished '{name}' rc={proc.returncode} elapsed={elapsed:.1f}s "
          f"at T+{time.monotonic() - _pipeline_start_global:.0f}s")
    if proc.returncode != 0:
        print(f"[ERROR] {name} failed with exit code {proc.returncode} (after {elapsed:.1f}s)")
        print(f"  → Skipping (continue_on_error={continue_on_error})")
        return False

    print(f"[OK] {name} ({elapsed:.1f}s)")
    return True


def _project_table_cmd(files_dir, comp_workers, base_name):
    """Build the Project_League_Table command with --competition-workers when >0."""
    cmd = [sys.executable, str(files_dir / base_name)]
    if comp_workers and comp_workers > 0:
        cmd += ["--competition-workers", str(comp_workers)]
    return cmd


def _run_global_subpipeline(args, api_token):
    """Run all global (European) + national + WC steps. Returns dict of results."""
    py = sys.executable
    sub = {}
    comp_workers = int(getattr(args, "competition_workers", 0) or 0)

    sub["global_download_latest_data"] = run_step(
        "[global] Download latest data",
        [py, str(FILES_DIR / "Download_Latest_Data.py")],
        continue_on_error=args.continue_on_error,
        timeout=1200,
    )
    sub["global_process_data"] = run_step(
        "[global] Process data",
        [py, str(FILES_DIR / "Process_Data.py")],
        continue_on_error=args.continue_on_error,
        timeout=1800,
    )
    sub["global_sort_data"] = run_step(
        "[global] Sort data",
        [py, str(FILES_DIR / "Sort_Data.py")],
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
    )
    sub["global_projected_league_tables"] = run_step(
        "[global] Projected league tables",
        _project_table_cmd(FILES_DIR, comp_workers, "Project_League_Table.py"),
        continue_on_error=args.continue_on_error,
    )
    sub["global_upcoming_cups"] = run_step(
        "[global] Upcoming cup predictions",
        [py, str(FILES_DIR / "Predict_Upcoming_Cups"), "--window-days", str(args.cup_window_days)],
        continue_on_error=args.continue_on_error,
    )
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
    return sub


def _run_mls_subpipeline(args, api_token):
    """Run the MLS sub-pipeline. Returns dict of results."""
    py = sys.executable
    sub = {}
    comp_workers = int(getattr(args, "competition_workers", 0) or 0)

    mls_dl_cmd = [py, str(MLS_FILES_DIR / "Download_Latest_Data.py")]
    if args.skip_model_train:
        mls_dl_cmd.append("--skip-squad-values")
    sub["mls_download_process_sort"] = run_step(
        "[mls] Download/process/sort latest data",
        mls_dl_cmd,
        continue_on_error=args.continue_on_error,
        timeout=1200,
    )
    sub["mls_build_model_cache"] = run_step(
        "[mls] Build model cache (non-interactive)",
        [py, str(MLS_FILES_DIR / "Predict_Match.py")],
        continue_on_error=args.continue_on_error,
        input_text="n\nq\n",
        timeout=3600,
    )
    mls_upcoming_cmd = [py, str(MLS_FILES_DIR / "Predict_Upcoming_Matchweek.py"), "--window-days", str(args.window_days)]
    if api_token:
        mls_upcoming_cmd += ["--api-token", api_token]
    sub["mls_upcoming_matchweek"] = run_step(
        "[mls] Upcoming matchweek predictions",
        mls_upcoming_cmd,
        continue_on_error=args.continue_on_error,
    )
    sub["mls_projected_league_tables"] = run_step(
        "[mls] Projected league tables",
        _project_table_cmd(MLS_FILES_DIR, comp_workers, "Project_League_Table.py"),
        continue_on_error=args.continue_on_error,
    )
    return sub


def _run_extra_subpipeline(args, api_token):
    """Run the extra-leagues sub-pipeline (smaller European / S. American / Asian leagues)."""
    py = sys.executable
    sub = {}
    comp_workers = int(getattr(args, "competition_workers", 0) or 0)
    sub["extra_download_process_sort"] = run_step(
        "[extra] Download/process/sort latest data",
        [py, str(EXTRA_FILES_DIR / "Download_Latest_Data.py")],
        continue_on_error=args.continue_on_error,
        timeout=1200,
    )
    if _should_build_model_cache(args, "extra", EXTRA_FILES_DIR / "Predict_Match.py")[0]:
        sub["extra_build_model_cache"] = run_step(
            "[extra] Build model cache (non-interactive)",
            [py, str(EXTRA_FILES_DIR / "Predict_Match.py")],
            continue_on_error=args.continue_on_error,
            input_text="n\nq\n",
            timeout=3600,
        )
    sub["extra_upcoming_matchweek"] = run_step(
        "[extra] Upcoming matchweek predictions",
        [py, str(EXTRA_FILES_DIR / "Predict_Upcoming_Matchweek.py"), "--window-days", str(args.window_days)],
        continue_on_error=args.continue_on_error,
    )
    sub["extra_projected_league_tables"] = run_step(
        "[extra] Projected league tables",
        _project_table_cmd(EXTRA_FILES_DIR, comp_workers, "Project_League_Table.py"),
        continue_on_error=args.continue_on_error,
    )
    return sub


def _run_shared_post_steps(args, api_token):
    """Run the steps that depend on all sub-pipelines having finished (settle, track, accuracy)."""
    py = sys.executable
    sub = {}

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
    if not args.skip_global:
        sub["track_cup_results"] = run_step(
            "Track completed cup predictions and cup projections",
            [py, str(FILES_DIR / "Track_Cup_Results.py")],
            continue_on_error=args.continue_on_error,
        )
    sub["update_website_accuracy_history"] = run_step(
        "Update website accuracy history",
        [
            py,
            "-c",
            (
                "import importlib.util; import sys; "
                "sys.path.insert(0, 'Beyond-the-Stats/Website'); "
                "p=r'Beyond-the-Stats/Website/app.py'; "
                "s=importlib.util.spec_from_file_location('webapp', p); "
                "m=importlib.util.module_from_spec(s); "
                "s.loader.exec_module(m); "
                "m.update_accuracy_history_files()"
            ),
        ],
        continue_on_error=args.continue_on_error,
    )
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


_REAL_STANDINGS_FILE = SP_DIR / "Data" / "standings_cache.json"

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
    "UEFA/Champions League", "UEFA/Europa League", "UEFA/Conference League",
    "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
    "Italy/Coppa Italia", "Spain/Copa del Rey",
    "Germany/DFB-Pokal", "France/Coupe de France",
    "United States/US Open Cup",
    "CONCACAF/Leagues Cup",
}


def _build_real_standings():
    """Build real league tables using competition-aware Website standings logic."""
    website_dir = SP_DIR / "Website"
    if str(website_dir) not in sys.path:
        sys.path.insert(0, str(website_dir))

    try:
        from standings import _build_fallback_standings, _compute_standings_from_history
        from competition_rules import MLS_TABLE_VIEWS
    except ImportError as exc:
        print(f"  [real-standings] Could not import Website standings: {exc}")
        return False

    known_comps = sorted(_REAL_TABLE_COMPETITIONS | _CUP_COMPETITIONS | {"FIFA/World Cup"})
    standings: dict[str, dict] = {}
    for comp_name in known_comps:
        table = _compute_standings_from_history(comp_name)
        if table:
            standings[comp_name] = table
            continue
        fallback = _build_fallback_standings(comp_name)
        if fallback:
            standings[comp_name] = fallback

    for alias in MLS_TABLE_VIEWS:
        if alias in standings:
            continue
        sub = _compute_standings_from_history(alias)
        if sub:
            standings[alias] = sub
        else:
            fallback = _build_fallback_standings(alias)
            if fallback:
                standings[alias] = fallback

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
    2. **Sub-pipelines** (global / MLS / extra) — in parallel if ``--workers > 1``
    3. **Post-pipeline steps** — settle, track cups, accuracy history

    When ``args.workers > 1`` the three sub-pipelines (global/MLS/extra) are
    scheduled concurrently via ``ProcessPoolExecutor``; their step order is
    preserved within each sub-pipeline, and the post-pipeline steps (settle,
    track, accuracy history) still run sequentially afterwards.

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

    # ── Pre-pipeline: build real standings from completed games ──
    results["build_real_standings"] = _build_real_standings()

    workers = max(1, min(int(getattr(args, "workers", 1) or 1), MAX_SUBPIPELINE_WORKERS))
    sub_tasks = []
    if not args.skip_global:
        sub_tasks.append(("global", _run_global_subpipeline))
    if not args.skip_mls:
        sub_tasks.append(("mls", _run_mls_subpipeline))
    if not args.skip_extra:
        sub_tasks.append(("extra", _run_extra_subpipeline))

    pipeline_start = time.monotonic()

    if workers == 1 or len(sub_tasks) <= 1:
        # Sequential: keeps the original behavior (no extra startup overhead).
        for name, fn in sub_tasks:
            sub_start = time.monotonic()
            print(f"\n>>> Running {name} sub-pipeline (sequential)")
            sub_result = fn(args, api_token)
            results.update(sub_result)
            elapsed = time.monotonic() - sub_start
            if sub_result and not all(sub_result.values()):
                failed_steps = [k for k, v in sub_result.items() if not v]
                print(f"  [WARN] {name} sub-pipeline had {len(failed_steps)} failed step(s): {failed_steps}")
            print(f"  [TIMING] {name} sub-pipeline: {elapsed:.1f}s")
    else:
        max_workers = min(workers, len(sub_tasks))
        print(f"\n>>> Running {len(sub_tasks)} sub-pipelines in parallel (max_workers={max_workers})")
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(fn, args, api_token): name
                for name, fn in sub_tasks
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    sub_result = fut.result()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as exc:
                    print(f"[ERROR] Sub-pipeline '{name}' failed: {exc}")
                    sub_result = {f"{name}_failed": False}
                results.update(sub_result)
                print(f"  [OK] {name} sub-pipeline finished")

    # Post-pipeline steps (depend on all sub-pipelines' outputs being on disk).
    post_start = time.monotonic()
    print("\n>>> Running post-pipeline steps")
    results.update(_run_shared_post_steps(args, api_token))

    # Archive completed games AFTER settle (so CSVs have actual_result filled).
    print("\n=== [past-games] Archive completed games to past_games.json ===")
    _archive_completed_games()

    print(f"  [TIMING] post-pipeline steps: {time.monotonic() - post_start:.1f}s")
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
    """Write pipeline step results to Data/pipeline_status.json for the API."""
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


def main():
    args = parse_args()
    api_token = load_api_token()
    tee = pipeline_log.activate_stdout_tee(trigger="cli")
    try:
        run_full_pipeline(args, api_token)
        _write_pipeline_timestamp()
        print("\nPipeline complete.")
    finally:
        pipeline_log.deactivate_stdout_tee()


if __name__ == "__main__":
    main()
