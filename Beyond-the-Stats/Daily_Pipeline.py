"""Compatibility shim — entry point moved to ``main/Daily_Pipeline.py``.

Keeps ``python Daily_Pipeline.py`` working from Beyond-the-Stats/ after the reorg.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "main" / "Daily_Pipeline.py"
if not _TARGET.is_file():
    sys.stderr.write(f"error: missing moved entry point: {_TARGET}\n")
    raise SystemExit(1)
sys.argv[0] = str(_TARGET)
runpy.run_path(str(_TARGET), run_name="__main__")
