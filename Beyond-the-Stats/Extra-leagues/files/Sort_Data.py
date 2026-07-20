"""Sort Extra Leagues team data — builds head-to-head + form files from processed CSVs."""
import pandas as pd
import os
import json
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from io import StringIO

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROCESSED_DIR = os.path.join(BASE_DIR, "Data", "Processed_Data")
OUTPUT_DIR = os.path.join(BASE_DIR, "Data", "Team_Data")
SEASON_PATTERN = re.compile(r"^(?:[a-z0-9]+stat)(\d{4})(?:-(\d{2}))?\.csv$", re.IGNORECASE)
MIN_START_YEAR = 2002
# Approximate relative-strength coefficients so every Extra-leagues
# competition isn't silently flattened to the same 0.85 fallback used by
# sort_all_seasons() below. European leagues here are informed by UEFA's
# national-association coefficient rankings; the non-European ones use a
# rough domestic-quality ordering. These only need to be broadly ordered
# correctly relative to the main global-pipeline leagues (see
# files/Sort_Data.py's DEFAULT_LEAGUE_STRENGTH for the top tiers).
DEFAULT_LEAGUE_STRENGTH = {
    "Brazil/Brasileirão": 0.75,
    "Argentina/Primera Division": 0.70,
    "Japan/J1 League": 0.60,
}
H2H_RECENT_YEARS = 2
USE_GPU_DF = os.getenv("SOCCER_USE_GPU_DF", "1").strip().lower() not in {"0", "false", "no"}

try:
    import cudf  # type: ignore
except Exception:
    cudf = None
MAPPING_FILE = os.path.join(BASE_DIR, "..", "..", "Data", "team_name_mapping_master.json")
_name_mapping_cache = None


def _load_name_mapping():
    global _name_mapping_cache
    if _name_mapping_cache is not None:
        return _name_mapping_cache
    try:
        with open(MAPPING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        flat = {}
        for comp, entries in data.items():
            if isinstance(entries, dict):
                for k, v in entries.items():
                    flat[k.strip().lower()] = v
        _name_mapping_cache = flat
    except Exception:
        _name_mapping_cache = {}
    return _name_mapping_cache


TRANSFERMARKT_SEARCH_URL = "https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={query}"
TRANSFERMARKT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}
TOP_MARKET_VALUE_FILE = "team_top_market_value_players.json"
TRANSFERMARKT_QUERY_ALIASES = {}
PLAYER_POSITION_SUFFIXES = [
    "Goalkeeper",
    "Centre-Back",
    "Left-Back",
    "Right-Back",
    "Defensive Midfield",
    "Central Midfield",
    "Attacking Midfield",
    "Left Midfield",
    "Right Midfield",
    "Left Winger",
    "Right Winger",
    "Second Striker",
    "Centre-Forward",
    "Striker",
]


def read_csv_fast(path):
    if USE_GPU_DF and cudf is not None:
        try:
            gdf = cudf.read_csv(path)
            return gdf.to_pandas()
        except Exception:
            pass
    return pd.read_csv(path)

# the basic storage for all of the stats to be stored
def blank_team_stats():
    return {
        "games": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "points": 0,

        "goals_scored": 0,
        "goals_conceded": 0,

        "home_games": 0,
        "away_games": 0,

        "home_wins": 0,
        "home_draws": 0,
        "home_losses": 0,

        "away_wins": 0,
        "away_draws": 0,
        "away_losses": 0,

        "home_goals_scored": 0,
        "home_goals_conceded": 0,
        "away_goals_scored": 0,
        "away_goals_conceded": 0,

        "shots_for": 0,
        "shots_against": 0,
        "shots_on_target_for": 0,
        "shots_on_target_against": 0,

        "home_shots_for": 0,
        "home_shots_against": 0,
        "home_sot_for": 0,
        "home_sot_against": 0,

        "away_shots_for": 0,
        "away_shots_against": 0,
        "away_sot_for": 0,
        "away_sot_against": 0
    }

# the function used to update a team with the stats in the processed file
def update_team(stats, gf, ga, shots_f, shots_a, sot_f, sot_a, result, is_home):
    stats["games"] += 1
    stats["goals_scored"] += gf
    stats["goals_conceded"] += ga
    stats["shots_for"] += shots_f
    stats["shots_against"] += shots_a
    stats["shots_on_target_for"] += sot_f
    stats["shots_on_target_against"] += sot_a

    if result == "W":
        stats["wins"] += 1
        stats["points"] += 3
    elif result == "D":
        stats["draws"] += 1
        stats["points"] += 1
    else:
        stats["losses"] += 1

    if is_home:
        stats["home_games"] += 1
        stats["home_goals_scored"] += gf
        stats["home_goals_conceded"] += ga
        stats["home_shots_for"] += shots_f
        stats["home_shots_against"] += shots_a
        stats["home_sot_for"] += sot_f
        stats["home_sot_against"] += sot_a

        if result == "W":
            stats["home_wins"] += 1
        elif result == "D":
            stats["home_draws"] += 1
        else:
            stats["home_losses"] += 1
    else:
        stats["away_games"] += 1
        stats["away_goals_scored"] += gf
        stats["away_goals_conceded"] += ga
        stats["away_shots_for"] += shots_f
        stats["away_shots_against"] += shots_a
        stats["away_sot_for"] += sot_f
        stats["away_sot_against"] += sot_a

        if result == "W":
            stats["away_wins"] += 1
        elif result == "D":
            stats["away_draws"] += 1
        else:
            stats["away_losses"] += 1


def update_team_weighted(stats, gf, ga, shots_f, shots_a, sot_f, sot_a, result, is_home, coeff):
    stats["games"] += coeff
    stats["goals_scored"] += gf * coeff
    stats["goals_conceded"] += ga * coeff
    stats["shots_for"] += shots_f * coeff
    stats["shots_against"] += shots_a * coeff
    stats["shots_on_target_for"] += sot_f * coeff
    stats["shots_on_target_against"] += sot_a * coeff

    if result == "W":
        stats["wins"] += coeff
        stats["points"] += 3 * coeff
    elif result == "D":
        stats["draws"] += coeff
        stats["points"] += 1 * coeff
    else:
        stats["losses"] += coeff

    if is_home:
        stats["home_games"] += coeff
        stats["home_goals_scored"] += gf * coeff
        stats["home_goals_conceded"] += ga * coeff
        stats["home_shots_for"] += shots_f * coeff
        stats["home_shots_against"] += shots_a * coeff
        stats["home_sot_for"] += sot_f * coeff
        stats["home_sot_against"] += sot_a * coeff
        if result == "W":
            stats["home_wins"] += coeff
        elif result == "D":
            stats["home_draws"] += coeff
        else:
            stats["home_losses"] += coeff
    else:
        stats["away_games"] += coeff
        stats["away_goals_scored"] += gf * coeff
        stats["away_goals_conceded"] += ga * coeff
        stats["away_shots_for"] += shots_f * coeff
        stats["away_shots_against"] += shots_a * coeff
        stats["away_sot_for"] += sot_f * coeff
        stats["away_sot_against"] += sot_a * coeff
        if result == "W":
            stats["away_wins"] += coeff
        elif result == "D":
            stats["away_draws"] += coeff
        else:
            stats["away_losses"] += coeff


# function used to calculate the average goals for/agains for a team
def calculate_averages(team_dict):
    for team, stats in team_dict.items():
        g = stats["games"]
        hg = stats["home_games"]
        ag = stats["away_games"]
        if g > 0:
            stats["avg_goals_scored"] = round(stats["goals_scored"] / g, 2)
            stats["avg_goals_conceded"] = round(stats["goals_conceded"] / g, 2)
            stats["avg_shots"] = round(stats["shots_for"] / g, 2)
            stats["avg_sot"] = round(stats["shots_on_target_for"] / g, 2)
            stats["avg_shots_conceded"] = round(stats["shots_against"] / g, 2)
        if hg > 0:
            stats["avg_home_goals_scored"] = round(stats["home_goals_scored"] / hg, 2)
            stats["avg_home_goals_conceded"] = round(stats["home_goals_conceded"] / hg, 2)
            stats["avg_home_shots_for"] = round(stats["home_shots_for"] / hg, 2)
            stats["avg_home_shots_against"] = round(stats["home_shots_against"] / hg, 2)
        if ag > 0:
            stats["avg_away_goals_scored"] = round(stats["away_goals_scored"] / ag, 2)
            stats["avg_away_goals_conceded"] = round(stats["away_goals_conceded"] / ag, 2)
            stats["avg_away_shots_for"] = round(stats["away_shots_for"] / ag, 2)
            stats["avg_away_shots_against"] = round(stats["away_shots_against"] / ag, 2)


def calculate_weighted_averages(team_dict):
    for _, stats in team_dict.items():
        g = stats["games"]
        hg = stats["home_games"]
        ag = stats["away_games"]
        if g > 0:
            stats["weighted_avg_goals_scored"] = round(stats["goals_scored"] / g, 3)
            stats["weighted_avg_goals_conceded"] = round(stats["goals_conceded"] / g, 3)
            stats["weighted_avg_shots_for"] = round(stats["shots_for"] / g, 3)
            stats["weighted_avg_shots_against"] = round(stats["shots_against"] / g, 3)
        if hg > 0:
            stats["weighted_avg_home_goals_scored"] = round(stats["home_goals_scored"] / hg, 3)
            stats["weighted_avg_home_goals_conceded"] = round(stats["home_goals_conceded"] / hg, 3)
            stats["weighted_avg_home_shots_for"] = round(stats["home_shots_for"] / hg, 3)
            stats["weighted_avg_home_shots_against"] = round(stats["home_shots_against"] / hg, 3)
        if ag > 0:
            stats["weighted_avg_away_goals_scored"] = round(stats["away_goals_scored"] / ag, 3)
            stats["weighted_avg_away_goals_conceded"] = round(stats["away_goals_conceded"] / ag, 3)
            stats["weighted_avg_away_shots_for"] = round(stats["away_shots_for"] / ag, 3)
            stats["weighted_avg_away_shots_against"] = round(stats["away_shots_against"] / ag, 3)


def safe_float(value):
    if pd.isna(value):
        return None
    return float(value)


def canonical_team_name(name):
    if pd.isna(name):
        return name
    text = str(name).strip()
    if not text:
        return text
    mapping = _load_name_mapping()
    return mapping.get(text.lower(), text)


def is_valid_team_name(name):
    if pd.isna(name):
        return False
    text = str(name).strip()
    if not text:
        return False
    return text.lower() != "nan"


def int_if_count_key(key, value):
    if isinstance(value, (int, float)) and "avg" not in key.lower():
        if pd.isna(value):
            return 0
        return int(round(float(value)))
    return value


def sanitize_overall_teams(overall_teams, weighted_overall):
    cleaned = {}
    for team, stats in overall_teams.items():
        entry = {}
        for key, value in stats.items():
            entry[key] = int_if_count_key(key, value)

        weighted_entry = weighted_overall.get(team, {})
        for key, value in weighted_entry.items():
            if not key.startswith("weighted_avg_"):
                continue
            if isinstance(value, (int, float)) and pd.isna(value):
                entry[key] = 0.0
            else:
                entry[key] = float(value)

        cleaned[team] = entry
    return cleaned


def sanitize_head_to_head(h2h_stats, weighted_h2h):
    cleaned = {}
    for home, away_map in h2h_stats.items():
        if home not in cleaned:
            cleaned[home] = {}
        for away, stats in away_map.items():
            if home == away:
                continue
            entry = {}
            for key, value in stats.items():
                if key == "points":
                    continue
                entry[key] = int_if_count_key(key, value)

            weighted_entry = weighted_h2h.get(home, {}).get(away, {})
            for key, value in weighted_entry.items():
                if key.startswith("weighted_avg_"):
                    entry[key] = float(value)

            cleaned[home][away] = entry
    return cleaned

# gets the year for the file
def parse_season_start_year(file_name):
    match = SEASON_PATTERN.match(file_name)
    if not match:
        return None

    start_year = int(match.group(1))
    if start_year < MIN_START_YEAR:
        return None
    if start_year > datetime.now().year:
        return None

    return start_year

def get_target_season_files():
    valid = []
    for root, _, files in os.walk(PROCESSED_DIR):
        for file_name in files:
            if not file_name.endswith(".csv"):
                continue
            start_year = parse_season_start_year(file_name)
            if start_year is not None:
                full_path = os.path.join(root, file_name)
                rel_path = os.path.relpath(full_path, PROCESSED_DIR)
                valid.append((start_year, rel_path))
    valid.sort(key=lambda item: item[0])
    return [name for _, name in valid]


def season_recency_coefficient(age):
    if age <= 0:
        return 1.00
    if age == 1:
        return 0.80
    if age == 2:
        return 0.55
    if age == 3:
        return 0.35
    if age == 4:
        return 0.22
    return 0.12


def parse_market_value_to_eur(value_text):
    if pd.isna(value_text):
        return 0
    text = str(value_text).strip().lower().replace(",", ".")
    if not text or text == "-":
        return 0
    text = text.replace("€", "").replace("eur", "").strip()
    multiplier = 1
    if text.endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    elif text.endswith("k"):
        multiplier = 1_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except Exception:
        return 0


def normalize_player_key(name):
    text = str(name).strip().lower()
    text = (
        text.replace("á", "a")
        .replace("à", "a")
        .replace("ä", "a")
        .replace("â", "a")
        .replace("é", "e")
        .replace("è", "e")
        .replace("ë", "e")
        .replace("ê", "e")
        .replace("í", "i")
        .replace("ì", "i")
        .replace("ï", "i")
        .replace("î", "i")
        .replace("ó", "o")
        .replace("ò", "o")
        .replace("ö", "o")
        .replace("ô", "o")
        .replace("ú", "u")
        .replace("ù", "u")
        .replace("ü", "u")
        .replace("û", "u")
        .replace("ñ", "n")
        .replace("ç", "c")
    )
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_position_suffix(player_name):
    name = str(player_name).strip()
    changed = True
    while changed:
        changed = False
        for suffix in PLAYER_POSITION_SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)].strip()
                changed = True
    return name


def detect_position_from_raw_player_text(raw_text):
    text = str(raw_text).strip().lower()
    if "winger" in text or "striker" in text or "centre-forward" in text or "second striker" in text:
        return "attack"
    if "attacking midfield" in text or "midfield" in text:
        return "midfield"
    if "goalkeeper" in text:
        return "goalkeeper"
    if "back" in text or "defender" in text or "centre-back" in text:
        return "defense"
    return "unknown"

def _normalize_column_name(col):
    if isinstance(col, tuple):
        parts = [str(part).strip() for part in col if str(part).strip().lower() not in {"", "nan", "none"}]
        text = " ".join(parts).strip()
    else:
        text = str(col).strip()
    return re.sub(r"\s+", " ", text).strip().lower()


def _find_player_market_columns(table):
    player_col = None
    market_col = None
    for col in table.columns:
        norm = _normalize_column_name(col)
        if player_col is None and "player" in norm:
            player_col = col
        if market_col is None and "market" in norm and "value" in norm:
            market_col = col
    return player_col, market_col


def fetch_injured_player_keys(club_url, club_id):
    injury_url = club_url.replace("/startseite/verein/", "/sperrenundverletzungen/verein/")
    request = urllib.request.Request(injury_url, headers=TRANSFERMARKT_HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="ignore")

    tables = pd.read_html(StringIO(html))
    injured = set()
    for table in tables:
        cols = [str(col) for col in table.columns]
        if "Player" not in cols:
            continue
        for raw_name in table["Player"].dropna().tolist():
            candidate = strip_position_suffix(raw_name)
            if not candidate or candidate.lower() == "injuries":
                continue
            injured.add(normalize_player_key(candidate))
    return injured


def find_transfermarkt_club_link(team_name):
    query = TRANSFERMARKT_QUERY_ALIASES.get(team_name, team_name)
    encoded = urllib.parse.quote(query)
    url = TRANSFERMARKT_SEARCH_URL.format(query=encoded)
    request = urllib.request.Request(url, headers=TRANSFERMARKT_HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="ignore")

    match = re.search(r'href="(/[^"#]+/startseite/verein/(\d+))"', html)
    if not match:
        return None, None
    relative = match.group(1)
    club_id = match.group(2)
    return f"https://www.transfermarkt.com{relative}", club_id


def fetch_top_market_value_players(club_url, club_id, season_id, injured_player_keys):
    # Use squad page for current season to get player market values in one request.
    squad_url = club_url.replace("/startseite/verein/", "/kader/verein/") + f"/saison_id/{season_id}"
    candidate_urls = [squad_url]
    if season_id > 2000:
        candidate_urls.append(club_url.replace("/startseite/verein/", "/kader/verein/") + f"/saison_id/{season_id - 1}")

    player_table = None
    for candidate in candidate_urls:
        request = urllib.request.Request(candidate, headers=TRANSFERMARKT_HEADERS)
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="ignore")

        tables = pd.read_html(StringIO(html))
        for table in tables:
            player_col, market_col = _find_player_market_columns(table)
            if player_col is not None and market_col is not None:
                player_table = table[[player_col, market_col]].copy()
                player_table.columns = ["Player", "Market value"]
                break
        if player_table is not None and not player_table.empty:
            break

    if player_table is None or player_table.empty:
        return []

    work = player_table[["Player", "Market value"]].copy()
    work["position_group"] = work["Player"].map(detect_position_from_raw_player_text)
    work["player_clean"] = work["Player"].map(strip_position_suffix)
    work["player_key"] = work["player_clean"].map(normalize_player_key)
    work["market_value_eur"] = work["Market value"].map(parse_market_value_to_eur)
    work = work[work["market_value_eur"] > 0]
    if injured_player_keys:
        work = work[~work["player_key"].isin(injured_player_keys)]
    if work.empty:
        return []
    work = work.drop_duplicates(subset=["player_key"]).sort_values("market_value_eur", ascending=False).head(5)

    top_players = []
    for _, row in work.iterrows():
        top_players.append(
            {
                "player": str(row["player_clean"]).strip(),
                "position_group": str(row["position_group"]).strip(),
                "market_value": str(row["Market value"]).strip(),
                "market_value_eur": int(row["market_value_eur"]),
            }
        )
    return top_players


def build_top_market_value_players_file():
    files = get_target_season_files()
    if not files:
        raise ValueError("No processed season CSV files found.")

    latest_file = files[-1]
    latest_file_norm = latest_file.replace("\\", "/").lower()
    if "mexico" in latest_file_norm or "liga mx" in latest_file_norm or "ligamx" in latest_file_norm:
        output = {
            "season": latest_file.replace(".csv", ""),
            "source_file": latest_file,
            "generated_at_utc": datetime.utcnow().replace(microsecond=0).isoformat(),
            "skipped": True,
            "reason": "Market values disabled for Mexico/Liga MX.",
            "teams": {},
        }
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, TOP_MARKET_VALUE_FILE), "w", encoding="utf-8") as file:
            json.dump(output, file, indent=4)
        print("Skipped market value fetch for Mexico/Liga MX.")
        return
    latest_path = os.path.join(PROCESSED_DIR, latest_file)
    latest_start_year = parse_season_start_year(os.path.basename(latest_file)) or datetime.now().year
    df = read_csv_fast(latest_path)
    teams = sorted(set(df["HomeTeam"].dropna()) | set(df["AwayTeam"].dropna()))

    output = {
        "season": latest_file.replace(".csv", ""),
        "source_file": latest_file,
        "generated_at_utc": datetime.utcnow().replace(microsecond=0).isoformat(),
        "teams": {},
    }

    for team in teams:
        team_name = str(team).strip()
        if not team_name:
            continue
        try:
            club_url, club_id = find_transfermarkt_club_link(team_name)
            if not club_url or not club_id:
                output["teams"][team_name] = {
                    "status": "club_not_found",
                    "top_5_players": [],
                }
                continue

            try:
                injured_player_keys = fetch_injured_player_keys(club_url, club_id)
            except Exception:
                injured_player_keys = set()
            top_players = fetch_top_market_value_players(club_url, club_id, latest_start_year, injured_player_keys)
            output["teams"][team_name] = {
                "status": "ok" if top_players else "no_values_found",
                "club_id": club_id,
                "club_url": club_url,
                "injured_players_excluded": len(injured_player_keys),
                "top_5_players": top_players,
            }
            print(f"Market values: {team_name} ({len(top_players)} players)")
        except Exception:
            output["teams"][team_name] = {
                "status": "fetch_failed",
                "top_5_players": [],
            }
            print(f"Market values: {team_name} (failed)")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, TOP_MARKET_VALUE_FILE), "w", encoding="utf-8") as file:
        json.dump(output, file, indent=4)

    print(f"Top market value players written to {TOP_MARKET_VALUE_FILE}")

# function used to build a file to store the current (last 10 game) stats for each team in the current season
def build_current_form_file():
    files = get_target_season_files()
    if not files:
        raise ValueError("No processed season CSV files found.")

    latest_file = files[-1]
    latest_path = os.path.join(PROCESSED_DIR, latest_file)
    df = read_csv_fast(latest_path)

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.sort_values("Date")

    team_matches = defaultdict(list)

    for row in df.itertuples(index=False):
        home = canonical_team_name(row.HomeTeam)
        away = canonical_team_name(row.AwayTeam)
        if not is_valid_team_name(home) or not is_valid_team_name(away):
            continue

        hg = row.FTHG
        ag = row.FTAG
        result = row.FTR

        avg_h = getattr(row, "AvgH", None)
        avg_d = getattr(row, "AvgD", None)
        avg_a = getattr(row, "AvgA", None)

        if result == "H":
            home_res = "W"
            away_res = "L"
        elif result == "A":
            home_res = "L"
            away_res = "W"
        else:
            home_res = "D"
            away_res = "D"

        team_matches[home].append(
            {
                "result": home_res,
                "gf": hg,
                "ga": ag,
                "win_odds": safe_float(avg_h),
                "draw_odds": safe_float(avg_d),
                "lose_odds": safe_float(avg_a),
            }
        )
        team_matches[away].append(
            {
                "result": away_res,
                "gf": ag,
                "ga": hg,
                "win_odds": safe_float(avg_a),
                "draw_odds": safe_float(avg_d),
                "lose_odds": safe_float(avg_h),
            }
        )

    current_form = {"season": latest_file.replace(".csv", ""), "source_file": latest_file, "teams": {}}

    for team, matches in team_matches.items():
        recent = matches[-10:]
        total_weight = 0.0
        weighted_wins = 0.0
        weighted_draws = 0.0
        weighted_losses = 0.0
        weighted_points = 0.0
        weighted_gf = 0.0
        weighted_ga = 0.0
        for idx, match in enumerate(recent):
            w = 2.0 if idx >= len(recent) - 5 else 1.0
            total_weight += w
            result = match["result"]
            if result == "W":
                weighted_wins += w
            elif result == "D":
                weighted_draws += w
            elif result == "L":
                weighted_losses += w
            weighted_points += w * (3 if result == "W" else 1 if result == "D" else 0)
            weighted_gf += w * match["gf"]
            weighted_ga += w * match["ga"]

        wins = round(weighted_wins * 10.0 / total_weight) if total_weight > 0 else 0
        draws = round(weighted_draws * 10.0 / total_weight) if total_weight > 0 else 0
        losses = round(weighted_losses * 10.0 / total_weight) if total_weight > 0 else 0
        points = round(weighted_points * 10.0 / total_weight) if total_weight > 0 else 0
        goals_for = round(weighted_gf * 10.0 / total_weight, 2) if total_weight > 0 else 0.0
        goals_against = round(weighted_ga * 10.0 / total_weight, 2) if total_weight > 0 else 0.0

        last_game = matches[-1] if matches else {}

        current_form["teams"][team] = {
            "games_played": len(matches),
            "form_last_10": "".join(match["result"] for match in recent),
            "wins_last_10": wins,
            "draws_last_10": draws,
            "losses_last_10": losses,
            "points_last_10": points,
            "avg_goals_for_last_10": goals_for / 10.0 if recent else 0.0,
            "avg_goals_against_last_10": goals_against / 10.0 if recent else 0.0,
            "previous_match_win_odds": last_game.get("win_odds"),
            "previous_match_draw_odds": last_game.get("draw_odds"),
            "previous_match_lose_odds": last_game.get("lose_odds"),
        }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "current_form.json"), "w") as file:
        json.dump(current_form, file, indent=4)

    print(f"Current form written from {latest_file}")

# function used to sort the data and store for each season and team
# thsi function is the main one called that completes the entiree task
def sort_all_seasons():
    overall_teams = defaultdict(blank_team_stats)
    weighted_overall = defaultdict(blank_team_stats)
    season_data = {}
    h2h_stats = defaultdict(lambda: defaultdict(blank_team_stats))
    weighted_h2h = defaultdict(lambda: defaultdict(blank_team_stats))
    competitions = set()

    files = get_target_season_files()
    latest_year = max(parse_season_start_year(os.path.basename(file)) for file in files) if files else MIN_START_YEAR

    for rel_path in files:
        print(f"Processing {rel_path}")
        competition = os.path.dirname(rel_path).replace("\\", "/") or "Unknown"
        competitions.add(competition)
        path = os.path.join(PROCESSED_DIR, rel_path)
        df = read_csv_fast(path)
        season_start_year = parse_season_start_year(os.path.basename(rel_path))
        age = max(0, latest_year - season_start_year)
        coeff = season_recency_coefficient(age)
        h2h_coeff = coeff * (1.55 if age < H2H_RECENT_YEARS else 0.50)

        season_teams = defaultdict(blank_team_stats)

        for row in df.itertuples(index=False):
            home = canonical_team_name(row.HomeTeam)
            away = canonical_team_name(row.AwayTeam)
            if not is_valid_team_name(home) or not is_valid_team_name(away):
                continue

            hg = row.FTHG
            ag = row.FTAG

            hs = 0 if pd.isna(row.HS) else row.HS
            ass = 0 if pd.isna(row.AS) else row.AS

            hst = 0 if pd.isna(row.HST) else row.HST
            ast = 0 if pd.isna(row.AST) else row.AST

            result = row.FTR

            if result == "H":
                home_res = "W"
                away_res = "L"
            elif result == "A":
                home_res = "L"
                away_res = "W"
            else:
                home_res = away_res = "D"

            # update the stats for the teams overall, season, and head to head stats
            update_team(season_teams[home], hg, ag, hs, ass, hst, ast, home_res, True)
            update_team(season_teams[away], ag, hg, ass, hs, ast, hst, away_res, False)

            update_team(overall_teams[home], hg, ag, hs, ass, hst, ast, home_res, True)
            update_team(overall_teams[away], ag, hg, ass, hs, ast, hst, away_res, False)
            update_team_weighted(weighted_overall[home], hg, ag, hs, ass, hst, ast, home_res, True, coeff)
            update_team_weighted(weighted_overall[away], ag, hg, ass, hs, ast, hst, away_res, False, coeff)

            if home != away:
                update_team(h2h_stats[home][away], hg, ag, hs, ass, hst, ast, home_res, True)
                update_team(h2h_stats[away][home], ag, hg, ass, hs, ast, hst, away_res, False)
                update_team_weighted(weighted_h2h[home][away], hg, ag, hs, ass, hst, ast, home_res, True, h2h_coeff)
                update_team_weighted(weighted_h2h[away][home], ag, hg, ass, hs, ast, hst, away_res, False, h2h_coeff)

        calculate_averages(season_teams)
        season_data[rel_path.replace(".csv", "")] = season_teams

    calculate_averages(overall_teams)
    calculate_weighted_averages(weighted_overall)
    for home in weighted_h2h:
        calculate_weighted_averages(weighted_h2h[home])

    cleaned_overall = sanitize_overall_teams(overall_teams, weighted_overall)
    cleaned_h2h = sanitize_head_to_head(h2h_stats, weighted_h2h)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open(os.path.join(OUTPUT_DIR, "overall_teams.json"), "w") as f:
        json.dump(cleaned_overall, f, indent=4)
    with open(os.path.join(OUTPUT_DIR, "season_teams.json"), "w") as f:
        json.dump(season_data, f, indent=4)
    with open(os.path.join(OUTPUT_DIR, "head_to_head.json"), "w") as f:
        json.dump(cleaned_h2h, f, indent=4)
    league_strength_path = os.path.join(OUTPUT_DIR, "league_strength.json")
    if not os.path.exists(league_strength_path):
        league_strength = {}
        for competition in sorted(competitions):
            league_strength[competition] = DEFAULT_LEAGUE_STRENGTH.get(competition, 0.85)
        with open(league_strength_path, "w") as f:
            json.dump(league_strength, f, indent=4)

    print("\nDone!\n")


# testing
if __name__ == "__main__":
    sort_all_seasons()
    build_current_form_file()
    build_top_market_value_players_file()
