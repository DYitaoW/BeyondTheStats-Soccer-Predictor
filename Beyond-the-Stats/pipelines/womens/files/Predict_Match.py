"""Match model for NWSL and WSL (pipelines/womens/Data).

Thin regional profile over the single shared implementation in
``pipelines/predict_match_core.py``: it sets ``REGION`` and executes the core
into this module's namespace, so ``import Predict_Match as pm`` (and running
this file as a script, e.g. ``--build-cache-only``) work exactly as before,
with this region's paths, model cache and globals. Region differences live
in the core as ``REGION`` switches.
"""

REGION = "womens"


def _core_code():
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "predict_match_core.py",
    )
    with open(path, encoding="utf-8") as fh:
        return compile(fh.read(), path, "exec")


_CORE_CODE = _core_code()
del _core_code
exec(_CORE_CODE, globals())
