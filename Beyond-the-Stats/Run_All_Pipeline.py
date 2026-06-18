import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


SP_DIR = Path(__file__).resolve().parent
ROOT_DIR = SP_DIR.parent
FILES_DIR = SP_DIR / "files"
MLS_FILES_DIR = SP_DIR / "MLS" / "files"
EXTRA_FILES_DIR = SP_DIR / "Extra-leagues" / "files"
LOCAL_KEYS_FILE = FILES_DIR / "local_api_keys.json"

DEFAULT_SUBPIPELINE_WORKERS = 3
MAX_SUBPIPELINE_WORKERS = 3


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


def run_step(name, cmd, continue_on_error=False, input_text=None):
    print(f"\n=== {name} ===")
    print(" ".join(str(c) for c in cmd))
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT_DIR),
            text=True,
            input=input_text,
            check=False,
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"[ERROR] {name}: {exc} (after {elapsed:.1f}s)")
        if continue_on_error:
            return False
        raise

    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        print(f"[ERROR] {name} failed with exit code {proc.returncode} (after {elapsed:.1f}s)")
        if continue_on_error:
            return False
        raise SystemExit(proc.returncode)

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
    )
    sub["global_process_data"] = run_step(
        "[global] Process data",
        [py, str(FILES_DIR / "Process_Data.py")],
        continue_on_error=args.continue_on_error,
    )
    sub["global_sort_data"] = run_step(
        "[global] Sort data",
        [py, str(FILES_DIR / "Sort_Data.py")],
        continue_on_error=args.continue_on_error,
    )
    if not args.skip_model_train:
        sub["global_build_model_cache"] = run_step(
            "[global] Build model cache (non-interactive)",
            [py, str(FILES_DIR / "Predict_Match.py"), "--build-cache-only"],
            continue_on_error=args.continue_on_error,
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
        [py, str(FILES_DIR / "Predict_Upcoming_Cups"), "--window-days", str(args.window_days)],
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
    )
    if not args.skip_model_train:
        sub["mls_build_model_cache"] = run_step(
            "[mls] Build model cache (non-interactive)",
            [py, str(MLS_FILES_DIR / "Predict_Match.py")],
            continue_on_error=args.continue_on_error,
            input_text="n\nq\n",
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
    """Run the Extra-leagues sub-pipeline. Returns dict of results."""
    py = sys.executable
    sub = {}
    comp_workers = int(getattr(args, "competition_workers", 0) or 0)

    sub["extra_download_process_sort"] = run_step(
        "[extra] Download/process/sort latest data",
        [py, str(EXTRA_FILES_DIR / "Download_Latest_Data.py")],
        continue_on_error=args.continue_on_error,
    )
    if not args.skip_model_train:
        sub["extra_build_model_cache"] = run_step(
            "[extra] Build model cache (non-interactive)",
            [py, str(EXTRA_FILES_DIR / "Predict_Match.py")],
            continue_on_error=args.continue_on_error,
            input_text="n\nq\n",
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
    py = sys.executable  # noqa: F841  (kept for backwards-compat with external callers)

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
            print(f"  [TIMING] {name} sub-pipeline: {time.monotonic() - sub_start:.1f}s")
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
                except Exception as exc:
                    print(f"[ERROR] Sub-pipeline '{name}' failed: {exc}")
                    if not args.continue_on_error:
                        raise
                    sub_result = {f"{name}_failed": False}
                results.update(sub_result)
                print(f"  [OK] {name} sub-pipeline finished")

    # Post-pipeline steps (depend on all sub-pipelines' outputs being on disk).
    post_start = time.monotonic()
    print("\n>>> Running post-pipeline steps")
    results.update(_run_shared_post_steps(args, api_token))
    print(f"  [TIMING] post-pipeline steps: {time.monotonic() - post_start:.1f}s")
    print(f"\n[TIMING] full pipeline: {time.monotonic() - pipeline_start:.1f}s")

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

    return results


def main():
    args = parse_args()
    api_token = load_api_token()
    run_full_pipeline(args, api_token)
    print("\nPipeline complete.")


if __name__ == "__main__":
    main()
