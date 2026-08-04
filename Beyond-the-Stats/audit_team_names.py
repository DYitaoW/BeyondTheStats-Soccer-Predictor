#!/usr/bin/env python3
"""Audit team names across all data sources and find unmapped display names.

Usage:
    python audit_team_names.py

Produces two temp files:
    csv_team_names.txt   — all team names from raw CSV data (predictor canonical)
    unmapped_names.txt   — display names from API/upcoming CSVs missing from
                           mapping file, grouped by competition/league
"""

import csv
import json
import os
import sys
import tempfile
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
import team_mapping_groups as tmg  # noqa: E402

RAW_DIRS = [
    os.path.join(PROJECT_DIR, "Data", "Raw_Data"),
    os.path.join(PROJECT_DIR, "MLS", "Data", "Raw_Data"),
    os.path.join(PROJECT_DIR, "Extra-leagues", "Data", "Raw_Data"),
]

UPCOMING_FILES = [
    os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_matchweek_predictions.csv"),
    os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_cup_predictions.csv"),
    os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_national_team_predictions.csv"),
    os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_club_friendlies.csv"),
    os.path.join(PROJECT_DIR, "MLS", "Data", "Predictions", "upcoming_matchweek_predictions.csv"),
    os.path.join(PROJECT_DIR, "Extra-leagues", "Data", "Predictions", "upcoming_matchweek_predictions.csv"),
    os.path.join(PROJECT_DIR, "Output", "Upcoming", "all_upcoming.csv"),
]

MAPPING_FILE = os.path.join(PROJECT_DIR, "..", "Data", "team_name_mapping_master.json")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

ESPN_IDS = {
    "England/Premier League": "eng.1",
    "England/Championship": "eng.2",
    "Spain/La Liga": "esp.1",
    "Spain/La Liga 2": "esp.2",
    "Italy/Serie A": "ita.1",
    "Italy/Serie B": "ita.2",
    "Germany/Bundesliga": "ger.1",
    "Germany/Bundesliga 2": "ger.2",
    "France/Ligue 1": "fra.1",
    "France/Ligue 2": "fra.2",
    "Portugal/Liga Portugal": "por.1",
    "Netherlands/Eredivisie": "ned.1",
    "United States/MLS": "usa.1",
    "Mexico/Liga MX": "mex.1",
    "Belgium/First Division A": "bel.1",
    "Scotland/Premiership": "sco.1",
    "Turkey/Super Lig": "tur.1",
    "Austria/Bundesliga": "aut.1",
    "Switzerland/Super League": "sui.1",
    "Greece/Super League": "gre.1",
    "Denmark/Danish Superliga": "den.1",
    "Ukraine/Premier League": "ukr.1",
    "Norway/Eliteserien": "nor.1",
    "Croatia/HNL": "cro.1",
    "Romania/Liga I": "rou.1",
    "Sweden/Allsvenskan": "swe.1",
    "Hungary/NB I": "hun.1",
    "Israel/Premier League": "isr.1",
    "Czech Republic/First League": "cze.1",
    "Poland/Ekstraklasa": "pol.1",
    "Serbia/SuperLiga": "srb.1",
    "Cyprus/First Division": "cyp.1",
    "Slovakia/Super Liga": "svk.1",
    "Slovenia/PrvaLiga": "svn.1",
    "Bulgaria/First League": "bul.1",
    "Argentina/Primera Division": "arg.1",
    "Brazil/Brasileirão": "bra.1",
    "Japan/J1 League": "jpn.1",
    "England/FA Cup": "eng.fa",
    "England/League Cup": "eng.efl",
    "Spain/Copa del Rey": "esp.copa_del_rey",
    "Germany/DFB-Pokal": "ger.dfb_pokal",
    "France/Coupe de France": "fra.coupe_de_france",
    "Italy/Coppa Italia": "ita.coppa",
    "United States/US Open Cup": "usa.open_cup",
    "North America/Leagues Cup": "concacaf.leagues.cup",
    "Europe/Champions League": "uefa.champions",
    "Europe/Europa League": "uefa.europa",
    "Europe/Conference League": "uefa.europa.conf",
    "International/World Cup": "fifa.world",
    "International/Friendly": "fifa.friendly",
    "Club Friendlies": "club.friendly",
}

CUP_SCOREBOARD_IDS = {
    "eng.fa", "eng.efl", "ita.coppa", "esp.copa_del_rey",
    "ger.dfb_pokal", "fra.coupe_de_france", "usa.open_cup",
    "concacaf.leagues.cup", "uefa.champions", "uefa.europa",
    "uefa.europa.conf", "fifa.world", "fifa.friendly", "club.friendly",
}


def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_csv_team_names():
    names = set()
    for raw_dir in RAW_DIRS:
        if not os.path.isdir(raw_dir):
            continue
        for root, _dirs, files in os.walk(raw_dir):
            for fname in files:
                if not fname.endswith(".csv"):
                    continue
                path = os.path.join(root, fname)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            ht = str(row.get("HomeTeam", "")).strip()
                            at = str(row.get("AwayTeam", "")).strip()
                            if ht: names.add(ht)
                            if at: names.add(at)
                except Exception:
                    pass
    return sorted(names)


def extract_upcoming_team_names_by_comp():
    comp_names = {}  # competition -> set of team names
    for path in UPCOMING_FILES:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    comp = str(row.get("competition", "")).strip()
                    if not comp:
                        continue
                    ht = str(row.get("home_team", "")).strip()
                    at = str(row.get("away_team", "")).strip()
                    if comp not in comp_names:
                        comp_names[comp] = set()
                    if ht: comp_names[comp].add(ht)
                    if at: comp_names[comp].add(at)
        except Exception:
            pass
    return comp_names


def extract_espn_team_names_by_comp():
    comp_names = {}
    for competition, espn_id in sorted(ESPN_IDS.items()):
        try:
            if espn_id in CUP_SCOREBOARD_IDS:
                url = f"{ESPN_BASE}/{espn_id}/scoreboard"
                data = fetch_json(url, timeout=10)
                events = data.get("events") or []
                names = set()
                for event in events:
                    comps = event.get("competitions") or [{}]
                    for c in comps:
                        for competitor in c.get("competitors") or []:
                            display = str(competitor.get("team", {}).get("displayName", "")).strip()
                            if display: names.add(display)
                comp_names[competition] = names
                print(f"  OK   {competition} ({espn_id}): {len(events)} events")
            else:
                url = f"{ESPN_BASE}/{espn_id}/teams"
                data = fetch_json(url, timeout=10)
                leagues = (data.get("sports") or [{}])[0].get("leagues") or [{}]
                names = set()
                for league in leagues:
                    for team_entry in league.get("teams") or []:
                        display = str(team_entry.get("team", {}).get("displayName", "")).strip()
                        if display: names.add(display)
                comp_names[competition] = names
                print(f"  OK   {competition} ({espn_id}): {len(names)} teams")
        except Exception as e:
            print(f"  SKIP {competition} ({espn_id}): {e}")
    return comp_names


def read_mappings():
    if not os.path.isfile(MAPPING_FILE):
        return {}
    try:
        return tmg.load_team_mapping(MAPPING_FILE)
    except Exception:
        return {}


def main():
    all_ok = True

    csv_names = extract_csv_team_names()

    upcoming_by_comp = extract_upcoming_team_names_by_comp()
    espn_by_comp = extract_espn_team_names_by_comp()

    all_comps = sorted(set(upcoming_by_comp) | set(espn_by_comp))
    api_by_comp = {}
    for comp in all_comps:
        names = set()
        names.update(upcoming_by_comp.get(comp, set()))
        names.update(espn_by_comp.get(comp, set()))
        if names:
            api_by_comp[comp] = sorted(names)

    mappings = read_mappings()
    mapped_api_names = set()
    for comp, mapping in mappings.items():
        mapped_api_names.update(mapping.keys())

    comp_unmapped = {}
    flat_unmapped = set()
    for comp in sorted(api_by_comp):
        u = sorted(n for n in api_by_comp[comp] if n not in mapped_api_names)
        if u:
            comp_unmapped[comp] = u
            flat_unmapped.update(u)

    total_unmapped = len(flat_unmapped)

    tmp = tempfile.gettempdir()
    csv_out = os.path.join(tmp, "csv_team_names.txt")
    unmapped_out = os.path.join(tmp, "unmapped_names.txt")

    with open(csv_out, "w", encoding="utf-8") as f:
        for name in csv_names:
            f.write(name + "\n")

    with open(unmapped_out, "w", encoding="utf-8") as f:
        for comp in sorted(comp_unmapped):
            f.write(f"=== {comp} ===\n")
            for name in comp_unmapped[comp]:
                f.write(f"  {name}\n")
            f.write("\n")

    print(f"\ncsv_team_names.txt  -> {csv_out}  ({len(csv_names)} names)")
    print(f"unmapped_names.txt  -> {unmapped_out}  ({total_unmapped} names, "
          f"{len(comp_unmapped)} competitions)")


if __name__ == "__main__":
    main()
