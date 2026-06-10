import pandas as pd
import os
import json
import re
from collections import defaultdict
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROCESSED_DIR = os.path.join(BASE_DIR, "Data", "Processed_Data")
OUTPUT_DIR = os.path.join(BASE_DIR, "Data", "Team_Data")
SEASON_PATTERN = re.compile(r"^(?:[a-z0-9]+stat)(\d{4})-(\d{2})\.csv$", re.IGNORECASE)
MIN_START_YEAR = 2002
DEFAULT_LEAGUE_STRENGTH = {
    "England/Premier League": 1.00,
    "Spain/La Liga": 0.97,
    "Germany/Bundesliga": 0.96,
    "Italy/Serie A": 0.95,
    "France/Ligue 1": 0.92,
    "Portugal/Liga Portugal": 0.88,
    "England/Championship": 0.82,
    "Spain/La Liga 2": 0.78,
    "Germany/Bundesliga 2": 0.77,
    "Italy/Serie B": 0.76,
    "France/Ligue 2": 0.75,
}
H2H_RECENT_YEARS = 3
USE_GPU_DF = os.getenv("SOCCER_USE_GPU_DF", "1").strip().lower() not in {"0", "false", "no"}

try:
    import cudf  # type: ignore
except Exception:
    cudf = None
TEAM_NAME_ALIASES = {
    "man utd": "Man United",
    "manchester utd": "Man United",
    "manchester united": "Man United",
    "man city": "Man City",
    "manchester city": "Man City",
    "tottenham hotspur": "Tottenham",
    "spurs": "Tottenham",
    "wolverhampton wanderers": "Wolves",
    "wolverhampton": "Wolves",
    "newcastle united": "Newcastle",
    "newcastle utd": "Newcastle",
    "nottingham forest": "Nott'm Forest",
    "nottm forest": "Nott'm Forest",
}


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
    key = text.lower()
    return TEAM_NAME_ALIASES.get(key, text)


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
    end_year_two_digits = int(match.group(2))
    if end_year_two_digits != (start_year + 1) % 100:
        return None
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
        return 0.92
    if age == 2:
        return 0.84
    if age == 3:
        return 0.76
    if age == 4:
        return 0.70
    return 0.60

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
        # Weight last 5 matches 2x, matches 6-10 at 1x.
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
        h2h_coeff = coeff * (1.35 if age < H2H_RECENT_YEARS else 0.75)

        season_teams = defaultdict(blank_team_stats)

        for row in df.itertuples(index=False):
            home = canonical_team_name(row.HomeTeam)
            away = canonical_team_name(row.AwayTeam)
            if not is_valid_team_name(home) or not is_valid_team_name(away):
                continue

            hg = row.FTHG
            ag = row.FTAG

            hs = row.HS
            ass = row.AS

            hst = row.HST
            ast = row.AST

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
