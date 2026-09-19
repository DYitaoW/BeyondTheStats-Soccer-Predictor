"""Compatibility shim — real module lives in ``main/run_backend.py``.

- As a script: forwards to ``main/run_backend.py``.
- As an import: loads ``main/run_backend.py`` and re-exports its public attrs
  (so ``from Run_All_Pipeline import run_full_pipeline`` still works
  without executing ``main()`` with the caller's argv).
"""
from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "main" / "run_backend.py"
if not _TARGET.is_file():
    sys.stderr.write(f"error: missing moved entry point: {_TARGET}\n")
    raise SystemExit(1)

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    _spec = importlib.util.spec_from_file_location(__name__, _TARGET)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"cannot load {_TARGET}")
    _mod = importlib.util.module_from_spec(_spec)
    # Register before exec so circular imports see this module object.
    sys.modules[__name__] = _mod
    _spec.loader.exec_module(_mod)
    # Re-export for "from X import Y"
    globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
    # Keep dunder file pointing at the real implementation.
    globals()["__file__"] = str(_TARGET)
