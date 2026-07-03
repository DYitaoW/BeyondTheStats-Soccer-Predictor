"""
UEFA Data Manager — loads, caches, and integrates European-competition data.

Provides:
- UEFA country coefficients → league strength mapping
- Team registry (team name → country / domestic league)
- Domestic league table fetching from ESPN for non-tracked leagues
- Squad market value lookups (Transfermarkt cache)
- Historical UCL/UEL/UECL head-to-head and form data
- One-call ``build_uefa_context()`` that injects all data into the
  prediction context so ``inject_fallback_team()`` can use real stats.
"""
import json
import os
import urllib.request
from datetime import UTC, datetime
from typing import Any

# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEAM_DATA_DIR = os.path.join(BASE_DIR, "Data", "Team_Data")

COEFFICIENTS_FILE = os.path.join(TEAM_DATA_DIR, "uefa_country_coefficients.json")
TEAM_REGISTRY_FILE = os.path.join(TEAM_DATA_DIR, "uefa_team_registry.json")
DOMESTIC_TABLES_FILE = os.path.join(TEAM_DATA_DIR, "uefa_domestic_tables.json")
SQUAD_VALUES_FILE = os.path.join(TEAM_DATA_DIR, "uefa_squad_values.json")
EUROPEAN_H2H_FILE = os.path.join(TEAM_DATA_DIR, "uefa_european_h2h.json")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_HEADERS = {"User-Agent": "Mozilla/5.0"}
FETCH_TIMEOUT = 15

# ── Static data loaders ────────────────────────────────────────────


def _load_json(path: str, fallback: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback if fallback is not None else {}


def _save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── 1. Country coefficients ───────────────────────────────────────

# Code-backed fallback strengths for every UEFA member association (plus
# Russia, retained for historical/team-registry compatibility). These mirror
# the same 0.50-1.00 scale used by Data/Team_Data/league_strength.json: top
# domestic leagues sit near 1.00, mid-tier UEFA leagues in the 0.60-0.75 band,
# and microstate/developing leagues near 0.50. The generated
# uefa_country_coefficients.json file, when present, can still override any
# value below, but missing countries will never drop to an undifferentiated
# fallback just because the local generated cache is incomplete.
DEFAULT_UEFA_COUNTRY_STRENGTH = {
    "England": 1.00,
    "Spain": 0.97,
    "Germany": 0.96,
    "Italy": 0.95,
    "France": 0.92,
    "Netherlands": 0.86,
    "Portugal": 0.88,
    "Belgium": 0.80,
    "Turkey": 0.84,
    "Czech Republic": 0.67,
    "Greece": 0.65,
    "Norway": 0.62,
    "Austria": 0.68,
    "Scotland": 0.74,
    "Denmark": 0.66,
    "Switzerland": 0.69,
    "Sweden": 0.63,
    "Poland": 0.58,
    "Croatia": 0.61,
    "Serbia": 0.60,
    "Cyprus": 0.57,
    "Israel": 0.59,
    "Ukraine": 0.64,
    "Romania": 0.55,
    "Hungary": 0.56,
    "Slovakia": 0.54,
    "Slovenia": 0.53,
    "Moldova": 0.50,
    "Azerbaijan": 0.52,
    "Bulgaria": 0.54,
    "Finland": 0.52,
    "Ireland": 0.52,
    "Bosnia and Herzegovina": 0.51,
    "Kosovo": 0.50,
    "Kazakhstan": 0.52,
    "Armenia": 0.50,
    "Faroe Islands": 0.50,
    "Iceland": 0.51,
    "Latvia": 0.50,
    "Albania": 0.50,
    "Belarus": 0.51,
    "Malta": 0.50,
    "Georgia": 0.51,
    "Northern Ireland": 0.50,
    "Estonia": 0.50,
    "Lithuania": 0.50,
    "Wales": 0.50,
    "North Macedonia": 0.50,
    "Luxembourg": 0.50,
    "Montenegro": 0.50,
    "Gibraltar": 0.50,
    "Liechtenstein": 0.50,
    "Andorra": 0.50,
    "San Marino": 0.50,
    "Russia": 0.65,
}

UEFA_COUNTRY_ALIASES = {
    "Bosnia": "Bosnia and Herzegovina",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Czechia": "Czech Republic",
    "Czech Rep.": "Czech Republic",
    "Macedonia": "North Macedonia",
    "Republic of Ireland": "Ireland",
}


def load_country_coefficients() -> dict:
    """Return {country_name: strength_float} for all UEFA associations.

    Generated cache values in Data/Team_Data/uefa_country_coefficients.json
    override these code defaults when present, but incomplete/missing cache
    files still leave every UEFA country with a usable fallback coefficient.
    """
    coeffs = dict(DEFAULT_UEFA_COUNTRY_STRENGTH)
    raw = _load_json(COEFFICIENTS_FILE, {})
    for name, info in raw.get("coefficients", {}).items():
        try:
            coeffs[name] = float(info["strength"])
        except (KeyError, TypeError, ValueError):
            continue
    for alias, canonical in UEFA_COUNTRY_ALIASES.items():
        if canonical in coeffs:
            coeffs.setdefault(alias, coeffs[canonical])
    return coeffs


def get_country_strength(country: str, coeffs: dict | None = None) -> float:
    """Return mapped league strength for *country*, or 0.50 if unknown."""
    if coeffs is None:
        coeffs = load_country_coefficients()
    canonical = UEFA_COUNTRY_ALIASES.get(country, country)
    return coeffs.get(country, coeffs.get(canonical, 0.50))


# ── 2. Team registry ──────────────────────────────────────────────


def load_team_registry() -> dict:
    """Return the team-registry dict."""
    return _load_json(TEAM_REGISTRY_FILE, {})


def lookup_team(team_name: str, registry: dict | None = None) -> dict | None:
    """Look up a team by its primary name or any alias.

    Returns {"country": str, "league": str} or None.
    """
    if registry is None:
        registry = load_team_registry()
    teams = registry.get("teams", {})
    # Direct primary-name match
    entry = teams.get(team_name)
    if entry:
        return {"country": entry["country"], "league": entry["league"]}
    # Alias match
    for primary, info in teams.items():
        if team_name in info.get("aliases", []):
            return {"country": info["country"], "league": info["league"]}
    return None


# ── 3. Domestic table fetching (ESPN) ─────────────────────────────


def _fetch_espn_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers=ESPN_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def fetch_domestic_table(league_name: str, espn_id: str) -> list[dict] | None:
    """Fetch current standings from ESPN for a domestic league.

    Returns [{"name": str, "position": int, "points": float, ...}, ...] or None.
    """
    url = f"{ESPN_BASE}/{espn_id}/standings"
    data = _fetch_espn_json(url)
    if not data:
        return None
    standings = data.get("standings") or []
    if not standings:
        return None
    primary = standings[0]
    entries = primary.get("entries") or []
    if not entries:
        children = primary.get("children") or []
        if children:
            entries = children[0].get("entries") or []
    parsed = []
    for e in entries:
        team = ((e.get("team") or {}).get("displayName") or "").strip()
        stats = {s["name"]: s["value"] for s in (e.get("stats") or [])}
        parsed.append({
            "name": team,
            "position": int(stats.get("rank", 999)),
            "points": float(stats.get("points", 0)),
            "played": int(stats.get("gamesPlayed", 0)),
            "wins": int(stats.get("wins", 0)),
            "draws": int(stats.get("ties", 0)),
            "losses": int(stats.get("losses", 0)),
            "goals_for": int(stats.get("goalsFor", 0)),
            "goals_against": int(stats.get("goalsAgainst", 0)),
        })
    return parsed if parsed else None


def _load_or_fetch_domestic_tables(league_espn_map: dict) -> dict:
    """Return cached domestic tables, fetching missing ones from ESPN.

    Returns {league_name: [team_dict, ...]}.
    """
    cached = _load_json(DOMESTIC_TABLES_FILE, {})
    changed = False
    for league, espn_id in league_espn_map.items():
        if league in cached and cached[league]:
            continue
        table = fetch_domestic_table(league, espn_id)
        if table:
            cached[league] = {
                "fetched_at": datetime.now(UTC).isoformat(),
                "teams": table,
            }
            changed = True
    if changed:
        _save_json(DOMESTIC_TABLES_FILE, cached)
    return cached


def get_team_domestic_stats(
    team_name: str, league: str, tables: dict | None = None,
    registry: dict | None = None,
) -> dict | None:
    """Return {points, position, ppg, ...} for a team from cached domestic tables."""
    if tables is None:
        tables = _load_json(DOMESTIC_TABLES_FILE, {})
    if registry is None:
        registry = load_team_registry()
    league_data = tables.get(league)
    if not league_data:
        return None
    for team_row in league_data.get("teams", []):
        # Match by name or alias
        if team_row["name"].lower() == team_name.lower():
            return team_row
        # Check aliases
        entry = lookup_team(team_name, registry)
        if entry and entry["league"] == league:
            pass  # Already matched above
    # Fallback: try fuzzy match
    tn_lower = team_name.lower()
    for team_row in league_data.get("teams", []):
        if tn_lower in team_row["name"].lower() or team_row["name"].lower() in tn_lower:
            return team_row
    return None


# ── 4. Squad values (Transfermarkt) ───────────────────────────────


def load_uefa_squad_values() -> dict:
    """Return {team_name: squad_value_eur_m} from the cached file."""
    raw = _load_json(SQUAD_VALUES_FILE, {})
    return raw.get("teams", {})


def get_team_squad_value(team_name: str, registry: dict | None = None,
                         values: dict | None = None) -> float | None:
    """Return squad market value in millions for a team."""
    if values is None:
        values = load_uefa_squad_values()
    direct = values.get(team_name)
    if direct is not None:
        return direct
    # Try alias lookup
    if registry is None:
        registry = load_team_registry()
    entry = lookup_team(team_name, registry)
    if entry:
        for alias in [team_name] + registry.get("teams", {}).get(team_name, {}).get("aliases", []):
            if alias in values:
                return values[alias]
    return None


# ── 4b. Squad-value-informed stat scaling ─────────────────────────

# Roughly the squad market value (in EUR millions) of a club in a
# league with strength ~0.85 (i.e. the fallback default). Squads worth
# noticeably more/less than this nudge the synthetic attack/defense
# estimate used for unknown-team matchups, on top of the league
# coefficient and domestic-table signal.
_SQUAD_VALUE_BASELINE_EUR_M = 90.0
_SQUAD_VALUE_SCALE_MIN = 0.85
_SQUAD_VALUE_SCALE_MAX = 1.25
_SQUAD_VALUE_SCALE_EXPONENT = 0.12


def squad_value_scale_factor(squad_value_eur_m: float | None) -> float:
    """Return a multiplicative scale factor derived from squad market value.

    Used by ``inject_fallback_team()`` implementations so that, for a team
    not found in the training database, both the *league coefficient* and
    the *team's transfer-market value* influence the synthetic stats used
    to estimate a European-cup matchup — not just the league coefficient
    alone. Returns 1.0 (no adjustment) when no value is available.
    """
    if squad_value_eur_m is None or squad_value_eur_m <= 0:
        return 1.0
    ratio = squad_value_eur_m / _SQUAD_VALUE_BASELINE_EUR_M
    factor = ratio ** _SQUAD_VALUE_SCALE_EXPONENT
    return max(_SQUAD_VALUE_SCALE_MIN, min(_SQUAD_VALUE_SCALE_MAX, factor))


# ── 5. Historical European H2H ────────────────────────────────────


def load_european_h2h() -> dict:
    """Return H2H dict from cached UEFA results.

    Format matches Predict_Match's head_to_head format:
    {team_A: {team_B: {games, wins, draws, losses, goals_scored, ...}}}
    """
    return _load_json(EUROPEAN_H2H_FILE, {})


def get_european_h2h_for_team(team_name: str, opponent: str,
                               h2h: dict | None = None) -> dict | None:
    """Return H2H stats for a specific European matchup, or None."""
    if h2h is None:
        h2h = load_european_h2h()
    return h2h.get(team_name, {}).get(opponent)


# ── 6. Data build helpers (called by pipeline scripts) ────────────


def get_league_espn_id(league: str, registry: dict | None = None) -> str | None:
    """Return ESPN ID for a league from the registry mapping."""
    if registry is None:
        registry = load_team_registry()
    return registry.get("league_to_espn_id", {}).get(league)


def ensure_domestic_tables_for_teams(cup_teams: list[str]) -> dict:
    """Fetch domestic tables for any non-tracked league that has teams in *cup_teams*.

    Returns the updated {league: table_data} dict.
    """
    registry = load_team_registry()
    coeffs = load_country_coefficients()
    needed_leagues = set()
    for team in cup_teams:
        entry = lookup_team(team, registry)
        if entry:
            league = entry["league"]
            # Only non-top-5 non-tracked leagues need fetching
            if league not in _TRACKED_LEAGUES:
                needed_leagues.add(league)
    espn_map = {}
    for league in needed_leagues:
        espn_id = get_league_espn_id(league, registry)
        if espn_id:
            espn_map[league] = espn_id
    return _load_or_fetch_domestic_tables(espn_map)


# Known tracked leagues that already have full team data.
_TRACKED_LEAGUES = frozenset({
    "England/Premier League", "England/Championship",
    "Spain/La Liga", "Spain/La Liga 2",
    "Italy/Serie A", "Italy/Serie B",
    "Germany/Bundesliga", "Germany/Bundesliga 2",
    "France/Ligue 1", "France/Ligue 2",
    "Portugal/Liga Portugal", "Netherlands/Eredivisie",
    "United States/MLS",
    "Belgium/First Division A", "Scotland/Premiership", "Turkey/Super Lig",
    # Moved from Extra-leagues into the regular pipeline (see
    # files/Download_Latest_Data.py): real domestic data is now downloaded
    # for these, so ESPN domestic-table fetching is no longer needed for them.
    "Greece/Super League", "Norway/Eliteserien", "Sweden/Allsvenskan",
})


def build_uefa_context(context: dict) -> dict:
    """Inject all available UEFA data into the prediction *context* dict.

    Modifies the context in-place and also returns it for convenience.
    Specifically:
    - Adds country-coefficient-based league_strength entries for non-tracked leagues
    - Injects squad market values for teams that have them
    - Makes domestic table data accessible
    """
    coeffs = load_country_coefficients()
    registry = load_team_registry()
    squad_values = load_uefa_squad_values()
    domestic_tables = _load_json(DOMESTIC_TABLES_FILE, {})
    european_h2h = load_european_h2h()

    # Store UEFA data on the context for later use by inject_fallback_team
    context.setdefault("uefa_coefficients", coeffs)
    context.setdefault("uefa_team_registry", registry)
    context.setdefault("uefa_squad_values", squad_values)
    context.setdefault("uefa_domestic_tables", domestic_tables)
    context.setdefault("uefa_european_h2h", european_h2h)

    # Pre-populate league_strength for non-tracked leagues referenced in the registry
    ls = context.setdefault("league_strength", {})
    teams_in_use = set()
    team_comp_map = context.get("team_competition_map", {})
    for team, comp in team_comp_map.items():
        teams_in_use.add(team)
    for team in teams_in_use:
        entry = lookup_team(team, registry)
        if entry:
            league = entry["league"]
            if league not in _TRACKED_LEAGUES and league not in ls:
                strength = get_country_strength(entry["country"], coeffs)
                ls[league] = strength

    # Merge European H2H into main head_to_head
    h2h = context.setdefault("head_to_head", {})
    for team_a, opponents in european_h2h.items():
        h2h.setdefault(team_a, {}).update(opponents)

    return context


def lookup_team_data_for_fallback(
    team_name: str,
    uefa_coefficients: dict | None = None,
    uefa_team_registry: dict | None = None,
    uefa_squad_values: dict | None = None,
    uefa_domestic_tables: dict | None = None,
) -> dict:
    """Return a rich data bundle for a provisional team.

    Returns::
        {"country": str, "league": str, "league_strength": float,
         "squad_value_eur_m": float|None, "domestic": {position, points, ...}|None,
         "domestic_ppg": float}
    """
    if uefa_coefficients is None:
        uefa_coefficients = load_country_coefficients()
    if uefa_team_registry is None:
        uefa_team_registry = load_team_registry()
    if uefa_squad_values is None:
        uefa_squad_values = load_uefa_squad_values()
    if uefa_domestic_tables is None:
        uefa_domestic_tables = _load_json(DOMESTIC_TABLES_FILE, {})

    entry = lookup_team(team_name, uefa_team_registry)
    if not entry:
        return {"country": None, "league": None, "league_strength": 0.50,
                "squad_value_eur_m": None, "domestic": None, "domestic_ppg": 1.2}

    country = entry["country"]
    league = entry["league"]
    strength = get_country_strength(country, uefa_coefficients)
    squad_value = get_team_squad_value(team_name, uefa_team_registry, uefa_squad_values)
    domestic = get_team_domestic_stats(team_name, league, uefa_domestic_tables, uefa_team_registry)
    domestic_ppg = (domestic["points"] / domestic["played"]) if domestic and domestic.get("played", 0) > 0 else 1.2

    return {
        "country": country,
        "league": league,
        "league_strength": strength,
        "squad_value_eur_m": squad_value,
        "domestic": domestic,
        "domestic_ppg": domestic_ppg,
    }
