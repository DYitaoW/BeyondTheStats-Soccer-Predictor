#!/usr/bin/env python3
"""
Run the complete World Cup prediction pipeline.

This script orchestrates the full World Cup prediction workflow:
1. Process national team data (rankings, squad values, last 15 matches)
2. Build the national team predictor ML model
3. Generate World Cup group stage projections
4. Project tournament bracket outcomes

Usage:
    python Run_World_Cup_Pipeline.py [options]

Options:
    --skip-data-fetch     Reuse existing match data instead of fetching from ESPN
    --lookback-days N     How far back to fetch match history (default: 900)
    --rankings-file PATH  External FIFA rankings JSON/CSV file
    --squad-values-file PATH  External squad values JSON/CSV file
"""

import argparse
import os
import sys
import subprocess
from datetime import datetime, UTC
from pathlib import Path


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "Data", "National_Team_Data")


def print_section(title):
    """Print a formatted section header."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


def run_command(script_name, args=None):
    """
    Run a Python script in the current Python environment.
    
    Args:
        script_name: Name of the script to run (e.g., 'Process_National_Team_Data.py')
        args: List of command-line arguments to pass to the script
    
    Returns:
        bool: True if successful, False if failed
    """
    script_path = os.path.join(FILES_DIR, script_name)
    if not os.path.exists(script_path):
        print(f"ERROR: Script not found: {script_path}")
        return False
    
    cmd = [sys.executable, script_path]
    if args:
        cmd.extend(args)
    
    print(f"Running: {' '.join(cmd)}\n")
    try:
        result = subprocess.run(cmd, check=True, cwd=FILES_DIR)
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Script failed with exit code {e.returncode}")
        return False
    except Exception as e:
        print(f"ERROR: Failed to run script: {e}")
        return False


def verify_output_file(file_path, description):
    """Verify that an expected output file was created."""
    if os.path.exists(file_path):
        size_kb = os.path.getsize(file_path) / 1024.0
        print(f"✓ {description}: {file_path} ({size_kb:.1f} KB)")
        return True
    else:
        print(f"✗ {description}: NOT FOUND at {file_path}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Run the complete World Cup prediction pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--skip-data-fetch",
        action="store_true",
        help="Reuse existing match data instead of fetching from ESPN"
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=900,
        help="How far back to fetch match history (default: 900)"
    )
    parser.add_argument(
        "--rankings-file",
        default="",
        help="External FIFA rankings JSON/CSV file"
    )
    parser.add_argument(
        "--squad-values-file",
        default="",
        help="External squad values JSON/CSV file"
    )
    args = parser.parse_args()
    
    print_section("WORLD CUP PREDICTION PIPELINE")
    print(f"Started: {datetime.now(UTC).isoformat()}\n")
    
    # ===== STEP 1: Process National Team Data =====
    print_section("STEP 1: Process National Team Data")
    print("Building national team predictor with:")
    print("  • FIFA rankings from available sources")
    print("  • Squad market values")
    print("  • Last 15 matches for each team (with opponent quality adjustments)")
    print("  • ML model training (Logistic Regression + scaling)")
    
    process_args = ["--world-cup-only", f"--lookback-days={args.lookback_days}"]
    if args.skip_data_fetch:
        process_args.append("--skip-fetch")
    if args.rankings_file:
        process_args.extend(["--rankings-file", args.rankings_file])
    if args.squad_values_file:
        process_args.extend(["--squad-values-file", args.squad_values_file])
    
    if not run_command("Process_National_Team_Data.py", process_args):
        print("\n" + "!" * 80)
        print("PIPELINE FAILED: Could not process national team data")
        print("!" * 80)
        return 1
    
    # Verify outputs from step 1
    print("\nVerifying Step 1 outputs:")
    verify_output_file(
        os.path.join(DATA_DIR, "national_team_model_cache.pkl"),
        "Predictor model cache"
    )
    verify_output_file(
        os.path.join(DATA_DIR, "all_team_rankings.json"),
        "Complete team rankings"
    )
    verify_output_file(
        os.path.join(DATA_DIR, "national_team_recent_context.csv"),
        "Team context data"
    )
    
    # ===== STEP 2: Project World Cup =====
    print_section("STEP 2: Project World Cup Groups & Bracket")
    print("Generating probabilistic projections:")
    print("  • Group stage outcomes (using trained predictor)")
    print("  • Bracket advancement based on group standings")
    print("  • Tournament winner probabilities")
    print("  • Knockout stage predictions")
    
    if not run_command("Project_World_Cup.py", []):
        print("\n" + "!" * 80)
        print("PIPELINE FAILED: Could not project World Cup")
        print("!" * 80)
        return 1
    
    # Verify outputs from step 2
    print("\nVerifying Step 2 outputs:")
    verify_output_file(
        os.path.join(DATA_DIR, "world_cup_projection.json"),
        "World Cup projection"
    )
    verify_output_file(
        os.path.join(DATA_DIR, "projected_cup_brackets.json"),
        "Projected brackets"
    )
    verify_output_file(
        os.path.join(DATA_DIR, "projected_cup_tables.csv"),
        "Projected group tables"
    )
    
    # ===== Summary =====
    print_section("PIPELINE COMPLETE")
    print(f"Finished: {datetime.now(UTC).isoformat()}\n")
    print("Output files generated in: " + DATA_DIR)
    print("\nKey outputs:")
    print("  • world_cup_projection.json - Full tournament projection with probabilities")
    print("  • projected_cup_tables.csv - Group stage final standings")
    print("  • projected_cup_brackets.json - Knockout bracket predictions")
    print("  • all_team_rankings.json - Complete rankings used for analysis")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
