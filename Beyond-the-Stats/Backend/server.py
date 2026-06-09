"""
BackendServer: persistent orchestrator that runs the website and the
data pipeline together.

Responsibilities:
- Spawn the Flask web server in a background thread (or, on Linux/macOS,
  a separate process via ``gunicorn`` if installed).
- Schedule the full daily pipeline at a configured wall-clock time
  (default 02:00 America/New_York) using :class:`Backend.scheduler`.
- Spawn a :class:`FutureGamesWatcher` that re-runs the upcoming-matchweek
  script when the source CSVs change.
- Start a :class:`MemoryMonitor` that watches the process group and
  backs off the pipeline if RAM approaches the configured ceiling.
- Provide a single :py:meth:`run_forever` entry point and a thread-safe
  :py:meth:`stop` for graceful shutdown on SIGINT / SIGTERM.

The class deliberately keeps no module-level globals; everything goes
through the constructor so the server can be unit-tested.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .memory import MemoryMonitor, MemoryReading
from .scheduler import FutureGamesWatcher, next_run_after, seconds_until

LOG = logging.getLogger("backend.server")

DEFAULT_REFRESH_HOUR = 2
DEFAULT_REFRESH_MINUTE = 0
DEFAULT_REFRESH_TZ = "America/New_York"

DEFAULT_MEMORY_LIMIT_GB = 12.0
DEFAULT_WATCHER_INTERVAL_S = 300.0
DEFAULT_PIPELINE_TIMEOUT_S = 6 * 3600  # 6 hours for the full daily run

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SP_DIR = PROJECT_ROOT / "Beyond-the-Stats"
WEBSITE_DIR = SP_DIR / "Website"
OUTPUT_DIR = SP_DIR / "Output"


@dataclass
class BackendConfig:
    """All tunable knobs for the backend server."""

    # Website
    serve_website: bool = True
    host: str = "0.0.0.0"
    port: int = 5000
    use_gunicorn: bool = True  # auto-falls-back to Flask dev server on Windows

    # Pipeline
    daily_refresh_hour: int = DEFAULT_REFRESH_HOUR
    daily_refresh_minute: int = DEFAULT_REFRESH_MINUTE
    daily_refresh_tz: str = DEFAULT_REFRESH_TZ
    pipeline_workers: int = 3  # 3 = run global/MLS/extra sub-pipelines in parallel
    pipeline_competition_workers: int = 0  # 0 = auto
    pipeline_window_days: int = 3
    pipeline_national_window_days: int = 90
    pipeline_continue_on_error: bool = True
    pipeline_skip_mls: bool = False
    pipeline_skip_extra: bool = False
    pipeline_skip_global: bool = False
    pipeline_timeout_s: int = DEFAULT_PIPELINE_TIMEOUT_S
    run_on_start: bool = True  # run a full pipeline once on startup

    # Future-games watcher
    enable_watcher: bool = True
    watcher_interval_s: float = DEFAULT_WATCHER_INTERVAL_S
    watcher_window_days: int = 3  # how far ahead the watcher re-runs

    # Resource limits
    memory_limit_gb: float = DEFAULT_MEMORY_LIMIT_GB

    # Misc
    log_dir: Path = field(default_factory=lambda: SP_DIR / "logs")


class BackendServer:
    """Single-process orchestrator. Use :py:meth:`run_forever` to start."""

    def __init__(self, config: Optional[BackendConfig] = None) -> None:
        self.config = config or BackendConfig()
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._pipeline_lock = threading.Lock()
        self._pipeline_proc: Optional[subprocess.Popen] = None
        self._last_run: Optional[datetime] = None
        self._last_status: Optional[bool] = None
        self._flask_thread: Optional[threading.Thread] = None
        self._scheduler_thread: Optional[threading.Thread] = None
        self._watcher: Optional[FutureGamesWatcher] = None
        self._memory_monitor: Optional[MemoryMonitor] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers that call :py:meth:`stop`."""
        def _handler(signum, frame):
            LOG.info("[backend] received signal %d -- shutting down", signum)
            self.stop()

        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    signal.signal(sig, _handler)
                except (ValueError, OSError):
                    # Not on the main thread, or restricted environment.
                    pass

    def run_forever(self) -> None:
        """Boot every component and block until :py:meth:`stop` is called."""
        self.install_signal_handlers()
        self._log_banner()

        if self.config.serve_website:
            self._start_flask()

        self._start_memory_monitor()
        if self.config.enable_watcher:
            self._start_watcher()

        if self.config.run_on_start:
            self._run_pipeline_in_background(trigger="startup")

        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="daily-scheduler", daemon=True
        )
        self._scheduler_thread.start()

        try:
            while not self._stop.is_set():
                self._stop.wait(timeout=1.0)
        except KeyboardInterrupt:
            LOG.info("[backend] keyboard interrupt")
        finally:
            self._shutdown()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Flask
    # ------------------------------------------------------------------

    def _start_flask(self) -> None:
        """Start the Flask web server in a background thread or process."""
        if self._flask_thread is not None and self._flask_thread.is_alive():
            return
        # Prefer gunicorn on POSIX; fall back to Flask's dev server on Windows.
        use_gunicorn = self.config.use_gunicorn and (
            sys.platform.startswith("linux") or sys.platform == "darwin"
        )
        if use_gunicorn and self._gunicorn_available():
            self._flask_thread = threading.Thread(
                target=self._run_gunicorn, name="flask-gunicorn", daemon=True
            )
        else:
            self._flask_thread = threading.Thread(
                target=self._run_flask_dev, name="flask-dev", daemon=True
            )
        self._flask_thread.start()
        LOG.info(
            "[flask] server thread started: %s:%d (gunicorn=%s)",
            self.config.host,
            self.config.port,
            use_gunicorn,
        )

    def _gunicorn_available(self) -> bool:
        try:
            import gunicorn  # noqa: F401

            return True
        except Exception:
            return False

    def _run_gunicorn(self) -> None:
        """Run gunicorn in-process via ``gunicorn.app.base.BaseApplication``."""
        from gunicorn.app.base import BaseApplication

        # Cap workers at 4 so we leave headroom for the pipeline processes.
        workers = max(2, min(4, (os.cpu_count() or 2)))

        class FlaskApp(BaseApplication):
            def __init__(inner_self, app, options=None):
                inner_self.options = options or {}
                inner_self.application = app
                super().__init__()

            def load(inner_self):
                return inner_self.application

            def load_config(inner_self):
                for key, value in inner_self.options.items():
                    inner_self.cfg.set(key, value)

        options = {
            "bind": f"{self.config.host}:{self.config.port}",
            "workers": workers,
            "worker_class": "gthread",
            "threads": 4,
            "timeout": 120,
            "graceful_timeout": 30,
            "accesslog": "-",
            "errorlog": "-",
        }
        try:
            sys.path.insert(0, str(WEBSITE_DIR))
            import app as website_app

            website_app.app.config["_backend_refresh"] = self._run_pipeline_in_background
            FlaskApp(website_app.app, options).run()
        except Exception as exc:
            LOG.exception("[flask] gunicorn crashed: %s -- falling back to dev", exc)
            self._run_flask_dev()

    def _run_flask_dev(self) -> None:
        sys.path.insert(0, str(WEBSITE_DIR))
        try:
            import app as website_app
        except Exception as exc:
            LOG.error("[flask] failed to import website app: %s", exc)
            return
        # Disable the website's own scheduler thread -- the backend owns
        # scheduling, so we don't want the in-process loop firing concurrently.
        website_app.app.config["_backend_refresh"] = self._run_pipeline_in_background
        if hasattr(website_app, "start_daily_refresh_scheduler"):
            website_app.start_daily_refresh_scheduler = lambda *a, **kw: None
        try:
            website_app.app.run(
                host=self.config.host,
                port=self.config.port,
                debug=False,
                use_reloader=False,
                threaded=True,
            )
        except Exception as exc:
            LOG.exception("[flask] dev server crashed: %s", exc)

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    def _start_memory_monitor(self) -> None:
        def on_exceeded(reading: MemoryReading) -> None:
            LOG.error(
                "[memory] process group at %.1f MB / %.1f MB (%.1f%%); pausing pipeline",
                reading.rss_mb,
                reading.limit_mb,
                reading.utilization_pct,
            )
            self._kill_pipeline_blocking()

        def on_warn(reading: MemoryReading) -> None:
            LOG.warning(
                "[memory] process group at %.1f MB / %.1f MB (%.1f%%)",
                reading.rss_mb,
                reading.limit_mb,
                reading.utilization_pct,
            )

        self._memory_monitor = MemoryMonitor(
            limit_gb=self.config.memory_limit_gb,
            poll_interval_s=30.0,
            on_exceeded=on_exceeded,
            on_warn=on_warn,
        )
        self._memory_monitor.start()

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def _run_pipeline_in_background(self, trigger: str = "scheduled") -> None:
        """Spawn the daily pipeline as a subprocess. Non-blocking."""
        if not self._pipeline_lock.acquire(blocking=False):
            LOG.info("[pipeline] already running; skipping trigger=%s", trigger)
            return
        try:
            if self._memory_monitor is not None:
                reading = self._memory_monitor.current()
                if reading.utilization_pct >= 100.0:
                    LOG.error(
                        "[pipeline] over memory budget (%.1f MB / %.1f MB) -- skipping",
                        reading.rss_mb,
                        reading.limit_mb,
                    )
                    return
            log_path = self.config.log_dir / f"pipeline-{datetime.now():%Y%m%d-%H%M%S}-{trigger}.log"
            cmd = self._build_pipeline_cmd()
            LOG.info("[pipeline] starting (trigger=%s) -> %s", trigger, log_path)
            subprocess_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            self._pipeline_proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=subprocess_env,
            )
            self._tee_pipeline_output(log_path)
        finally:
            # Lock is released by ``_pipeline_done`` once the subprocess exits.
            pass

    def _tee_pipeline_output(self, log_path: Path) -> None:
        """Read subprocess stdout in a thread: write to log file + relay to LOG."""
        proc = self._pipeline_proc

        def _reader():
            try:
                with log_path.open("w", encoding="utf-8") as log_fh:
                    for line in iter(proc.stdout.readline, ""):
                        log_fh.write(line)
                        log_fh.flush()
                        LOG.info("[pipeline] %s", line.rstrip("\n\r"))
            except Exception:
                LOG.exception("[pipeline] output reader failed")
            finally:
                if proc.stdout and not proc.stdout.closed:
                    proc.stdout.close()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

    def _kill_pipeline_blocking(self) -> None:
        proc = self._pipeline_proc
        if proc is None or proc.poll() is not None:
            return
        LOG.warning("[pipeline] sending SIGTERM to pid %d", proc.pid)
        try:
            proc.terminate()
        except Exception:
            return
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            LOG.error("[pipeline] SIGTERM ignored -- sending SIGKILL")
            try:
                proc.kill()
            except Exception:
                pass

    def _pipeline_done(self, proc: subprocess.Popen) -> None:
        rc = proc.returncode
        self._last_run = datetime.now()
        self._last_status = rc == 0
        LOG.info(
            "[pipeline] finished rc=%s last_run=%s last_status=%s",
            rc,
            self._last_run.isoformat(),
            self._last_status,
        )
        # Mobile-app feed + Output/ publish are already part of the pipeline
        # in Daily_Pipeline.py, so the backend only spawns the run.
        try:
            self._pipeline_lock.release()
        except RuntimeError:
            pass

    def _build_pipeline_cmd(self) -> list[str]:
        cfg = self.config
        cmd = [
            sys.executable,
            str(SP_DIR / "Daily_Pipeline.py"),
            "--workers",
            str(cfg.pipeline_workers),
            "--competition-workers",
            str(cfg.pipeline_competition_workers),
            "--window-days",
            str(cfg.pipeline_window_days),
            "--national-window-days",
            str(cfg.pipeline_national_window_days),
        ]
        if cfg.pipeline_continue_on_error:
            cmd.append("--continue-on-error")
        if cfg.pipeline_skip_mls:
            cmd.append("--skip-mls")
        if cfg.pipeline_skip_extra:
            cmd.append("--skip-extra")
        if cfg.pipeline_skip_global:
            cmd.append("--skip-global")
        return cmd

    # ------------------------------------------------------------------
    # Watcher
    # ------------------------------------------------------------------

    def _start_watcher(self) -> None:
        def on_change(changed_path):
            if not self._pipeline_lock.acquire(blocking=False):
                LOG.info("[watcher] pipeline running; skipping change in %s", changed_path)
                return
            try:
                self._run_light_refresh(changed_path)
            finally:
                try:
                    self._pipeline_lock.release()
                except RuntimeError:
                    pass

        self._watcher = FutureGamesWatcher(
            project_root=PROJECT_ROOT,
            poll_interval_s=self.config.watcher_interval_s,
            refresh_callback=on_change,
            bootstrap=False,  # startup refresh is owned by the scheduler
        )
        self._watcher.start()

    def _run_light_refresh(self, changed_path: Optional[Path]) -> None:
        """Re-run the upcoming-matchweek script for each sub-pipeline.

        This is a cheap refresh -- it does NOT touch the model cache, the
        trained historical data, or the projections. It only re-predicts
        fixtures in the configured window so newly added games show up in
        the website + iOS feed within a few minutes of appearing in the
        source feed.
        """
        log_path = self.config.log_dir / f"watcher-{datetime.now():%Y%m%d-%H%M%S}.log"
        log_fh = log_path.open("w", encoding="utf-8")
        cmd_global = [
            sys.executable,
            str(SP_DIR / "files" / "Predict_Upcoming_Matchweek.py"),
            "--window-days",
            str(self.config.watcher_window_days),
        ]
        cmd_mls = [
            sys.executable,
            str(SP_DIR / "MLS" / "files" / "Predict_Upcoming_Matchweek.py"),
            "--window-days",
            str(self.config.watcher_window_days),
        ]
        cmd_extra = [
            sys.executable,
            str(SP_DIR / "Extra-leagues" / "files" / "Predict_Upcoming_Matchweek.py"),
            "--window-days",
            str(self.config.watcher_window_days),
        ]
        try:
            for cmd in (cmd_global, cmd_mls, cmd_extra):
                LOG.info("[watcher] %s", " ".join(str(c) for c in cmd))
                proc = subprocess.run(
                    cmd, cwd=str(PROJECT_ROOT), stdout=log_fh, stderr=subprocess.STDOUT
                )
                if proc.returncode != 0:
                    LOG.warning("[watcher] step %s exited with %d", cmd[-2], proc.returncode)
            # Settle live results so newly-completed matches move into the
            # accuracy history and the website's "settled" counts update.
            settle_cmd = [sys.executable, str(SP_DIR / "files" / "Update_Live_Prediction_Results.py")]
            LOG.info("[watcher] %s", " ".join(str(c) for c in settle_cmd))
            subprocess.run(settle_cmd, cwd=str(PROJECT_ROOT), stdout=log_fh, stderr=subprocess.STDOUT)
        finally:
            log_fh.close()

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            target = next_run_after(
                now=datetime.now(),
                hour=self.config.daily_refresh_hour,
                minute=self.config.daily_refresh_minute,
                tz_name=self.config.daily_refresh_tz,
            )
            wait_s = seconds_until(target)
            LOG.info(
                "[scheduler] next full pipeline at %s (in %.1f h)",
                target.isoformat(),
                wait_s / 3600.0,
            )
            # Sleep in 30-second ticks so shutdown is responsive.
            end = time.monotonic() + wait_s
            while not self._stop.is_set() and time.monotonic() < end:
                self._stop.wait(timeout=30.0)
                # If a pipeline finished while we slept, harvest its lock
                # so the next run isn't blocked.
                self._reap_pipeline()
            if self._stop.is_set():
                break
            self._run_pipeline_in_background(trigger="scheduled")
            # Wait for completion (or up to timeout) so we don't fire
            # another scheduled run while the previous is still going.
            self._wait_for_pipeline(timeout_s=self.config.pipeline_timeout_s)

    def _reap_pipeline(self) -> None:
        proc = self._pipeline_proc
        if proc is None:
            return
        rc = proc.poll()
        if rc is not None:
            self._pipeline_done(proc)
            self._pipeline_proc = None

    def _wait_for_pipeline(self, timeout_s: int) -> None:
        proc = self._pipeline_proc
        if proc is None:
            return
        deadline = time.monotonic() + timeout_s
        while proc.poll() is None and time.monotonic() < deadline and not self._stop.is_set():
            self._stop.wait(timeout=15.0)
        if proc.poll() is None:
            LOG.error("[pipeline] hit %.0fs timeout -- terminating", timeout_s)
            self._kill_pipeline_blocking()
        self._reap_pipeline()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        LOG.info("[backend] shutting down")
        self._stop.set()
        if self._watcher is not None:
            self._watcher.stop()
        if self._memory_monitor is not None:
            self._memory_monitor.stop()
        self._kill_pipeline_blocking()
        # Give the Flask thread up to 5 seconds to exit cleanly.
        if self._flask_thread is not None and self._flask_thread.is_alive():
            self._flask_thread.join(timeout=5.0)
        if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=5.0)
        LOG.info("[backend] shutdown complete")

    def _log_banner(self) -> None:
        cfg = self.config
        LOG.info(
            "[backend] starting: host=%s:%d serve_website=%s refresh=%02d:%02d %s "
            "workers=%d comp_workers=%d memory=%.1fGB watcher=%.0fs",
            cfg.host,
            cfg.port,
            cfg.serve_website,
            cfg.daily_refresh_hour,
            cfg.daily_refresh_minute,
            cfg.daily_refresh_tz,
            cfg.pipeline_workers,
            cfg.pipeline_competition_workers,
            cfg.memory_limit_gb,
            cfg.watcher_interval_s,
        )


def configure_logging(log_dir: Path) -> None:
    """Configure root logging to stderr only (journald)."""
    formatter = logging.Formatter(
        "[%(asctime)s] %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(stream_handler)
