"""
Persistent backend server for the Beyond the Score website + future iOS app.

Wraps the existing Daily_Pipeline runner with:
- A Flask web server thread so the website stays up while the pipeline runs.
- A 2am ET (or any timezone-aware) scheduler for the full daily refresh.
- A future-games watcher that re-runs the upcoming-matchweek script whenever
  new fixtures appear in the source feeds.
- A memory monitor that throttles workers if the process group is approaching
  the configured RAM budget (default 12 GB).
- Multiprocessing-friendly execution (--workers / --competition-workers
  forwarded to Run_All_Pipeline).

The intended entry point is ``run_backend.py`` at the project root; this
package is what it imports.
"""

from .server import BackendConfig, BackendServer
from .scheduler import FutureGamesWatcher, next_run_after
from .memory import MemoryMonitor, MemoryReading

__all__ = [
    "BackendConfig",
    "BackendServer",
    "FutureGamesWatcher",
    "MemoryMonitor",
    "MemoryReading",
    "next_run_after",
]
