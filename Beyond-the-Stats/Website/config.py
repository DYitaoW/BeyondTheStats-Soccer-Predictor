"""Configuration constants for the Beyond the Stats Flask application.

File paths, ESPN competition IDs, cache TTLs, and environment variables.
"""
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ── Directory Structure (via shared.paths) ─────────────────────────

WEBSITE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(WEBSITE_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
_SHARED_DIR = os.path.join(PROJECT_DIR, "shared")
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)
from shared import paths as _paths  # noqa: E402

_paths.ensure_output_dirs()

LAST_REFRESH_FILE = str(_paths.LAST_REFRESH_FILE)
FILES_DIR = str(_paths.EUROPE_FILES_DIR)
MLS_FILES_DIR = str(_paths.MLS_FILES_DIR)
EXTRA_FILES_DIR = str(_paths.EXTRA_FILES_DIR)
WEBSITE_FILES_DIR = os.path.join(WEBSITE_DIR, "files")
GRAPHICS_DIR = os.path.join(WEBSITE_DIR, "graphics")
ACCURACY_TOTALS_FILE = os.path.join(WEBSITE_FILES_DIR, "accuracy_totals.json")
ACCURACY_HISTORY_DIR = os.path.join(WEBSITE_FILES_DIR, "accuracy_history")

# Working / processed data roots (per region)
EUROPE_DATA_DIR = str(_paths.EUROPE_DATA_DIR)  # project-level Data/ (same as _paths.DATA_DIR)
MLS_DATA_DIR = str(_paths.MLS_DATA_DIR)
EXTRA_DATA_DIR = str(_paths.EXTRA_DATA_DIR)
EUROPE_PROCESSED_DIR = os.path.join(EUROPE_DATA_DIR, "Processed_Data")
MLS_PROCESSED_DIR = os.path.join(MLS_DATA_DIR, "Processed_Data")
EXTRA_PROCESSED_DIR = os.path.join(EXTRA_DATA_DIR, "Processed_Data")

# ── Prediction Files (all under Output/) ───────────────────────────

GLOBAL_UPCOMING_FILE = str(_paths.GLOBAL_UPCOMING_FILE)
GLOBAL_PROJECTED_MATCHES_FILE = str(_paths.GLOBAL_PROJECTED_MATCHES_FILE)
ALL_UPCOMING_FILE = str(_paths.ALL_UPCOMING_FILE)
FOUR_WEEK_WINDOW_FILE = str(_paths.FOUR_WEEK_WINDOW_FILE)
CUP_UPCOMING_FILE = str(_paths.CUP_UPCOMING_FILE)
CUP_COMPLETED_FILE = str(_paths.CUP_COMPLETED_FILE)
MLS_UPCOMING_FILE = str(_paths.MLS_UPCOMING_FILE)
EXTRA_UPCOMING_FILE = str(_paths.EXTRA_UPCOMING_FILE)
EXTRA_PROJECTED_MATCHES_FILE = str(_paths.EXTRA_PROJECTED_MATCHES_FILE)
NATIONAL_UPCOMING_FILE = str(_paths.NATIONAL_UPCOMING_FILE)
FRIENDLIES_UPCOMING_FILE = str(_paths.FRIENDLIES_UPCOMING_FILE)
GLOBAL_PROJECTED_TABLE_FILE = str(_paths.GLOBAL_PROJECTED_TABLE_FILE)
CUP_PROJECTED_TABLE_FILE = str(_paths.CUP_PROJECTED_TABLE_FILE)
CUP_REAL_TABLE_FILE = str(_paths.CUP_REAL_TABLE_FILE)
CUP_PROJECTED_BRACKET_FILE = str(_paths.CUP_PROJECTED_BRACKET_FILE)
PAST_GAMES_FILE = str(_paths.PAST_GAMES_FILE)
PAST_GAMES_JOURNAL_FILE = str(_paths.PAST_GAMES_JOURNAL_FILE)
PAST_GAMES_BACKUP_FILE = str(_paths.PAST_GAMES_BACKUP_FILE)
SQLITE_STORE_FILE = str(_paths.SQLITE_STORE_FILE)
PAST_GAMES_DB_FILE = str(_paths.PAST_GAMES_DB_FILE)
LIVE_SCORE_HISTORY_DB_FILE = str(_paths.LIVE_SCORE_HISTORY_DB_FILE)
MLS_PROJECTED_TABLE_FILE = str(_paths.MLS_PROJECTED_TABLE_FILE)
EXTRA_PROJECTED_TABLE_FILE = str(_paths.EXTRA_PROJECTED_TABLE_FILE)
MLS_PROJECTED_BRACKET_FILE = str(_paths.MLS_PROJECTED_BRACKET_FILE)

# ── Pipeline & Data Files ─────────────────────────────────────────

LIVE_RESULTS_UPDATER = str(_paths.LIVE_RESULTS_UPDATER)
RUN_ALL_PIPELINE = str(_paths.RUN_ALL_PIPELINE)

# Set to "0" / "false" to disable pipeline execution entirely.
# Useful during development/debugging so /api/refresh and /api/retrain
# return a clear "disabled" response instead of trying to run the pipeline.
PIPELINE_ENABLED = os.environ.get("PIPELINE_ENABLED", "1").strip().lower() in {"1", "true", "yes"}

# If True, _team_name_for_display / _team_name_for_db in team_utils
# will map canonical names through the display-mapping file.
USE_DISPLAY_NAME_MAPPING = False

LAST_DATA_REFRESH_FILE = str(_paths.LAST_DATA_REFRESH_FILE)
PIPELINE_STATUS_FILE = str(_paths.PIPELINE_STATUS_FILE)
BACKEND_RUN_STATUS_FILE = str(_paths.BACKEND_RUN_STATUS_FILE)
PIPELINE_LOG_FILE = str(_paths.PIPELINE_LOG_FILE)
TEAM_NAME_DISPLAY_MAPPING_FILE = str(_paths.TEAM_NAME_MAPPING_MASTER)
TOP_SCORERS_FILE = os.path.join(EUROPE_DATA_DIR, "Team_Data", "current_season_top_scorers.json")
LIVE_SCORE_HISTORY_FILE = str(_paths.LIVE_SCORE_HISTORY_FILE)
PREDICTION_TRACKING_FILE = str(_paths.PREDICTION_TRACKING_FILE)
REAL_TABLES_PERSIST_FILE = str(_paths.STANDINGS_CACHE_FILE)
LEAGUE_TEAMS_FILE = str(_paths.LEAGUE_TEAMS_FILE)
CURRENT_SEASON_TEAMS_FILE = str(_paths.CURRENT_SEASON_TEAMS_FILE)
WORLD_CUP_PROJECTION_FILE = str(_paths.WORLD_CUP_PROJECTION_FILE)

# ── League-Data / Cup-Data Cache (pre-computed under Output/) ──────

LEAGUE_DATA_DIR = str(_paths.OUTPUT_LEAGUE_DATA_DIR)
CUP_DATA_DIR = str(_paths.OUTPUT_CUP_DATA_DIR)

# ── Info Pages (changes / roadmap / upcoming) ───────────────────────

INFO_DIR = str(_paths.DATA_INFO_DIR)
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
CACHE_TTL_LIVE = int(os.environ.get("CACHE_TTL_LIVE", "30"))  # upcoming + live annotations
REAL_TABLES_CACHE_TTL = 300  # 5 minutes
REAL_LEADERS_CACHE_TTL = 300  # 5 minutes
_API_CACHE_MAX_AGE = int(os.environ.get("API_CACHE_MAX_AGE", "300"))
_STATIC_CACHE_MAX_AGE = int(os.environ.get("STATIC_CACHE_MAX_AGE", "86400"))
STATIC_PREDICTIONS_CACHE = os.environ.get("STATIC_PREDICTIONS_CACHE", "0").strip().lower() in {"1", "true", "yes"}

# ── API Rate Limits (per client IP / minute) ───────────────────────
# Applied to all ``/api/*`` routes. ``/api/redeem`` also has a tighter cap.
API_RATE_LIMIT_PER_MINUTE = int(os.environ.get("API_RATE_LIMIT_PER_MINUTE", "100"))
REDEEM_RATE_LIMIT_PER_MINUTE = int(os.environ.get("REDEEM_RATE_LIMIT_PER_MINUTE", "5"))

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
    "International/Friendly",
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
    # football-data code DK1 historically returned German Bundesliga rows;
    # keep ESPN den.1 live-score wiring but hide from league-facing APIs until
    # Extra/domestic history is ESPN-only and decontaminated.
    "Denmark/Danish Superliga",
    # UEFA/cup fallback roster only — no upcoming API, no domestic history
    "Czech Republic/First League",
    "Serbia/SuperLiga",
    "Cyprus/First Division",
    "Slovakia/Super Liga",
    "Slovenia/PrvaLiga",
    "Bulgaria/First League",
}

# National-team competitions (World Cup qualifying, Nations League, continental
# federations) are never domestic league tables. They are handled by the
# national-team pipeline and must not leak into league-facing APIs even when a
# stale projected-tables CSV still contains rows for them.
NATIONAL_TEAM_COMPETITION_PREFIXES = frozenset({
    "FIFA", "UEFA", "CONMEBOL", "CONCACAF", "CAF", "AFC", "OFC",
})


def is_national_team_competition(competition) -> bool:
    """Return True for national-team/qualifier competitions (not club leagues)."""
    name = str(competition or "").strip()
    if not name:
        return False
    if "World Cup Qualifying" in name or "Nations League" in name:
        return True
    head = name.split("/", 1)[0].strip().upper()
    return head in NATIONAL_TEAM_COMPETITION_PREFIXES


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
    "North America/Leagues Cup": "concacaf.leagues.cup",
    # Domestic cups
    "England/FA Cup": "eng.fa",
    "England/League Cup": "eng.league_cup",
    "Europe/Champions League": "uefa.champions",
    "Europe/Europa League": "uefa.europa",
    "Europe/Conference League": "uefa.europa.conf",
    # Europe/ prefix aliases (used by Predict_Upcoming_Matchweek short codes)
    "Europe/Champions League": "uefa.champions",
    "Europe/Europa League": "uefa.europa",
    "Europe/Conference League": "uefa.europa.conf",
    # Domestic cups (predictions pipeline produces these)
    "Italy/Coppa Italia": "ita.coppa_italia",
    "Spain/Copa del Rey": "esp.copa_del_rey",
    "Germany/DFB-Pokal": "ger.dfb_pokal",
    "France/Coupe de France": "fra.coupe_de_france",
    "United States/US Open Cup": "usa.open",
    "North America/Leagues Cup": "concacaf.leagues.cup",
    CLUB_FRIENDLIES_COMPETITION: CLUB_FRIENDLIES_ESPN_ID,
    # National team & World Cup
    "International/World Cup": "fifa.world",
    "International/Friendly": "fifa.friendly",
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


# Always polled each cycle so live scores work even when upcoming CSV discovery is empty.
CORE_LIVE_POLL_COMPETITIONS = (
    "England/Premier League",
    "England/Championship",
    "Spain/La Liga",
    "Italy/Serie A",
    "Germany/Bundesliga",
    "France/Ligue 1",
    "Portugal/Liga Portugal",
    "Netherlands/Eredivisie",
    "United States/MLS",
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
    "England/FA Cup",
    "England/League Cup",
    "North America/Leagues Cup",
)


# UEFA club competitions: show qualifying fixtures in upcoming, but defer
# in-play live scoring until the main group/league phase (September).
UEFA_MAIN_STAGE_LIVE_FROM = "2026-09-01"
UEFA_LIVE_SCORE_COMPETITIONS = frozenset({
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
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
    # Map legacy / pipeline labels to canonical live-score keys.
    _ALIASES = {
        "FIFA/World Cup": "International/World Cup",
        "World Cup": "International/World Cup",
        "UEFA/Champions League": "Europe/Champions League",
        "UEFA/Europa League": "Europe/Europa League",
        "UEFA/Conference League": "Europe/Conference League",
        "Europe/Champions League": "Europe/Champions League",
        "Europe/Europa League": "Europe/Europa League",
        "Europe/Conference League": "Europe/Conference League",
    }
    canonical = _ALIASES.get(comp, comp)
    if canonical:
        aliases.add(canonical)
    for variant, target in _ALIASES.items():
        if target == comp or target == canonical:
            aliases.add(variant)
            aliases.add(target)
    return aliases


def resolve_live_competition(competition: str) -> str | None:
    """Return the LIVE_SCORE_COMPETITIONS key for a pipeline competition label."""
    for alias in competition_live_aliases(competition):
        if alias in LIVE_SCORE_COMPETITIONS:
            return alias
    return None

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


def app_dataset_competitions() -> tuple[str, ...]:
    """League/cup competitions exposed in app league tables and league-data APIs."""
    seen: set[str] = set()
    out: list[str] = []
    for comp in (
        *GLOBAL_DATASET_COMPETITIONS,
        *EXTRA_DATASET_COMPETITIONS,
        MLS_COMPETITION,
        LIGA_MX_COMPETITION,
    ):
        name = str(comp or "").strip()
        if not name or name in seen:
            continue
        if name in UPCOMING_ONLY_COMPETITIONS or name in LEAGUE_API_EXCLUDED_COMPETITIONS:
            continue
        seen.add(name)
        out.append(name)
    return tuple(sorted(out, key=str.lower))


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
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
    "England/FA Cup",
    "England/League Cup",
    "North America/Leagues Cup",
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
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
    "Italy/Coppa Italia",
    "Spain/Copa del Rey",
    "Germany/DFB-Pokal",
    "France/Coupe de France",
    "United States/US Open Cup",
    "North America/Leagues Cup",
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
    "North America/Leagues Cup": {
        # 2026 format: cross-league Phase One (MLS vs Liga MX only), then QF+.
        "format": "dual_league_phase_then_knockout",
        "description": (
            "Phase One: each club plays 3 MLS↔Liga MX matches. Separate MLS and "
            "Liga MX tables (teams do not play within their own league table). "
            "No draws — 3 pts regulation win, 2 pts shootout win, 1 pt shootout loss. "
            "Top 4 from each table advance to Quarter-finals → Semi-finals → "
            "Third Place & Final."
        ),
        "phase_one_matches_per_team": 3,
        "league_phase_matches": 3,
        "league_tables": ["MLS", "Liga MX"],
        "advance_per_table": 4,
        "points": {
            "regulation_win": 3,
            "shootout_win": 2,
            "shootout_loss": 1,
        },
        "no_draws": True,
        "stages": ["Phase One", "Quarter-finals", "Semi-finals", "Third Place", "Final"],
        "knockout_rounds": ["Quarter-finals", "Semi-finals", "Third Place", "Final"],
        "knockout_seedings": [
            "MLS 1 vs Liga MX 4",
            "MLS 2 vs Liga MX 3",
            "MLS 3 vs Liga MX 2",
            "MLS 4 vs Liga MX 1",
        ],
        "two_leg_rounds": [],
        "final_neutral": True,
    },
    # World Cup temporarily hidden from cups competitions APIs (keep definition
    # for dedicated /api/world-cup and competition-data paths).
    # "International/World Cup": {
    #     "format": "group_stage_then_knockout",
    #     "description": "12 groups of 4 teams (3 group matches each). Top two plus eight best third-place teams advance to a fixed Round of 32 knockout bracket.",
    #     "group_count": 12,
    #     "group_stage_matches_per_team": 3,
    #     "group_labels": list("ABCDEFGHIJKL"),
    #     "stages": [
    #         "Group Stage", "Round of 32", "Round of 16", "Quarter-finals",
    #         "Semi-finals", "Third Place", "Final",
    #     ],
    #     "knockout_rounds": [
    #         "Round of 32", "Round of 16", "Quarter-finals",
    #         "Semi-finals", "Third Place", "Final",
    #     ],
    #     "two_leg_rounds": [],
    #     "final_neutral": True,
    # },
}

# Mobile app / website tournament keys → canonical competition names.
TOURNAMENT_KEY_MAP = {
    "world-cup": "International/World Cup",
    "champions-league": "Europe/Champions League",
    "europa-league": "Europe/Europa League",
    "conference-league": "Europe/Conference League",
    "euros": "International/European Championship",
    "copa-america": "South America/Copa America",
    "fa-cup": "England/FA Cup",
    "efl-cup": "England/League Cup",
    "league-cup": "England/League Cup",
    "dfb-pokal": "Germany/DFB-Pokal",
    "coupe-de-france": "France/Coupe de France",
    "coppa-italia": "Italy/Coppa Italia",
    "copa-del-rey": "Spain/Copa del Rey",
    "us-open-cup": "United States/US Open Cup",
    "leagues-cup": "North America/Leagues Cup",
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
APNS_TOPIC = os.environ.get("APNS_TOPIC", "Yitao.BeyondTheStatsApp").strip()
APNS_LIVE_ACTIVITY_TOPIC = os.environ.get("APNS_LIVE_ACTIVITY_TOPIC", f"{APNS_TOPIC}.push-type.liveactivity").strip()
APNS_USE_SANDBOX = os.environ.get("APNS_USE_SANDBOX", "0").strip().lower() in {"1", "true", "yes"}
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

# ── Redeem Codes ──────────────────────────────────────────────────
# Same folder as team_name_mapping_master.json: <repo>/Data/redeem_codes.json
# Format: [{"code": "CODEHERE", "value": true}, ...]
# Codes are case-sensitive; letters and digits only (no special characters).
# See redeem_codes.example.json.

REDEEM_CODES_FILE = str(_paths.REDEEM_CODES_FILE)
REDEEM_CODES_EXAMPLE_FILE = str(_paths.REDEEM_CODES_EXAMPLE_FILE)


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
