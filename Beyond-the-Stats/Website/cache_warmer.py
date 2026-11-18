"""Keep the heaviest API responses warm.

Cold requests rebuilt shared indexes on the request path (``/api/team`` 7-8 s,
``/api/upcoming/global`` 5 s, ``/api/past-games`` 4.5 s, ``/api/startup``
1.4-3.9 s). The response cache is stale-while-revalidate, so once an entry
exists it is never cold again while it is requested; this worker makes the
first request after a restart warm and re-touches the entries every
``WARM_INTERVAL_SECONDS`` (240 s) so they never fall out of the stale window.

Requests go through the ASGI app in-process (``TestClient`` without its
context manager, so the lifespan and its workers are not started again).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Below the shortest stale window (30 s TTL x 10 = 300 s) so warm entries never expire.
WARM_INTERVAL_SECONDS = int(os.getenv("BTS_CACHE_WARM_INTERVAL", "240"))
STARTUP_DELAY_SECONDS = float(os.getenv("BTS_CACHE_WARM_DELAY", "20"))

# Response-cached endpoints only (``/api/startup`` / ``/api/live-scores`` are
# live and not response-cached; ``/api/startup`` memoizes its upcoming part).
WARM_PATHS = (
    "/api/upcoming/global",
    "/api/upcoming/global?compact=1",
    "/api/home/upcoming",
    "/api/past-games",
    "/api/teams/catalog",
)
WARM_LEAGUES = (
    "England/Premier League", "Spain/La Liga", "Italy/Serie A", "Germany/Bundesliga",
    "France/Ligue 1", "England/Championship", "United States/MLS",
)
# One team request builds the shared team indexes; the others are cheap after.
WARM_TEAMS = ("Arsenal", "Manchester United", "Real Madrid", "Barcelona", "Inter Miami")


def warm_paths() -> list[str]:
    paths = list(WARM_PATHS)
    paths += [f"/api/league-data/{quote(c)}" for c in WARM_LEAGUES]
    paths += [f"/api/team/{quote(t)}" for t in WARM_TEAMS]
    return paths


def warm_once(app) -> dict:
    """Request every warm path once; returns ``{path: (status, seconds)}``."""
    from starlette.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=False)
    results = {}
    for path in warm_paths():
        t0 = time.monotonic()
        try:
            status = client.get(path, headers={"X-BTS-Cache-Warm": "1"}).status_code
        except Exception as exc:  # pragma: no cover - logged only
            logger.warning(f"[cache-warm] {path} failed: {exc}")
            status = -1
        results[path] = (status, round(time.monotonic() - t0, 2))
    return results


def start_cache_warmer(app) -> None:
    """Daemon thread: warm after startup, then every ``WARM_INTERVAL_SECONDS``."""
    if os.getenv("BTS_DISABLE_CACHE_WARMER", "").strip() in {"1", "true", "yes"}:
        return
    try:
        from shared import paths as _paths

        if _paths.running_under_test_runner():
            return
    except Exception:
        pass

    def _worker():
        time.sleep(STARTUP_DELAY_SECONDS)
        while True:
            t0 = time.monotonic()
            try:
                res = warm_once(app)
                slow = {p: v for p, v in res.items() if v[1] > 1.0 or v[0] != 200}
                logger.info(f"[cache-warm] {len(res)} endpoints in {time.monotonic() - t0:.1f}s; slow/failed: {slow}")
            except Exception as exc:  # pragma: no cover
                logger.warning(f"[cache-warm] cycle failed: {exc}")
            time.sleep(max(60, WARM_INTERVAL_SECONDS))

    threading.Thread(target=_worker, name="CacheWarmer", daemon=True).start()
