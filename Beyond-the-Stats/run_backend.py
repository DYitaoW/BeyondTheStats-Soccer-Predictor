"""
run_backend.py -- the single entry point for the persistent backend.

Boots the Flask web server, the daily pipeline scheduler, the future-games
watcher, and the memory monitor in one process tree, and stays running
until SIGINT / SIGTERM. This is what you launch on the host that powers
the website (and the future iOS app feed).

Usage:
    python Beyond-the-Stats/run_backend.py                     # full backend
    python Beyond-the-Stats/run_backend.py --no-website        # scheduler + watcher only
    python Beyond-the-Stats/run_backend.py --host 127.0.0.1    # local-only
    python Beyond-the-Stats/run_backend.py --port 8080         # custom port
    python Beyond-the-Stats/run_backend.py --workers 3         # pipeline parallelism
    python Beyond-the-Stats/run_backend.py --memory-limit-gb 8 # tighter cap
    python Beyond-the-Stats/run_backend.py --no-run-on-start   # wait for the 2am ET run
    python Beyond-the-Stats/run_backend.py --refresh-time 03:30 --timezone America/New_York

What "multiple cores" means here:
  - Sub-pipelines (global, MLS, extra) run concurrently via
    ``ProcessPoolExecutor`` -- three Python processes for the duration of
    a pipeline run. This is what ``--workers 3`` (the default) enables.
  - Per-competition parallelism inside ``Project_League_Table.py`` is
    forwarded via ``--competition-workers``. The default of 0 picks
    ``min(CPU_count, 4)`` automatically.
  - The Flask server uses ``gunicorn`` workers on POSIX (auto-falls back
    to the threaded dev server on Windows). Workers are capped at 4 so
    the pipeline processes retain headroom.

What "stay under 12 GB" means here:
  - ``MemoryMonitor`` polls the process group RSS every 30 s. When it
    exceeds 90 % of the limit it logs a warning; when it hits 100 % it
    pauses the running pipeline subprocess (SIGTERM, then SIGKILL after
    30 s of grace). 12 GB is enough for OS + Flask + pandas + the
    pipeline's peak (3 GB during WC projection).

What "update for future games" means here:
  - ``FutureGamesWatcher`` polls the upcoming-fixture CSVs every 5 min
    (configurable). When one changes (new fixtures added in the source
    feed), it runs the cheap ``Predict_Upcoming_Matchweek.py`` script
    for each sub-pipeline so newly added games appear in the website +
    iOS feed within minutes, without waiting for the 2am ET full run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SP_DIR = PROJECT_ROOT / "Beyond-the-Stats"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Persistent backend: Flask web server + daily pipeline scheduler + "
            "future-games watcher + memory monitor. Runs until SIGINT/SIGTERM."
        )
    )

    # Website
    parser.add_argument(
        "--no-website",
        action="store_true",
        help="Don't start the Flask web server (scheduler + watcher only).",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Flask bind host (default 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Flask bind port (default 5000).",
    )
    parser.add_argument(
        "--no-gunicorn",
        action="store_true",
        help="Disable gunicorn even on POSIX (use Flask dev server).",
    )

    # Pipeline schedule
    parser.add_argument(
        "--refresh-time",
        type=str,
        default="02:00",
        help="Daily wall-clock refresh time HH:MM (default 02:00).",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default="America/New_York",
        help="IANA timezone for --refresh-time (default America/New_York).",
    )
    parser.add_argument(
        "--no-run-on-start",
        action="store_true",
        help="Don't run the full pipeline at startup; wait for the scheduled tick.",
    )

    # Pipeline parallelism
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help=(
            "Number of sub-pipelines (global/MLS/extra) to run concurrently. "
            "1 = sequential, 3 = all in parallel (default). Max 3."
        ),
    )
    parser.add_argument(
        "--competition-workers",
        type=int,
        default=0,
        help=(
            "Per-competition worker count passed to Project_League_Table.py. "
            "0 = auto (min(CPU_count, 4)). 1 = serial."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=365,
        help="Fixture window days for upcoming-matchweek predictions.",
    )
    parser.add_argument(
        "--national-window-days",
        type=int,
        default=90,
        help="Fixture window days for national-team / WC predictions.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        default=True,
        help="Continue remaining pipeline steps if one step fails (default true).",
    )
    parser.add_argument(
        "--no-continue-on-error",
        dest="continue_on_error",
        action="store_false",
        help="Stop the pipeline on the first error.",
    )
    parser.add_argument(
        "--skip-mls",
        action="store_true",
        help="Skip the MLS sub-pipeline.",
    )
    parser.add_argument(
        "--skip-extra",
        action="store_true",
        help="Skip the extra-leagues sub-pipeline.",
    )
    parser.add_argument(
        "--skip-global",
        action="store_true",
        help="Skip the European/global sub-pipeline.",
    )

    # Future-games watcher
    parser.add_argument(
        "--no-watcher",
        action="store_true",
        help="Disable the future-games watcher (rely on the 2am ET full run only).",
    )
    parser.add_argument(
        "--watcher-interval",
        type=int,
        default=300,
        help="Watcher poll interval in seconds (default 300 = 5 min).",
    )

    # Memory limit
    parser.add_argument(
        "--memory-limit-gb",
        type=float,
        default=12.0,
        help="Hard RAM cap in GB for the process group (default 12).",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if str(SP_DIR) not in sys.path:
        sys.path.insert(0, str(SP_DIR))

    from Backend.server import BackendConfig, BackendServer, configure_logging

    # Sanity-check the refresh-time so we fail fast instead of in the scheduler.
    refresh_time = args.refresh_time.strip()
    if refresh_time:
        try:
            hour_str, minute_str = refresh_time.split(":", 1)
            assert 0 <= int(hour_str) < 24
            assert 0 <= int(minute_str) < 60
        except (ValueError, AssertionError):
            print(f"[ERROR] --refresh-time {refresh_time!r} is not HH:MM", file=sys.stderr)
            return 2

    workers = max(1, min(int(args.workers), 3))
    cpu_count = os.cpu_count() or 1
    # When sub-pipelines already run in parallel, keep competition workers low
    # so Global+MLS+Extra projection pools do not oversubscribe the host.
    if args.competition_workers <= 0:
        competition_workers = max(1, min(2 if workers > 1 else 4, cpu_count))
        if workers > 1:
            competition_workers = max(1, min(competition_workers, cpu_count // workers or 1))
    else:
        competition_workers = max(1, int(args.competition_workers))
        if workers > 1:
            competition_workers = max(1, min(competition_workers, 2))

    config = BackendConfig(
        serve_website=not args.no_website,
        host=args.host,
        port=args.port,
        use_gunicorn=not args.no_gunicorn,
        daily_refresh_hour=int(refresh_time.split(":")[0]) if refresh_time else 2,
        daily_refresh_minute=int(refresh_time.split(":")[1]) if refresh_time else 0,
        daily_refresh_tz=args.timezone,
        pipeline_workers=workers,
        pipeline_competition_workers=competition_workers,
        pipeline_window_days=args.window_days,
        pipeline_national_window_days=args.national_window_days,
        pipeline_continue_on_error=args.continue_on_error,
        pipeline_skip_mls=args.skip_mls,
        pipeline_skip_extra=args.skip_extra,
        pipeline_skip_global=args.skip_global,
        run_on_start=not args.no_run_on_start,
        enable_watcher=not args.no_watcher,
        watcher_interval_s=float(args.watcher_interval),
        memory_limit_gb=float(args.memory_limit_gb),
    )

    configure_logging()
    server = BackendServer(config)
    try:
        server.run_forever()
    except Exception as exc:
        import logging

        logging.getLogger("backend").exception("Backend crashed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
