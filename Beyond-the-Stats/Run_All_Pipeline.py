"""Compatibility shim — entry point moved to ``main/Run_All_Pipeline.py``.

Keeps ``python Run_All_Pipeline.py`` working from Beyond-the-Stats/ after the reorg.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "main" / "Run_All_Pipeline.py"
if not _TARGET.is_file():
    sys.stderr.write(f"error: missing moved entry point: {_TARGET}\n")
    raise SystemExit(1)
sys.argv[0] = str(_TARGET)
runpy.run_path(str(_TARGET), run_name="__main__")
