"""Gunicorn hooks for the Beyond the Stats website.

Used by direct ``gunicorn`` deployments (e.g. Render) where BackendServer
is not wrapping the process.  Starts background workers inside each gunicorn
worker after fork — threads started in the master do not survive ``fork()``.
"""


def post_worker_init(worker):
    """Start per-worker background services after the worker process forks."""
    try:
        from app import start_live_score_poller, start_apns_worker

        start_live_score_poller()
        start_apns_worker()
    except Exception as exc:
        worker.log.exception("post_worker_init failed: %s", exc)
