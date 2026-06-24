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
``--skip-model-train`` — skip model cache building; also implies ``--skip-squad-values``
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
        default=3,
        help="Fixture window days for upcoming matchweek scripts.",
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
        help="Skip model cache building (retraining) steps; only fetch data and update predictions.",
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

    # Global league steps commented out — no active league games (Jun 2026).
    # sub["global_download_latest_data"] = run_step(
    #     "[global] Download latest data",
    #     [py, str(FILES_DIR / "Download_Latest_Data.py")],
    #     continue_on_error=args.continue_on_error,
    #     timeout=1200,
    # )
    # sub["global_process_data"] = run_step(
    #     "[global] Process data",
    #     [py, str(FILES_DIR / "Process_Data.py")],
    #     continue_on_error=args.continue_on_error,
    #     timeout=1800,
    # )
    # sub["global_sort_data"] = run_step(
    #     "[global] Sort data",
    #     [py, str(FILES_DIR / "Sort_Data.py")],
    #     continue_on_error=args.continue_on_error,
    #     timeout=600,
    # )
    # if not args.skip_model_train:
    #     sub["global_build_model_cache"] = run_step(
    #         "[global] Build model cache (non-interactive)",
    #         [py, str(FILES_DIR / "Predict_Match.py"), "--build-cache-only"],
    #         continue_on_error=args.continue_on_error,
    #         timeout=3600,
    #     )
    # upcoming_cmd = [py, str(FILES_DIR / "Predict_Upcoming_Matchweek.py"), "--window-days", str(args.window_days)]
    # if api_token:
    #     upcoming_cmd += ["--api-token", api_token]
    # sub["global_upcoming_matchweek"] = run_step(
    #     "[global] Upcoming matchweek predictions",
    #     upcoming_cmd,
    #     continue_on_error=args.continue_on_error,
    # )
    # sub["global_projected_league_tables"] = run_step(
    #     "[global] Projected league tables",
    #     _project_table_cmd(FILES_DIR, comp_workers, "Project_League_Table.py"),
    #     continue_on_error=args.continue_on_error,
    # )
    # sub["global_upcoming_cups"] = run_step(
    #     "[global] Upcoming cup predictions",
    #     [py, str(FILES_DIR / "Predict_Upcoming_Cups"), "--window-days", str(args.window_days)],
    #     continue_on_error=args.continue_on_error,
    # )
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

    # MLS download commented out — ESPN returns 403 (no active games Jun 2026).
    # mls_dl_cmd = [py, str(MLS_FILES_DIR / "Download_Latest_Data.py")]
    # if args.skip_model_train:
    #     mls_dl_cmd.append("--skip-squad-values")
    # sub["mls_download_process_sort"] = run_step(
    #     "[mls] Download/process/sort latest data",
    #     mls_dl_cmd,
    #     continue_on_error=args.continue_on_error,
    #     timeout=1200,
    # )
    if not args.skip_model_train:
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
    """Extra-leagues steps commented out — no active league games (Jun 2026)."""
    # py = sys.executable
    # sub = {}
    # comp_workers = int(getattr(args, "competition_workers", 0) or 0)
    # sub["extra_download_process_sort"] = run_step(
    #     "[extra] Download/process/sort latest data",
    #     [py, str(EXTRA_FILES_DIR / "Download_Latest_Data.py")],
    #     continue_on_error=args.continue_on_error,
    #     timeout=1200,
    # )
    # if not args.skip_model_train:
    #     sub["extra_build_model_cache"] = run_step(
    #         "[extra] Build model cache (non-interactive)",
    #         [py, str(EXTRA_FILES_DIR / "Predict_Match.py")],
    #         continue_on_error=args.continue_on_error,
    #         input_text="n\nq\n",
    #         timeout=3600,
    #     )
    # sub["extra_upcoming_matchweek"] = run_step(
    #     "[extra] Upcoming matchweek predictions",
    #     [py, str(EXTRA_FILES_DIR / "Predict_Upcoming_Matchweek.py"), "--window-days", str(args.window_days)],
    #     continue_on_error=args.continue_on_error,
    # )
    # sub["extra_projected_league_tables"] = run_step(
    #     "[extra] Projected league tables",
    #     _project_table_cmd(EXTRA_FILES_DIR, comp_workers, "Project_League_Table.py"),
    #     continue_on_error=args.continue_on_error,
    # )
    # return sub
    return {}


def _run_shared_post_steps(args, api_token):
    """Run the steps that depend on all sub-pipelines having finished (settle, track, accuracy)."""
    py = sys.executable
    sub = {}

    sub["settle_predictions"] = run_step(
        "Settle predictions with live/final results",
        [py, str(FILES_DIR / "Update_Live_Prediction_Results.py")],
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
                "import importlib.util; "
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


def _archive_upcoming_to_past():
    """Archive completed/expired rows from all upcoming CSVs into past_games.json.

    Called as the first step of run_full_pipeline() before any sub-pipeline
    clears/updates the upcoming CSVs.  Also prunes past_games.json entries
    older than the rolling window (previous Thursday in Eastern Time).
    """
    today_et = (datetime.now(UTC) - timedelta(hours=4)).date()
    prev_thursday = today_et - timedelta(days=(today_et.weekday() - 3) % 7 + 7)
    cutoff_str = prev_thursday.isoformat()
    today_ts = pd.Timestamp(today_et)

    upcoming_files = [
        (GLOBAL_UPCOMING_FILE, "global"),
        (MLS_UPCOMING_FILE, "mls"),
        (EXTRA_UPCOMING_FILE, "extra"),
        (CUP_UPCOMING_FILE, "cups"),
        (NATIONAL_UPCOMING_FILE, "national"),
    ]

    new_rows = []
    seen_in_batch = set()

    for csv_path, mode in upcoming_files:
        if not csv_path.exists():
            continue
        try:
            df = pd.read_csv(csv_path, dtype=str)
        except Exception as exc:
            print(f"  [WARN] Could not read {csv_path}: {exc}")
            continue
        if df.empty or "match_date" not in df.columns:
            continue

        df["_parsed_date"] = pd.to_datetime(df["match_date"], errors="coerce")
        has_result = df.get("actual_result", pd.Series([""] * len(df))).isin(["H", "D", "A"])
        is_past = df["_parsed_date"].notna() & (df["_parsed_date"] < today_ts)
        qualified = df[is_past | has_result]

        for _, row in qualified.iterrows():
            entry = row.dropna().to_dict()
            entry.pop("_parsed_date", None)
            # Composite key for dedup: date|competition|home|away
            ck = "|".join(
                str(entry.get(k, "")).strip()
                for k in ("match_date", "competition", "home_team", "away_team")
            ).lower()
            if not ck or ck in seen_in_batch:
                continue
            seen_in_batch.add(ck)

            entry["source"] = f"pipeline_{mode}"
            # Map CSV actual_home_goals/actual_away_goals → home_score/away_score
            if "actual_home_goals" in entry and "home_score" not in entry:
                try:
                    entry["home_score"] = int(float(entry["actual_home_goals"]))
                except (ValueError, TypeError):
                    entry["home_score"] = None
            if "actual_away_goals" in entry and "away_score" not in entry:
                try:
                    entry["away_score"] = int(float(entry["actual_away_goals"]))
                except (ValueError, TypeError):
                    entry["away_score"] = None
            new_rows.append(entry)

    if not new_rows:
        print("  [archive] No completed/expired rows found in upcoming CSVs.")
        return

    # Load existing past_games.json
    if PAST_GAMES_FILE.exists():
        try:
            existing = json.loads(PAST_GAMES_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    else:
        existing = []

    existing_keys = set()
    for r in existing:
        ck = "|".join(
            str(r.get(k, "")).strip()
            for k in ("match_date", "competition", "home_team", "away_team")
        ).lower()
        if ck:
            existing_keys.add(ck)

    merged = list(existing)
    added = 0
    for r in new_rows:
        ck = "|".join(
            str(r.get(k, "")).strip()
            for k in ("match_date", "competition", "home_team", "away_team")
        ).lower()
        if ck not in existing_keys:
            existing_keys.add(ck)
            merged.append(r)
            added += 1

    # Prune rows older than the rolling window
    before = len(merged)
    merged = [r for r in merged if str(r.get("match_date", "")).strip() >= cutoff_str]
    pruned = before - len(merged)

    if added or pruned:
        PAST_GAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAST_GAMES_FILE.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  [archive] Added {added} rows, pruned {pruned} old rows "
              f"\u2192 past_games.json ({len(merged)} total)")
    else:
        print("  [archive] No changes to past_games.json")


def run_full_pipeline(args, api_token, results=None):
    """Run every pipeline step and record success/failure in `results`.

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

    # Step 0: Archive completed/expired rows from upcoming CSVs before any
    # sub-pipeline clears them out.
    print("\n=== [archive] Archive completed/expired rows to past_games.json ===")
    _archive_upcoming_to_past()

    _check_dependencies()

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
        PIPELINE_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PIPELINE_STATUS_FILE.write_text(
            json.dumps({
                "finished_utc": now.isoformat(),
                "total_steps": len(results),
                "passed": passed,
                "failed": failed,
                "steps": {k: bool(v) for k, v in sorted(results.items())},
            }, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[WARN] Could not write {PIPELINE_STATUS_FILE}: {exc}")


def main():
    args = parse_args()
    api_token = load_api_token()
    run_full_pipeline(args, api_token)
    _write_pipeline_timestamp()
    print("\nPipeline complete.")


if __name__ == "__main__":
    main()
