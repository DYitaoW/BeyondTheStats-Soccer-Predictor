"""
Migrate regional pipeline working data (pipelines/mls/Data and pipelines/extra/Data)
into the centralized shared storage (Data/mls and Data/extra).
Safe copy-first-then-remove migration.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

SP_DIR = Path(__file__).resolve().parent.parent
if str(SP_DIR) not in sys.path:
    sys.path.insert(0, str(SP_DIR))
if str(SP_DIR / "shared") not in sys.path:
    sys.path.insert(0, str(SP_DIR / "shared"))

from shared import paths


def migrate() -> None:
    print("[migrate] Starting data storage consolidation...")
    paths.ensure_output_dirs()
    print("[migrate] Running migrate_legacy_working_data()...")
    paths.migrate_legacy_working_data()
    print("[migrate] Running migrate_legacy_runtime_files()...")
    paths.migrate_legacy_runtime_files()
    print("[migrate] Ensuring consolidated directories exist:")
    print(f"  MLS shared data:   {paths.MLS_DATA_DIR}")
    print(f"  Extra shared data: {paths.EXTRA_DATA_DIR}")
    print(f"  Europe shared data:{paths.EUROPE_DATA_DIR}")
    print("[migrate] Migration completed successfully.")


if __name__ == "__main__":
    migrate()

