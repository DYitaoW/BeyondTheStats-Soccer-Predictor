
import os as _os_paths_setup
import sys as _sys_paths_setup
_FILES_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.abspath(__file__))
_REGION_DIR = _os_paths_setup.path.dirname(_FILES_DIR)  # pipelines/europe
_SP_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.dirname(_REGION_DIR))  # Beyond-the-Stats
if _SP_DIR not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, _SP_DIR)
from shared import paths as _bts_paths
if str(_bts_paths.SHARED_DIR) not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, str(_bts_paths.SHARED_DIR))
BASE_DIR = str(_bts_paths.SP_DIR)  # europe uses project-level Data/
PREDICTIONS_DIR = str(_bts_paths.OUTPUT_PRED_EUROPE)
PROJECT_DIR = str(_bts_paths.SP_DIR)
"""Generate current_season_teams.json — a manually-verifiable roster of
every team in every league for the 2026-27 season.

Pre-fills teams from the ESPN snapshot (league_teams.json) where available.
Leagues that returned empty from ESPN are left as [] for manual entry.

Usage:  python files/generate_season_teams.py
"""
import json, os

LEAGUES = [
    # ── Global (Aug–May) ──────────────────────────────────────
    "England/Premier League", "England/Championship",
    "Spain/La Liga", "Spain/La Liga 2",
    "Italy/Serie A", "Italy/Serie B",
    "Germany/Bundesliga", "Germany/Bundesliga 2",
    "France/Ligue 1", "France/Ligue 2",
    "Portugal/Liga Portugal", "Netherlands/Eredivisie",
    "Belgium/First Division A", "Scotland/Premiership",
    "Turkey/Super Lig",
    # ── MLS (Mar–Oct) ──────────────────────────────────────────
    "United States/MLS",
    # ── Extra leagues (various calendars) ──────────────────────
    "Austria/Bundesliga",
    "Greece/Super League", "Norway/Eliteserien",
    "Romania/Liga I", "Sweden/Allsvenskan",
    "Poland/Ekstraklasa",
    "Mexico/Liga MX", "Argentina/Primera Division",
    "Brazil/Brasileirão", "Japan/J1 League",
    "North America/Leagues Cup",
    # ── MLS sub-competitions (for bracket display) ──────────────
    "United States/MLS - Supporters Shield Table",
    "United States/MLS - Eastern Conference",
    "United States/MLS - Western Conference",
]

# Load ESPN snapshot (fallback source)
espn_file = str(_bts_paths.LEAGUE_TEAMS_FILE)
espn_data = {}
if os.path.exists(espn_file):
    with open(espn_file, "r", encoding="utf-8-sig") as f:
        espn_data = json.load(f)

output = {}
for comp in LEAGUES:
    teams = espn_data.get(comp)
    if teams:
        output[comp] = teams
    else:
        output[comp] = []  # user to fill in manually

out_path = str(_bts_paths.CURRENT_SEASON_TEAMS_FILE)
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

filled = sum(1 for v in output.values() if v)
empty = sum(1 for v in output.values() if not v)
print(f"Saved {len(output)} leagues ({filled} pre-filled, {empty} need manual entry) to {out_path}")
