#!/usr/bin/env python3
"""Clear Raw_Data CSVs so downloaders can re-fetch every season from scratch.

Use this when leftover files may be stale or use an old naming scheme
(e.g. leagues moved Extra → Global, or ``*statYYYY.csv`` vs ``*statYYYY-YY.csv``).
Download scripts skip seasons that already exist on disk, so a full wipe is
the reliable way to force a clean redownload.

Raw trees cleared (by default all three):

  Data/Raw_Data/
  MLS/Data/Raw_Data/
  Extra-leagues/Data/Raw_Data/

Does **not** touch Processed_Data, Team_Data, Predictions, or Output.
After clearing, re-run the daily pipeline (or each Download_Latest_Data.py)
to refill Raw_Data.

Examples:
  python clear_raw_data.py --dry-run
  python clear_raw_data.py --yes
  python clear_raw_data.py --pipeline extra --yes
  python clear_raw_data.py --pipeline global --pipeline mls --yes
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


SP_DIR = Path(__file__).resolve().parent

RAW_DATA_BY_PIPELINE = {
    "global": SP_DIR / "Data" / "Raw_Data",
    "mls": SP_DIR / "MLS" / "Data" / "Raw_Data",
    "extra": SP_DIR / "Extra-leagues" / "Data" / "Raw_Data",
}


def _iter_files(root: Path):
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def _summarize(root: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) under ``root``."""
    count = 0
    total = 0
    for path in _iter_files(root):
        count += 1
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return count, total


def _format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{n} B"


def clear_raw_tree(root: Path, *, dry_run: bool) -> tuple[int, int]:
    """Delete all files under ``root``, then remove empty directories.

    Recreates an empty ``root`` so downloaders always have a target folder.
    Returns (files_removed, bytes_removed).
    """
    file_count, total_bytes = _summarize(root)
    if not root.exists():
        if not dry_run:
            root.mkdir(parents=True, exist_ok=True)
        return 0, 0

    if dry_run:
        return file_count, total_bytes

    # Remove the whole tree, then recreate the empty root.
    shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    return file_count, total_bytes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clear Raw_Data directories so season CSVs can be fully redownloaded.",
    )
    parser.add_argument(
        "--pipeline",
        action="append",
        choices=sorted(RAW_DATA_BY_PIPELINE),
        dest="pipelines",
        help="Pipeline Raw_Data tree to clear (repeatable). Default: all three.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be deleted without removing anything.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pipelines = args.pipelines or list(RAW_DATA_BY_PIPELINE)

    targets = [(name, RAW_DATA_BY_PIPELINE[name]) for name in pipelines]

    print("Raw_Data clear targets:")
    planned_files = 0
    planned_bytes = 0
    for name, root in targets:
        count, size = _summarize(root)
        planned_files += count
        planned_bytes += size
        status = "missing" if not root.exists() else f"{count} file(s), {_format_bytes(size)}"
        print(f"  [{name}] {root}  ({status})")

    if planned_files == 0:
        print("\nNothing to clear — Raw_Data trees are already empty or missing.")
        if not args.dry_run:
            for _, root in targets:
                root.mkdir(parents=True, exist_ok=True)
        return 0

    print(f"\nTotal: {planned_files} file(s), {_format_bytes(planned_bytes)}")

    if args.dry_run:
        print("Dry run only — no files removed.")
        return 0

    if not args.yes:
        try:
            reply = input("Delete these Raw_Data files? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in {"y", "yes"}:
            print("Aborted.")
            return 1

    removed_files = 0
    removed_bytes = 0
    for name, root in targets:
        count, size = clear_raw_tree(root, dry_run=False)
        removed_files += count
        removed_bytes += size
        print(f"  cleared [{name}] ({count} file(s), {_format_bytes(size)})")

    print(
        f"\nDone. Removed {removed_files} file(s) ({_format_bytes(removed_bytes)}). "
        "Re-run the pipeline (or each Download_Latest_Data.py) to redownload."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
