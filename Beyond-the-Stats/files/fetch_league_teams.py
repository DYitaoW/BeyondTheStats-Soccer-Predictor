"""One-time fetch: persist team rosters per league from ESPN.

Saves to Data/Predictions/league_teams.json so the predictor can know
which teams belong to each league even during the offseason when no
scheduled games exist.

Usage:  python files/fetch_league_teams.py
"""
import json, os, sys, time, urllib.request, urllib.error

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# All leagues with valid ESPN slugs.
# Main + cup leagues from LIVE_SCORE_COMPETITIONS, plus extra leagues
# that have ESPN IDs even though they aren't live-polled.
LEAGUES = {
    # Club leagues (top European + MLS)
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
    "Belgium/First Division A": "bel.1",
    "Scotland/Premiership": "sco.1",
    "Turkey/Super Lig": "tur.1",
    "United States/MLS": "usa.1",
    "Mexico/Liga MX": "mex.1",
    "North America/Leagues Cup": "concacaf.leagues.cup",
    # Extra leagues (have ESPN IDs but no live polling)
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
    # Domestic cups
    "England/FA Cup": "eng.fa",
    "England/League Cup": "eng.efl",
    "Italy/Coppa Italia": "ita.coppa",
    "Spain/Copa del Rey": "esp.copa_del_rey",
    "Germany/DFB-Pokal": "ger.dfb_pokal",
    "France/Coupe de France": "fra.coupe_de_france",
    "United States/US Open Cup": "usa.open_cup",
    "North America/Leagues Cup": "concacaf.leagues.cup",
    # UEFA club competitions
    "Europe/Champions League": "uefa.champions",
    "Europe/Europa League": "uefa.europa",
    "Europe/Conference League": "uefa.europa.conf",
    # National team
    "International/World Cup": "fifa.world",
    "International/World Cup Qualifying - UEFA": "fifa.worldq.uefa",
    "International/World Cup Qualifying - CONMEBOL": "fifa.worldq.conmebol",
    "International/World Cup Qualifying - CONCACAF": "fifa.worldq.concacaf",
    "International/World Cup Qualifying - AFC": "fifa.worldq.afc",
    "International/Friendly": "fifa.friendly",
    "International/European Championship": "uefa.euro",
    "International/Nations League": "uefa.nations",
    "South America/Copa America": "conmebol.america",
    "North America/Gold Cup": "concacaf.gold",
    "Africa/Africa Cup of Nations": "caf.nations",
    "Asia/Asian Cup": "afc.cup",
}

# Load team name mapping (ESPN display name → canonical name)
MAPPING_FILE = os.path.join(PROJECT_DIR, "..", "Data", "team_name_mapping_master.json")
mapping = {}
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    for comp, comp_map in raw.items():
        if isinstance(comp_map, dict):
            # Reverse: ESPN display_name → canonical_name
            mapping[comp] = {v: k for k, v in comp_map.items()}


def fetch_json(url, retries=2):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
                continue
            print(f"    [FAIL] {e}")
            return None


def fetch_teams(espn_id):
    url = f"{ESPN_BASE}/{espn_id}/teams"
    data = fetch_json(url)
    if not data:
        return []
    teams = []
    for league in (data.get("sports") or [{}])[0].get("leagues") or []:
        for t in league.get("teams") or []:
            entry = t.get("team", t)
            name = str(entry.get("displayName", "")).strip()
            if name:
                teams.append(name)
    return teams


def map_name(comp_name, espn_name):
    """Map ESPN display name to canonical CSV name."""
    comp_mapping = mapping.get(comp_name, {})
    if espn_name in comp_mapping:
        return comp_mapping[espn_name]
    # Normalized fallback
    norm = espn_name.lower().replace(" fc", "").replace(" afc", "").strip()
    for display_name, canonical_name in comp_mapping.items():
        if display_name.lower().replace(" fc", "").replace(" afc", "").strip() == norm:
            return canonical_name
    return espn_name


def main():
    output_file = os.path.join(PROJECT_DIR, "Data", "Predictions", "league_teams.json")
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    all_teams = {}
    total = len(LEAGUES)
    for idx, (comp_name, espn_id) in enumerate(sorted(LEAGUES.items()), 1):
        print(f"[{idx}/{total}] {comp_name} ({espn_id})...")
        raw_teams = fetch_teams(espn_id)
        if not raw_teams:
            print(f"    -> 0 teams (skipping)")
            continue
        canonical = sorted(set(map_name(comp_name, t) for t in raw_teams))
        all_teams[comp_name] = canonical
        print(f"    -> {len(canonical)} teams")
        time.sleep(0.3)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_teams, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {len(all_teams)} leagues to {output_file}")


if __name__ == "__main__":
    main()
