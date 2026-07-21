"""Configuration constants for the Beyond the Stats Flask application.

File paths, ESPN competition IDs, cache TTLs, and environment variables.
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ── Directory Structure ────────────────────────────────────────────

WEBSITE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(WEBSITE_DIR)
LAST_REFRESH_FILE = os.path.join(PROJECT_DIR, "Data", "last_refresh.json")
FILES_DIR = os.path.join(PROJECT_DIR, "files")
MLS_FILES_DIR = os.path.join(PROJECT_DIR, "MLS", "files")
EXTRA_FILES_DIR = os.path.join(PROJECT_DIR, "Extra-leagues", "files")
WEBSITE_FILES_DIR = os.path.join(WEBSITE_DIR, "files")
GRAPHICS_DIR = os.path.join(WEBSITE_DIR, "graphics")
ACCURACY_TOTALS_FILE = os.path.join(WEBSITE_FILES_DIR, "accuracy_totals.json")

# ── Prediction Files ──────────────────────────────────────────────

GLOBAL_UPCOMING_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_matchweek_predictions.csv")
GLOBAL_PROJECTED_MATCHES_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "projected_future_matches.csv")
ALL_UPCOMING_FILE = os.path.join(PROJECT_DIR, "Output", "Upcoming", "all_upcoming.csv")
FOUR_WEEK_WINDOW_FILE = os.path.join(PROJECT_DIR, "Output", "Upcoming", "four_week_window.csv")
CUP_UPCOMING_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_cup_predictions.csv")
CUP_COMPLETED_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "completed_cup_predictions.csv")
MLS_UPCOMING_FILE = os.path.join(PROJECT_DIR, "MLS", "Data", "Predictions", "upcoming_matchweek_predictions.csv")
EXTRA_UPCOMING_FILE = os.path.join(PROJECT_DIR, "Extra-leagues", "Data", "Predictions", "upcoming_matchweek_predictions.csv")
EXTRA_PROJECTED_MATCHES_FILE = os.path.join(PROJECT_DIR, "Extra-leagues", "Data", "Predictions", "projected_future_matches.csv")
NATIONAL_UPCOMING_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_national_team_predictions.csv")
FRIENDLIES_UPCOMING_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_club_friendlies.csv")
GLOBAL_PROJECTED_TABLE_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "projected_league_tables.csv")
CUP_PROJECTED_TABLE_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "projected_cup_tables.csv")
CUP_PROJECTED_BRACKET_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "projected_cup_brackets.json")
PAST_GAMES_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "past_games.json")
MLS_PROJECTED_TABLE_FILE = os.path.join(PROJECT_DIR, "MLS", "Data", "Predictions", "projected_league_tables.csv")
EXTRA_PROJECTED_TABLE_FILE = os.path.join(PROJECT_DIR, "Extra-leagues", "Data", "Predictions", "projected_league_tables.csv")
MLS_PROJECTED_BRACKET_FILE = os.path.join(PROJECT_DIR, "MLS", "Data", "Predictions", "projected_mls_playoff_bracket.json")

# ── Pipeline & Data Files ─────────────────────────────────────────

LIVE_RESULTS_UPDATER = os.path.join(FILES_DIR, "Update_Live_Prediction_Results.py")
RUN_ALL_PIPELINE = os.path.join(PROJECT_DIR, "Run_All_Pipeline.py")

# Set to "0" / "false" to disable pipeline execution entirely.
# Useful during development/debugging so /api/refresh and /api/retrain
# return a clear "disabled" response instead of trying to run the pipeline.
PIPELINE_ENABLED = os.environ.get("PIPELINE_ENABLED", "1").strip().lower() in {"1", "true", "yes"}

# If True, _team_name_for_display / _team_name_for_db in team_utils
# will map canonical names through the display-mapping file.
USE_DISPLAY_NAME_MAPPING = False

LAST_DATA_REFRESH_FILE = os.path.join(PROJECT_DIR, "Data", "last_data_refresh.json")
PIPELINE_STATUS_FILE = os.path.join(PROJECT_DIR, "Data", "pipeline_status.json")
BACKEND_RUN_STATUS_FILE = os.path.join(PROJECT_DIR, "Data", "backend_run_status.json")
PIPELINE_LOG_FILE = os.path.join(PROJECT_DIR, "Data", "pipeline_latest.log")
TEAM_NAME_DISPLAY_MAPPING_FILE = os.path.join(PROJECT_DIR, "..", "Data", "team_name_mapping_master.json")
TOP_SCORERS_FILE = os.path.join(PROJECT_DIR, "Data", "Team_Data", "current_season_top_scorers.json")
LIVE_SCORE_HISTORY_FILE = os.path.join(PROJECT_DIR, "Data", "live_score_history.json")
PREDICTION_TRACKING_FILE = os.path.join(PROJECT_DIR, "Data", "prediction_tracking.json")
REAL_TABLES_PERSIST_FILE = os.path.join(PROJECT_DIR, "Data", "standings_cache.json")
LEAGUE_TEAMS_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "league_teams.json")
CURRENT_SEASON_TEAMS_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "current_season_teams.json")
WORLD_CUP_PROJECTION_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "world_cup_projection.json")

# ── League-Data Cache (pre-computed at pipeline / cached on read) ──

LEAGUE_DATA_DIR = os.path.join(PROJECT_DIR, "Output", "LeagueData")

# ── Info Pages (changes / roadmap / upcoming) ───────────────────────

INFO_DIR = os.path.join(PROJECT_DIR, "Data", "Info")
INFO_CHANGES_FILE = os.path.join(INFO_DIR, "changes.json")
INFO_ROADMAP_FILE = os.path.join(INFO_DIR, "roadmap.json")
INFO_UPCOMING_FILE = os.path.join(INFO_DIR, "upcoming.json")

UPCOMING_CSV_FILES = {
    "global": GLOBAL_UPCOMING_FILE,
    "mls": MLS_UPCOMING_FILE,
    "extra": EXTRA_UPCOMING_FILE,
    "cups": CUP_UPCOMING_FILE,
    "national": NATIONAL_UPCOMING_FILE,
    "friendlies": FRIENDLIES_UPCOMING_FILE,
}

# ── Cache Configuration ───────────────────────────────────────────

REDIS_URL = os.environ.get("REDIS_URL", "")
CACHE_TTL_DEFAULT = int(os.environ.get("CACHE_TTL_DEFAULT", "120"))  # seconds
CACHE_TTL_LONG = int(os.environ.get("CACHE_TTL_LONG", "600"))  # 10 min
REAL_TABLES_CACHE_TTL = 300  # 5 minutes
REAL_LEADERS_CACHE_TTL = 300  # 5 minutes
_API_CACHE_MAX_AGE = int(os.environ.get("API_CACHE_MAX_AGE", "300"))
_STATIC_CACHE_MAX_AGE = int(os.environ.get("STATIC_CACHE_MAX_AGE", "86400"))
STATIC_PREDICTIONS_CACHE = os.environ.get("STATIC_PREDICTIONS_CACHE", "0").strip().lower() in {"1", "true", "yes"}

# ── Feature Flags ─────────────────────────────────────────────────

# Prefer precomputed prediction CSVs for /api/predict and team lists.
STATIC_PREDICTIONS = os.environ.get("STATIC_PREDICTIONS", "1").strip().lower() in {"1", "true", "yes"}
# When loading large static CSVs, keep only the columns needed for display.
LOW_MEMORY_STATIC = os.environ.get("LOW_MEMORY_STATIC", "1").strip().lower() in {"1", "true", "yes"}
STATIC_PREDICTIONS_GLOBAL_FILE = os.environ.get("STATIC_PREDICTIONS_GLOBAL_FILE", GLOBAL_UPCOMING_FILE)
STATIC_PREDICTIONS_MLS_FILE = os.environ.get("STATIC_PREDICTIONS_MLS_FILE", MLS_UPCOMING_FILE)
STATIC_PREDICTIONS_EXTRA_FILE = os.environ.get("STATIC_PREDICTIONS_EXTRA_FILE", EXTRA_UPCOMING_FILE)

# ── Live Score Polling ────────────────────────────────────────────

LIVE_SCORE_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

CLUB_FRIENDLIES_COMPETITION = "Club Friendlies"
CLUB_FRIENDLIES_ESPN_ID = "club.friendly"

# Competitions polled for scores but excluded from league-table/help listings.
UPCOMING_ONLY_COMPETITIONS = {
    "FIFA/Friendly",
}

# Domestic leagues used only for UEFA/cup roster fallback (or upcoming API with no
# domestic model history). Kept in fetch_league_teams / UEFA_Data_Manager but
# omitted from league-facing APIs (tables, upcoming-global, league-data, leaders).
LEAGUE_API_EXCLUDED_COMPETITIONS = {
    # football-data.org upcoming fixtures only — no domestic prediction history
    "Switzerland/Super League",
    "Ukraine/Premier League",
    "Croatia/HNL",
    "Hungary/NB I",
    "Israel/Premier League",
    # UEFA/cup fallback roster only — no upcoming API, no domestic history
    "Czech Republic/First League",
    "Serbia/SuperLiga",
    "Cyprus/First Division",
    "Slovakia/Super Liga",
    "Slovenia/PrvaLiga",
    "Bulgaria/First League",
}

LIVE_SCORE_COMPETITIONS = {
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
    "United States/MLS": "usa.1",
    "CONCACAF/Leagues Cup": "concacaf.leagues.cup",
    # Domestic cups
    "England/FA Cup": "eng.fa",
    "England/League Cup": "eng.efl",
    "UEFA/Champions League": "uefa.champions",
    "UEFA/Europa League": "uefa.europa",
    "UEFA/Conference League": "uefa.europa.conf",
    # Europe/ prefix aliases (used by Predict_Upcoming_Matchweek short codes)
    "Europe/Champions League": "uefa.champions",
    "Europe/Europa League": "uefa.europa",
    "Europe/Conference League": "uefa.europa.conf",
    # Domestic cups (predictions pipeline produces these)
    "Italy/Coppa Italia": "ita.coppa",
    "Spain/Copa del Rey": "esp.copa_del_rey",
    "Germany/DFB-Pokal": "ger.dfb_pokal",
    "France/Coupe de France": "fra.coupe_de_france",
    "United States/US Open Cup": "usa.open_cup",
    "CONCACAF/Leagues Cup": "concacaf.leagues.cup",
    CLUB_FRIENDLIES_COMPETITION: CLUB_FRIENDLIES_ESPN_ID,
    # National team & World Cup
    "FIFA/World Cup": "fifa.world",
    "FIFA/Friendly": "fifa.friendly",
    # Mexico/Liga MX, Belgium/First Division A, Scotland/Premiership, Turkey/Super Lig
    # are result-only (post-match fetch, no in-play polling).
    "Mexico/Liga MX": "mex.1",
    "Belgium/First Division A": "bel.1",
    "Scotland/Premiership": "sco.1",
    "Turkey/Super Lig": "tur.1",
    # Extra leagues — result-only (post-match fetch, no in-play polling).
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
}


# UEFA club competitions: show qualifying fixtures in upcoming, but defer
# in-play live scoring until the main group/league phase (September).
UEFA_MAIN_STAGE_LIVE_FROM = "2026-09-01"
UEFA_LIVE_SCORE_COMPETITIONS = frozenset({
    "UEFA/Champions League",
    "UEFA/Europa League",
    "UEFA/Conference League",
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
})

# Competitions that should only get post-match result fetch (no in-play polling).
# These are polled during idle cycles, 5-15 min after expected match end (~110 min after start).
RESULT_ONLY_COMPETITIONS = frozenset({
    "Mexico/Liga MX",
    "Belgium/First Division A",
    "Scotland/Premiership",
    "Turkey/Super Lig",
    "Austria/Bundesliga",
    "Switzerland/Super League",
    "Greece/Super League",
    "Denmark/Danish Superliga",
    "Ukraine/Premier League",
    "Norway/Eliteserien",
    "Croatia/HNL",
    "Romania/Liga I",
    "Sweden/Allsvenskan",
    "Hungary/NB I",
    "Israel/Premier League",
    "Czech Republic/First League",
    "Poland/Ekstraklasa",
    "Serbia/SuperLiga",
    "Cyprus/First Division",
    "Slovakia/Super Liga",
    "Slovenia/PrvaLiga",
    "Bulgaria/First League",
})

# Second-division leagues: only poll for HT result and FT result (no in-play tracking).
REDUCED_POLLING_COMPETITIONS = frozenset({
    "Spain/La Liga 2",
    "Italy/Serie B",
    "Germany/Bundesliga 2",
    "France/Ligue 2",
})

# Domestic cups that should prefer ESPN fixtures and skip synthetic fallbacks.
CUP_ESPN_FIRST_COMPETITIONS = frozenset({
    "Spain/Copa del Rey",
    "France/Coupe de France",
    "Italy/Coppa Italia",
    "Germany/DFB-Pokal",
})
CUP_NO_PRESEASON_FALLBACK = CUP_ESPN_FIRST_COMPETITIONS
CUP_SKIP_THESPORTSDB = CUP_ESPN_FIRST_COMPETITIONS


def uefa_live_scoring_allowed(as_of=None):
    """Return True when UEFA club competitions should receive in-play live scores."""
    from datetime import date

    cutoff = date.fromisoformat(UEFA_MAIN_STAGE_LIVE_FROM)
    today = as_of or date.today()
    return today >= cutoff


def competition_live_aliases(competition: str) -> set[str]:
    """Return equivalent competition labels used across pipelines."""
    comp = str(competition or "").strip()
    aliases = {comp} if comp else set()
    if comp.startswith("Europe/"):
        aliases.add(comp.replace("Europe/", "UEFA/", 1))
    elif comp.startswith("UEFA/"):
        aliases.add(comp.replace("UEFA/", "Europe/", 1))
    return aliases

# ── Competitions ──────────────────────────────────────────────────

MLS_COMPETITION = "United States/MLS"
MLS_CUP_COMPETITION = "United States/MLS - MLS Cup"
LIGA_MX_COMPETITION = "Mexico/Liga MX"
MLS_DATASET_COMPETITIONS = (
    MLS_COMPETITION,
    "United States/MLS - Supporters Shield Table",
    "United States/MLS - Eastern Conference",
    "United States/MLS - Western Conference",
    LIGA_MX_COMPETITION,
)
GLOBAL_DATASET_COMPETITIONS = (
    "England/Premier League",
    "England/Championship",
    "Spain/La Liga",
    "Spain/La Liga 2",
    "Germany/Bundesliga",
    "Germany/Bundesliga 2",
    "Italy/Serie A",
    "Italy/Serie B",
    "France/Ligue 1",
    "France/Ligue 2",
    "Portugal/Liga Portugal",
    "UEFA/Champions League",
    "UEFA/Europa League",
    "UEFA/Conference League",
    "England/FA Cup",
    "England/League Cup",
    "CONCACAF/Leagues Cup",
    "Germany/DFB-Pokal",
    "Italy/Coppa Italia",
    "Spain/Copa del Rey",
    "France/Coupe de France",
)
EXTRA_DATASET_COMPETITIONS = (
    "Argentina/Primera Division",
    "Brazil/Brasileirão",
    "Japan/J1 League",
    "Netherlands/Eredivisie",
    "Belgium/First Division A",
    "Scotland/Premiership",
    "Turkey/Super Lig",
    "Austria/Bundesliga",
    "Greece/Super League",
    "Norway/Eliteserien",
    "Romania/Liga I",
    "Poland/Ekstraklasa",
    "Sweden/Allsvenskan",
)
# MLS conference views duplicate the main MLS table on the home sidebar.
HOME_SIDEBAR_SKIP_COMPETITIONS = frozenset({
    "United States/MLS - Supporters Shield Table",
    "United States/MLS - Eastern Conference",
    "United States/MLS - Western Conference",
})
MLS_WINNER_VIEWS = {
    "supporters_shield": "United States/MLS - Supporters Shield Table",
    "eastern_conference": "United States/MLS - Eastern Conference",
    "western_conference": "United States/MLS - Western Conference",
    "mls_cup": MLS_CUP_COMPETITION,
}

CUP_COMPETITIONS = {
    "England/FA Cup",
    "England/League Cup",
    "UEFA/Champions League",
    "UEFA/Europa League",
    "UEFA/Conference League",
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
    "Italy/Coppa Italia",
    "Spain/Copa del Rey",
    "Germany/DFB-Pokal",
    "France/Coupe de France",
    "United States/US Open Cup",
    "CONCACAF/Leagues Cup",
}

_CUP_FORMATS = {
    "England/FA Cup": {
        "format": "knockout",
        "description": "Single-elimination knockout. Early rounds have replays if drawn. Semi-finals and Final at neutral venues.",
        "stages": ["First Round", "Second Round", "Third Round", "Fourth Round", "Fifth Round", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": [],
        "final_neutral": True,
    },
    "England/League Cup": {
        "format": "knockout",
        "description": "Single-elimination knockout. Semi-finals are two-legged. Final at neutral venue.",
        "stages": ["First Round", "Second Round", "Third Round", "Fourth Round", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Semi-finals"],
        "final_neutral": True,
    },
    "UEFA/Champions League": {
        "format": "league_phase_then_knockout",
        "description": "League phase (8 matches per team) followed by two-legged knockout play-offs, Round of 16, Quarter-finals, Semi-finals, and a single-match Final at a neutral venue.",
        "league_phase_matches": 8,
        "stages": ["League Phase", "Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "knockout_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals"],
        "final_neutral": True,
    },
    "UEFA/Europa League": {
        "format": "league_phase_then_knockout",
        "description": "League phase (8 matches per team) followed by two-legged knockout play-offs, Round of 16, Quarter-finals, Semi-finals, and a single-match Final at a neutral venue.",
        "league_phase_matches": 8,
        "stages": ["League Phase", "Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "knockout_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals"],
        "final_neutral": True,
    },
    "UEFA/Conference League": {
        "format": "league_phase_then_knockout",
        "description": "League phase (6 matches per team) followed by two-legged knockout play-offs, Round of 16, Quarter-finals, Semi-finals, and a single-match Final at a neutral venue.",
        "league_phase_matches": 6,
        "stages": ["League Phase", "Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "knockout_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals"],
        "final_neutral": True,
    },
    "Europe/Champions League": {
        "format": "league_phase_then_knockout",
        "description": "League phase (8 matches per team) followed by two-legged knockout play-offs, Round of 16, Quarter-finals, Semi-finals, and a single-match Final at a neutral venue.",
        "league_phase_matches": 8,
        "stages": ["League Phase", "Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "knockout_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals"],
        "final_neutral": True,
    },
    "Europe/Europa League": {
        "format": "league_phase_then_knockout",
        "description": "League phase (8 matches per team) followed by two-legged knockout play-offs, Round of 16, Quarter-finals, Semi-finals, and a single-match Final at a neutral venue.",
        "league_phase_matches": 8,
        "stages": ["League Phase", "Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "knockout_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals"],
        "final_neutral": True,
    },
    "Europe/Conference League": {
        "format": "league_phase_then_knockout",
        "description": "League phase (6 matches per team) followed by two-legged knockout play-offs, Round of 16, Quarter-finals, Semi-finals, and a single-match Final at a neutral venue.",
        "league_phase_matches": 6,
        "stages": ["League Phase", "Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "knockout_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Knockout Round Play-offs", "Round of 16", "Quarter-finals", "Semi-finals"],
        "final_neutral": True,
    },
    "Italy/Coppa Italia": {
        "format": "knockout",
        "description": "Single-elimination knockout. Semi-finals are two-legged. Final at neutral venue.",
        "stages": ["First Round", "Second Round", "Third Round", "Fourth Round", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Semi-finals"],
        "final_neutral": True,
    },
    "Spain/Copa del Rey": {
        "format": "knockout",
        "description": "Single-elimination knockout. Semi-finals are two-legged. Final at neutral venue.",
        "stages": ["Preliminary Round", "First Round", "Second Round", "Third Round", "Round of 32", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Semi-finals"],
        "final_neutral": True,
    },
    "Germany/DFB-Pokal": {
        "format": "knockout",
        "description": "Single-elimination knockout. All rounds are single match. Final at neutral venue.",
        "stages": ["First Round", "Second Round", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": [],
        "final_neutral": True,
    },
    "France/Coupe de France": {
        "format": "knockout",
        "description": "Single-elimination knockout. All rounds are single match. Final at neutral venue.",
        "stages": ["First Round", "Second Round", "Third Round", "Fourth Round", "Fifth Round", "Sixth Round", "Seventh Round", "Eighth Round", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": [],
        "final_neutral": True,
    },
    "United States/US Open Cup": {
        "format": "knockout",
        "description": "Single-elimination knockout. All rounds are single match.",
        "stages": ["First Round", "Second Round", "Third Round", "Fourth Round", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": [],
        "final_neutral": False,
    },
    "CONCACAF/Leagues Cup": {
        "format": "group_stage_then_knockout",
        "description": "Group stage (3 matches per team across MLS and Liga MX clubs) followed by single-elimination knockout rounds.",
        "group_count": 4,
        "group_stage_matches_per_team": 3,
        "league_phase_matches": 3,
        "stages": ["Group Stage", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "knockout_rounds": ["Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": [],
        "final_neutral": True,
    },
    "FIFA/World Cup": {
        "format": "group_stage_then_knockout",
        "description": "12 groups of 4 teams (3 group matches each). Top two plus eight best third-place teams advance to a fixed Round of 32 knockout bracket.",
        "group_count": 12,
        "group_stage_matches_per_team": 3,
        "group_labels": list("ABCDEFGHIJKL"),
        "stages": [
            "Group Stage", "Round of 32", "Round of 16", "Quarter-finals",
            "Semi-finals", "Third Place", "Final",
        ],
        "knockout_rounds": [
            "Round of 32", "Round of 16", "Quarter-finals",
            "Semi-finals", "Third Place", "Final",
        ],
        "two_leg_rounds": [],
        "final_neutral": True,
    },
}

# Mobile app / website tournament keys → canonical competition names.
TOURNAMENT_KEY_MAP = {
    "world-cup": "FIFA/World Cup",
    "champions-league": "UEFA/Champions League",
    "europa-league": "UEFA/Europa League",
    "conference-league": "UEFA/Conference League",
    "euros": "UEFA/European Championship",
    "copa-america": "CONMEBOL/Copa America",
    "fa-cup": "England/FA Cup",
    "efl-cup": "England/League Cup",
    "dfb-pokal": "Germany/DFB-Pokal",
    "coupe-de-france": "France/Coupe de France",
    "coppa-italia": "Italy/Coppa Italia",
    "us-open-cup": "United States/US Open Cup",
    "leagues-cup": "CONCACAF/Leagues Cup",
}

# Leagues using head-to-head as the first tiebreaker (canonical: competition_rules.H2H_TIEBREAKER_COMPETITIONS).
H2H_LEAGUES = {
    "Spain/La Liga", "Spain/La Liga 2",
    "Italy/Serie A", "Italy/Serie B",
    "Portugal/Liga Portugal",
    "Belgium/First Division A",
    "Turkey/Super Lig",
    LIGA_MX_COMPETITION,
}

MLS_TABLE_VIEW_ALIASES = {
    "United States/MLS - Eastern Conference",
    "United States/MLS - Western Conference",
    "United States/MLS - Supporters Shield Table",
    MLS_CUP_COMPETITION,
}

# ── API Authentication ────────────────────────────────────────────

MUTATION_API_TOKEN = os.environ.get("MUTATION_API_TOKEN", "").strip()

# ── Apple Push Notification service (APNs) ────────────────────────

# APNs key from Apple Developer → Certificates → Keys
APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "").strip()
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "").strip()
APNS_AUTH_KEY_PATH = os.environ.get("APNS_AUTH_KEY_PATH", "").strip()
APNS_TOPIC = os.environ.get("APNS_TOPIC", "com.beyondthestats.app").strip()
APNS_LIVE_ACTIVITY_TOPIC = os.environ.get("APNS_LIVE_ACTIVITY_TOPIC", f"{APNS_TOPIC}.push-type.live-activity").strip()
APNS_USE_SANDBOX = os.environ.get("APNS_USE_SANDBOX", "0").strip().lower() in {"1", "true", "yes"}
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

# ── Redeem Codes ──────────────────────────────────────────────────
# Absolute path: <Beyond-the-Stats>/Data/redeem_codes.json
# (i.e. next to standings_cache.json / live_score_history.json).
# See redeem_codes.example.json for the expected shape.

REDEEM_CODES_FILE = os.path.join(PROJECT_DIR, "Data", "redeem_codes.json")


def get_live_score_tier(competition: str) -> str:
    """Return the live-score polling tier for a competition.

    ``"full"``
        Full in-play polling via ESPN.
    ``"reduced"``
        HT / FT only (second divisions, UEFA pre-main-stage).
    ``"result_only"``
        Post-match result fetch only (no in-play polling).
    ``"none"``
        Not covered by live-score polling at all.
    """
    if competition in REDUCED_POLLING_COMPETITIONS:
        return "reduced"
    if competition in RESULT_ONLY_COMPETITIONS:
        return "result_only"
    if competition in UEFA_LIVE_SCORE_COMPETITIONS:
        from datetime import date
        try:
            main_start = date.fromisoformat(UEFA_MAIN_STAGE_LIVE_FROM)
            if date.today() < main_start:
                return "reduced"
        except Exception:
            pass
        return "full"
    if competition in LIVE_SCORE_COMPETITIONS:
        return "full"
    return "none"
