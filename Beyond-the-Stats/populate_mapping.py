#!/usr/bin/env python3
"""Add missing competition sections to team_name_mapping_master.json.

Scans all raw CSV data directories, finds competitions not yet in the
mapping file, adds them as empty dicts, and prints the canonical CSV
team names for each so you know what to map ESPN display names to.

Usage:
    python populate_mapping.py
"""

import csv
import json
import os
from collections import OrderedDict

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MAPPING_FILE = os.path.join(PROJECT_DIR, "..", "Data", "team_name_mapping_master.json")

# All known competition name -> list of possible raw data directory paths
COMP_TO_DIRS = {
    "England/Premier League":      ["Data/Raw_Data/England/Premier League"],
    "England/Championship":        ["Data/Raw_Data/England/Championship"],
    "Spain/La Liga":              ["Data/Raw_Data/Spain/La Liga"],
    "Spain/La Liga 2":            ["Data/Raw_Data/Spain/La Liga 2"],
    "Italy/Serie A":              ["Data/Raw_Data/Italy/Serie A"],
    "Italy/Serie B":              ["Data/Raw_Data/Italy/Serie B"],
    "Germany/Bundesliga":         ["Data/Raw_Data/Germany/Bundesliga"],
    "Germany/Bundesliga 2":       ["Data/Raw_Data/Germany/Bundesliga 2"],
    "France/Ligue 1":             ["Data/Raw_Data/France/Ligue 1"],
    "France/Ligue 2":             ["Data/Raw_Data/France/Ligue 2"],
    "Portugal/Liga Portugal":     ["Data/Raw_Data/Portugal/Liga Portugal"],
    "Netherlands/Eredivisie":     ["Data/Raw_Data/Netherlands/Eredivisie"],
    "Belgium/First Division A":   ["Data/Raw_Data/Belgium/First Division A",
                                   "Extra-leagues/Data/Raw_Data/Belgium/First Division A"],
    "Scotland/Premiership":       ["Data/Raw_Data/Scotland/Premiership"],
    "Turkey/Super Lig":           ["Data/Raw_Data/Turkey/Super Lig"],
    "Greece/Super League":        ["Data/Raw_Data/Greece/Super League",
                                   "Extra-leagues/Data/Raw_Data/Greece/Super League"],
    "Norway/Eliteserien":         ["Data/Raw_Data/Norway/Eliteserien",
                                   "Extra-leagues/Data/Raw_Data/Norway/Eliteserien"],
    "Poland/Ekstraklasa":         ["Data/Raw_Data/Poland/Ekstraklasa",
                                   "Extra-leagues/Data/Raw_Data/Poland/Ekstraklasa"],
    "Romania/Liga I":             ["Data/Raw_Data/Romania/Liga I",
                                   "Extra-leagues/Data/Raw_Data/Romania/Liga I"],
    "Sweden/Allsvenskan":         ["Data/Raw_Data/Sweden/Allsvenskan",
                                   "Extra-leagues/Data/Raw_Data/Sweden/Allsvenskan"],
    "Austria/Bundesliga":         ["Data/Raw_Data/Austria/Bundesliga",
                                   "Extra-leagues/Data/Raw_Data/Austria/Bundesliga"],
    "United States/MLS":          ["MLS/Data/Raw_Data/United States/MLS"],
    "Mexico/Liga MX":             ["MLS/Data/Raw_Data/Mexico/Liga MX",
                                   "Extra-leagues/Data/Raw_Data/Mexico/Liga MX"],
    "Argentina/Primera Division": ["Extra-leagues/Data/Raw_Data/Argentina/Primera Division"],
    "Brazil/Brasileirão":            ["Extra-leagues/Data/Raw_Data/Brazil/Brasileirão"],
    "Japan/J1 League":           ["Extra-leagues/Data/Raw_Data/Japan/J1 League"],
}

# Competitions with no raw CSV data (cups, UEFA, FIFA, etc.)
NO_CSV_COMPS = [
    "Switzerland/Super League",
    "Ukraine/Premier League",
    "Croatia/HNL",
    "Hungary/NB I",
    "Czech Republic/First League",
    "Serbia/SuperLiga",
    "Cyprus/First Division",
    "Slovakia/Super Liga",
    "Slovenia/PrvaLiga",
    "England/FA Cup",
    "England/League Cup",
    "Spain/Copa del Rey",
    "Germany/DFB-Pokal",
    "France/Coupe de France",
    "Italy/Coppa Italia",
    "United States/US Open Cup",
    "CONCACAF/Leagues Cup",
    "UEFA/Champions League",
    "UEFA/Europa League",
    "UEFA/Conference League",
    "FIFA/World Cup",
    "FIFA/Friendly",
    "Club Friendlies",
]


def extract_teams(dir_path):
    """Read all CSVs in a directory and return sorted unique team names."""
    full = os.path.join(PROJECT_DIR, dir_path)
    if not os.path.isdir(full):
        return None
    teams = set()
    for fname in os.listdir(full):
        if not fname.endswith(".csv"):
            continue
        path = os.path.join(full, fname)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    continue
                cols = [c.strip() for c in reader.fieldnames]
                ht_col = "HomeTeam" if "HomeTeam" in cols else ("Home" if "Home" in cols else None)
                at_col = "AwayTeam" if "AwayTeam" in cols else ("Away" if "Away" in cols else None)
                if not ht_col or not at_col:
                    continue
                for row in reader:
                    ht = str(row.get(ht_col, "")).strip()
                    at = str(row.get(at_col, "")).strip()
                    if ht: teams.add(ht)
                    if at: teams.add(at)
        except Exception:
            pass
    return sorted(teams) if teams else None


def _teams_to_dict(teams):
    """Convert a sorted list of team names to a mapping dict (name -> name)."""
    return OrderedDict((t, t) for t in teams)


def main():
    with open(MAPPING_FILE, "r", encoding="utf-8") as f:
        mapping = json.load(f, object_pairs_hook=OrderedDict)

    existing = set(mapping.keys())
    added = {}      # comp -> teams (new competition sections added)
    populated = []   # comps that were empty {} and now filled with CSV names
    enriched = []    # (comp, new_teams) entries added to existing populated sections

    # --- Phase 1: Add new competitions with CSV data ---
    for comp, dirs in COMP_TO_DIRS.items():
        if comp in existing:
            continue
        for d in dirs:
            teams = extract_teams(d)
            if teams is not None:
                added[comp] = teams
                mapping[comp] = _teams_to_dict(teams)
                break

    # --- Phase 2: Add competitions without CSV data ---
    for comp in NO_CSV_COMPS:
        if comp in existing or comp in added:
            continue
        added[comp] = None
        mapping[comp] = OrderedDict()

    # --- Phase 3: Populate existing empty entries with CSV names ---
    for comp, dirs in COMP_TO_DIRS.items():
        if comp not in existing or comp in added:
            continue
        if mapping.get(comp):  # already has mappings
            continue
        for d in dirs:
            teams = extract_teams(d)
            if teams is not None:
                mapping[comp] = _teams_to_dict(teams)
                populated.append(comp)
                break

    # --- Phase 4: Add new CSV names to existing populated sections ---
    for comp, dirs in COMP_TO_DIRS.items():
        if comp not in existing or comp in added or comp in populated:
            continue
        existing_values = set(mapping.get(comp, {}).values())
        for d in dirs:
            teams = extract_teams(d)
            if teams is not None:
                new_names = [t for t in teams if t not in existing_values]
                if new_names:
                    mapping[comp].update((t, t) for t in new_names)
                    enriched.append((comp, new_names))
                break

    if not added and not populated and not enriched:
        print("All competitions already present in the mapping file.")
        return

    with open(MAPPING_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)
        f.write("\n")

    for comp in sorted(added):
        teams = added[comp]
        if teams:
            print(f"\n=== {comp} ===")
            for t in teams:
                print(f"  {t}")
        else:
            print(f"\n=== {comp} ===  (no local CSV data)")

    for comp in sorted(populated):
        teams = extract_teams(next(d for d in COMP_TO_DIRS[comp] if os.path.isdir(os.path.join(PROJECT_DIR, d))))
        print(f"\n=== {comp} ===  (populated from CSV)")
        for t in teams:
            print(f"  {t}")

    for comp, new_names in enriched:
        print(f"\n=== {comp} ===  (+{len(new_names)} new CSV names added to existing mapping)")
        for t in new_names:
            print(f"  {t}")

    parts = []
    if added:
        parts.append(f"Added {len(added)}")
    if populated:
        parts.append(f"Populated {len(populated)}")
    if enriched:
        parts.append(f"Enriched {len(enriched)} (+{sum(len(n) for _, n in enriched)} names)")
    print(f"\n{'  |  '.join(parts)}")


if __name__ == "__main__":
    main()
