#!/usr/bin/env python3
"""Diagnostic script to check website data loading issues."""
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from shared import paths as _paths

files = {
    "Cup": _paths.CUP_UPCOMING_FILE,
    "Global": _paths.GLOBAL_UPCOMING_FILE,
    "MLS": _paths.MLS_UPCOMING_FILE,
    "Extra": _paths.EXTRA_UPCOMING_FILE,
}

print("=" * 80)
print("WEBSITE DATA DIAGNOSTIC")
print("=" * 80)

today = datetime.now().date()
print(f"\nToday: {today}\n")

for name, path in files.items():
    abs_path = os.path.abspath(path)
    print(f"\n{'=' * 80}")
    print(f"{name} Predictions: {abs_path}")
    print(f"{'=' * 80}")

    if not os.path.exists(abs_path):
        print("File does not exist!")
        continue

    try:
        df = pd.read_csv(abs_path)
    except Exception as exc:
        print(f"Failed to read: {exc}")
        continue

    print(f"Rows: {len(df)}")
    if "match_date" in df.columns:
        try:
            dates = pd.to_datetime(df["match_date"], errors="coerce")
            print(f"Date range: {dates.min()} -> {dates.max()}")
        except Exception:
            pass

missing = [name for name, path in files.items() if not os.path.exists(path)]
if missing:
    print("\nMissing prediction files:", ", ".join(missing))
    print("  1. Run the prediction pipelines for MLS and Extra")
    print("  2. Confirm outputs land under Output/Predictions/{region}/")
else:
    print("\nAll checked prediction files are present.")
