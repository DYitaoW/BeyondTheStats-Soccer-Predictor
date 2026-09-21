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
import json
import os
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Paths ──────────────────────────────────────────────────────────
# BASE_DIR set by shared.paths bootstrap above
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

# League-name prefixes that are not UEFA associations / domestic countries.
_NON_DOMESTIC_LEAGUE_PREFIXES = frozenset({
    "Europe", "International", "Asia", "Africa", "North America", "South America",
    "World", "FIFA", "UEFA",
})

_CUP_LEAGUE_TOKENS = (
    "cup", "pokal", "copa", "coupe", "coppa", "shield", "trophy",
    "playoff", "play-off", "super cup", "supercup",
)

_SEED_TEAM_INDEX: dict | None = None
_STANDINGS_CACHE: dict | None = None


def _normalize_name(name: str) -> str:
    text = str(name or "").strip().lower()
    for ch in ("'", "’", ".", "-", "_"):
        text = text.replace(ch, " ")
    return " ".join(text.split())


def _country_from_league(league: str) -> str | None:
    league = str(league or "").strip()
    if not league or "/" not in league:
        return None
    prefix = league.split("/", 1)[0].strip()
    if not prefix or prefix in _NON_DOMESTIC_LEAGUE_PREFIXES:
        return None
    return UEFA_COUNTRY_ALIASES.get(prefix, prefix)


def _is_domestic_league_name(league: str) -> bool:
    """True for domestic league tables (not domestic cups / continental cups)."""
    league = str(league or "").strip()
    if not league or _country_from_league(league) is None:
        return False
    lower = league.lower()
    return not any(tok in lower for tok in _CUP_LEAGUE_TOKENS)


def _load_seed_team_index() -> dict:
    """Build team→{country,league} from league_teams / current_season seeds.

    Used when ``uefa_team_registry.json`` is missing or incomplete (common in
    fresh / cloud environments where Team_Data caches are not generated yet).
    Prefers domestic leagues over domestic cups when a team appears in both.
    """
    global _SEED_TEAM_INDEX
    if _SEED_TEAM_INDEX is not None:
        return _SEED_TEAM_INDEX
    index: dict[str, dict] = {}
    seed_paths = []
    try:
        from shared import paths as _paths
        seed_paths = [
            Path(_paths.LEAGUE_TEAMS_FILE),
            Path(_paths.CURRENT_SEASON_TEAMS_FILE),
        ]
    except Exception:
        root = Path(TEAM_DATA_DIR).resolve().parent
        seed_paths = [
            root / "Seeds" / "league_teams.json",
            root / "Seeds" / "current_season_teams.json",
        ]

    def _store(name: str, entry: dict, *, prefer: bool) -> None:
        for key in (name, _normalize_name(name)):
            if not key:
                continue
            existing = index.get(key)
            if existing is None or (prefer and not _is_domestic_league_name(existing.get("league", ""))):
                index[key] = entry

    for path in seed_paths:
        data = _load_json(str(path), {})
        if not isinstance(data, dict):
            continue
        # Pass 1: domestic leagues. Pass 2: everything else (cups) as fill-ins.
        for prefer_domestic in (True, False):
            for league, teams in data.items():
                country = _country_from_league(league)
                if not country:
                    continue
                is_domestic = _is_domestic_league_name(league)
                if prefer_domestic and not is_domestic:
                    continue
                if (not prefer_domestic) and is_domestic:
                    continue
                if not isinstance(teams, list):
                    continue
                entry = {"country": country, "league": str(league).strip()}
                for team in teams:
                    name = str(team or "").strip()
                    if name:
                        _store(name, entry, prefer=is_domestic)
    _SEED_TEAM_INDEX = index
    return index


def _load_standings_cache() -> dict:
    global _STANDINGS_CACHE
    if _STANDINGS_CACHE is not None:
        return _STANDINGS_CACHE
    try:
        from shared import paths as _paths
        path = Path(_paths.STANDINGS_CACHE_FILE)
    except Exception:
        path = Path(TEAM_DATA_DIR).resolve().parent.parent / "Output" / "Status" / "standings_cache.json"
    _STANDINGS_CACHE = _load_json(str(path), {})
    if not isinstance(_STANDINGS_CACHE, dict):
        _STANDINGS_CACHE = {}
    return _STANDINGS_CACHE


def _match_standings_entry(team_name: str, entries: list) -> dict | None:
    want = _normalize_name(team_name)
    if not want:
        return None
    exact = None
    soft = []
    for entry in entries or []:
        name = str((entry or {}).get("team") or "").strip()
        if not name:
            continue
        key = _normalize_name(name)
        if key == want:
            exact = entry
            break
        if want in key or key in want:
            soft.append(entry)
    if exact is not None:
        return exact
    if len(soft) == 1:
        return soft[0]
    return None


def domestic_stats_from_standings_cache(team_name: str, league: str | None = None) -> dict | None:
    """Read live domestic position/points from Output/Status/standings_cache.json."""
    cache = _load_standings_cache()
    if league:
        leagues = [league]
    else:
        # Prefer real domestic leagues over cup tables (FA Cup ranks are useless).
        leagues = sorted(
            cache.keys(),
            key=lambda name: (0 if _is_domestic_league_name(name) else 1, str(name)),
        )
    for league_name in leagues:
        if not league_name or _country_from_league(league_name) is None:
            continue
        if league is None and not _is_domestic_league_name(league_name):
            continue
        payload = cache.get(league_name) or {}
        groups = payload.get("groups") or []
        entries = []
        for group in groups:
            entries.extend(group.get("entries") or [])
        hit = _match_standings_entry(team_name, entries)
        if not hit:
            continue
        played = int(hit.get("P") or hit.get("played") or 0)
        points = float(hit.get("Pts") or hit.get("points") or 0)
        position = int(hit.get("position") or hit.get("rank") or 0)
        return {
            "name": str(hit.get("team") or team_name),
            "position": position or 999,
            "points": points,
            "played": played,
            "wins": int(hit.get("W") or 0),
            "draws": int(hit.get("D") or 0),
            "losses": int(hit.get("L") or 0),
            "goals_for": int(hit.get("GF") or 0),
            "goals_against": int(hit.get("GA") or 0),
            "league": league_name,
            "source": "standings_cache",
        }
    return None


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
    Falls back to league_teams / current_season seeds when the UEFA registry
    cache is empty.
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
    # Seed index fallback (exact + normalized).
    seeds = _load_seed_team_index()
    hit = seeds.get(team_name) or seeds.get(_normalize_name(team_name))
    if hit:
        return dict(hit)
    # Soft containment against seed keys (Man City ↔ Manchester City).
    want = _normalize_name(team_name)
    soft = []
    for key, info in seeds.items():
        if " " not in str(key):
            # Skip normalized duplicates without spaces weirdness; keys include both.
            pass
        kn = _normalize_name(key) if key != _normalize_name(key) else key
        # Only compare against display-name keys (contain uppercase originally).
        if key != _normalize_name(key):
            continue
        # key here is already normalized form stored alongside display names.
        if want and kn and (want in kn or kn in want):
            soft.append(info)
    # Also scan display names
    soft2 = []
    for key, info in seeds.items():
        if key == _normalize_name(key):
            continue
        kn = _normalize_name(key)
        if want and kn and (want in kn or kn in want) and abs(len(want) - len(kn)) <= 12:
            soft2.append(info)
    candidates = soft2 or soft
    if len(candidates) == 1:
        return dict(candidates[0])
    # Prefer unique league among soft matches.
    leagues = {c["league"] for c in candidates}
    if len(leagues) == 1 and candidates:
        return dict(candidates[0])
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
    # Even without a registry hit, standings_cache may know the team.
    domestic = None
    country = None
    league = None
    if entry:
        country = entry["country"]
        league = entry["league"]
        domestic = get_team_domestic_stats(
            team_name, league, uefa_domestic_tables, uefa_team_registry
        )
    if domestic is None:
        domestic = domestic_stats_from_standings_cache(team_name, league)
        if domestic and not league:
            league = domestic.get("league")
            country = country or _country_from_league(league)
    if not entry and not domestic:
        return {
            "country": None,
            "league": None,
            "league_strength": 0.50,
            "squad_value_eur_m": None,
            "domestic": None,
            "domestic_ppg": 1.2,
        }

    if country is None and league:
        country = _country_from_league(league)
    strength = get_country_strength(country, uefa_coefficients) if country else 0.50
    squad_value = get_team_squad_value(team_name, uefa_team_registry, uefa_squad_values)
    if domestic and domestic.get("played", 0) > 0:
        domestic_ppg = float(domestic["points"]) / float(domestic["played"])
    elif domestic and domestic.get("position"):
        # Early season: infer a mild PPG prior from table position alone.
        pos = float(domestic.get("position") or 10)
        domestic_ppg = max(0.6, min(2.6, 2.4 - (pos - 1) * 0.08))
    else:
        domestic_ppg = 1.2

    return {
        "country": country,
        "league": league,
        "league_strength": strength,
        "squad_value_eur_m": squad_value,
        "domestic": domestic,
        "domestic_ppg": domestic_ppg,
    }


# ── 7. Cup matchup priors (domestic position × country strength) ──


def _position_to_strength(position: float | None) -> float:
    """Soft rank transform: pos 1 → ~1.0, mid-table → ~0.55, bottom → ~0.28."""
    try:
        pos = float(position or 0.0)
    except (TypeError, ValueError):
        pos = 0.0
    if pos <= 0:
        return 0.45
    strength = 1.0 / (1.0 + (pos - 1.0) * 0.08)
    return max(0.05, min(1.0, strength))


def cup_team_strength(
    team_name: str,
    *,
    uefa_coefficients: dict | None = None,
    uefa_team_registry: dict | None = None,
    uefa_squad_values: dict | None = None,
    uefa_domestic_tables: dict | None = None,
) -> dict:
    """Scalar strength for a club in a European / cross-league cup tie.

    Combines:
      - domestic table position (rank transform)
      - domestic points-per-game
      - UEFA country / league coefficient
      - squad market-value scale

    Returns dict with ``strength`` in roughly [0.05, 1.25] plus metadata.
    """
    bundle = lookup_team_data_for_fallback(
        team_name,
        uefa_coefficients=uefa_coefficients,
        uefa_team_registry=uefa_team_registry,
        uefa_squad_values=uefa_squad_values,
        uefa_domestic_tables=uefa_domestic_tables,
    )
    domestic = bundle.get("domestic") or {}
    league_ls = float(bundle.get("league_strength") or 0.50)
    pos_strength = _position_to_strength(domestic.get("position") if domestic else None)
    ppg = float(bundle.get("domestic_ppg") or 1.2)
    # ~2.4 PPG is title-challenger pace across most European leagues.
    ppg_strength = max(0.15, min(1.0, ppg / 2.4))
    form_strength = 0.65 * pos_strength + 0.35 * ppg_strength
    value_scale = squad_value_scale_factor(bundle.get("squad_value_eur_m"))
    strength = max(0.05, min(1.35, form_strength * league_ls * value_scale))
    return {
        "team": team_name,
        "strength": strength,
        "position_strength": pos_strength,
        "ppg_strength": ppg_strength,
        "league_strength": league_ls,
        "squad_value_scale": value_scale,
        "country": bundle.get("country"),
        "league": bundle.get("league"),
        "domestic_position": (domestic or {}).get("position"),
        "domestic_ppg": ppg,
        "found": bool(bundle.get("country") or bundle.get("league") or domestic),
    }


def cup_matchup_prior(
    home_team: str,
    away_team: str,
    *,
    is_neutral: bool = False,
    uefa_coefficients: dict | None = None,
    uefa_team_registry: dict | None = None,
    uefa_squad_values: dict | None = None,
    uefa_domestic_tables: dict | None = None,
) -> dict | None:
    """Build H/D/A prior from domestic standing × league coefficient.

    Used when cup sides share little/no H2H history so model outputs do not
    collapse toward 1/3–1/3–1/3 (or a flat home prior).
    """
    home = cup_team_strength(
        home_team,
        uefa_coefficients=uefa_coefficients,
        uefa_team_registry=uefa_team_registry,
        uefa_squad_values=uefa_squad_values,
        uefa_domestic_tables=uefa_domestic_tables,
    )
    away = cup_team_strength(
        away_team,
        uefa_coefficients=uefa_coefficients,
        uefa_team_registry=uefa_team_registry,
        uefa_squad_values=uefa_squad_values,
        uefa_domestic_tables=uefa_domestic_tables,
    )
    if not home.get("found") and not away.get("found"):
        return None

    hs = float(home["strength"])
    as_ = float(away["strength"])
    if not is_neutral:
        hs += 0.05  # modest single-leg / first-leg home edge

    gap = abs(hs - as_)
    draw_prior = max(0.18, min(0.32, 0.32 - 0.55 * gap))
    if hs >= as_:
        home_share = 0.5 + 0.55 * min(1.0, gap)
    else:
        home_share = 0.5 - 0.55 * min(1.0, gap)
    home_share = max(0.12, min(0.88, home_share))
    away_share = 1.0 - home_share
    ph = (1.0 - draw_prior) * home_share
    pa = (1.0 - draw_prior) * away_share
    total = ph + draw_prior + pa
    if total <= 0:
        return None
    # Expected goals from relative strength (keeps sims from 1-0 / 0-1 only).
    home_xg = max(0.6, min(3.2, 1.15 + 1.4 * (hs - as_)))
    away_xg = max(0.4, min(2.8, 1.05 + 1.4 * (as_ - hs)))
    return {
        "H": ph / total,
        "D": draw_prior / total,
        "A": pa / total,
        "pred_home_goals": round(home_xg, 2),
        "pred_away_goals": round(away_xg, 2),
        "home_strength": hs,
        "away_strength": as_,
        "home_meta": home,
        "away_meta": away,
        "source": "cup_strength_prior",
    }


def blend_probs_with_cup_prior(
    model_probs: dict,
    prior: dict | None,
    *,
    prior_weight: float = 0.45,
) -> dict:
    """Blend model {H,D,A} with a cup strength prior. prior_weight in [0,1]."""
    if not prior:
        return {
            "H": float(model_probs.get("H") or model_probs.get("prob_home") or 0.0),
            "D": float(model_probs.get("D") or model_probs.get("prob_draw") or 0.0),
            "A": float(model_probs.get("A") or model_probs.get("prob_away") or 0.0),
        }
    w = max(0.0, min(1.0, float(prior_weight)))
    mh = float(model_probs.get("H") or model_probs.get("prob_home") or 0.0)
    md = float(model_probs.get("D") or model_probs.get("prob_draw") or 0.0)
    ma = float(model_probs.get("A") or model_probs.get("prob_away") or 0.0)
    mt = mh + md + ma
    if mt <= 1e-9:
        return {"H": prior["H"], "D": prior["D"], "A": prior["A"]}
    mh, md, ma = mh / mt, md / mt, ma / mt
    out = {
        "H": (1.0 - w) * mh + w * float(prior["H"]),
        "D": (1.0 - w) * md + w * float(prior["D"]),
        "A": (1.0 - w) * ma + w * float(prior["A"]),
    }
    total = out["H"] + out["D"] + out["A"]
    if total > 0:
        out = {k: v / total for k, v in out.items()}
    return out
