"""Cross-competition team mapping helpers (country leagues, cups, UEFA)."""
from __future__ import annotations

import difflib
import unicodedata

INTERNATIONAL_PREFIXES = (
    "UEFA/",
    "Europe/",
    "FIFA/",
    "CONCACAF/",
)

CONCACAF_REGION_PREFIXES = (
    "United States/",
    "Mexico/",
)

# Normalized-key aliases so ESPN short names match football-data canonicals.
# Keys/values are post-stop-word strings (no spaces/punctuation).
TEAM_KEY_ALIASES = {
    # MLS — ESPN short forms vs football-data.co.uk USA.csv names
    "lagalaxy": "losangelesgalaxy",
    "losangelesgalaxy": "losangelesgalaxy",
    # "Los Angeles FC" drops stop-word "fc" → losangeles; ESPN "LAFC" → lafc
    "lafc": "losangeles",
    "losangelesfc": "losangeles",
    "rsl": "realsaltlake",
    "intermiamicf": "intermiami",
    "atlantautd": "atlanta",
    "atlantaunited": "atlanta",
    "atlantaunitedfc": "atlanta",
}


def normalize_team_key(name) -> str:
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip().replace("&", " and ")
    text = text.replace("'", "").replace(".", " ")
    text = text.replace("-", " ")
    parts = [p for p in text.split() if p]
    token_aliases = {
        "weds": "wednesday",
        "utd": "united",
        "st": "saint",
    }
    parts = [token_aliases.get(p, p) for p in parts]
    stop_words = {
        "fc",
        "cf",
        "ac",
        "ca",
        "afc",
        "us",
        "sc",
        "sv",
        "fk",
        "the",
        "club",
        "de",
        "calcio",
        "team",
        "football",
        "sociedad",
        "and",
        "town",
        "athletic",
        "county",
        "albion",
        "wanderers",
        "hotspur",
        "city",
        "united",
    }
    parts = [p for p in parts if p not in stop_words]
    key = "".join(parts)
    return TEAM_KEY_ALIASES.get(key, key)


def competition_country(competition: str) -> str | None:
    comp = str(competition or "").strip()
    if "/" not in comp:
        return None
    country = comp.split("/", 1)[0].strip()
    return country or None


def is_international_competition(competition: str) -> bool:
    comp = str(competition or "").strip()
    return any(comp.startswith(prefix) for prefix in INTERNATIONAL_PREFIXES)


def is_domestic_competition(competition: str) -> bool:
    comp = str(competition or "").strip()
    return bool(competition_country(comp)) and not is_international_competition(comp)


def mapping_lookup_competitions(competition: str, mapping: dict) -> list[str]:
    """Return competitions to search for an API name, in priority order."""
    comp = str(competition or "").strip()
    if not comp:
        return []

    ordered: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        key = str(name or "").strip()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)

    add(comp)
    country = competition_country(comp)

    if country:
        same_country = sorted(
            (k for k in mapping.keys() if competition_country(k) == country),
            key=lambda item: (item != comp, item.lower()),
        )
        for key in same_country:
            add(key)

    if comp.startswith("CONCACAF/"):
        for prefix in CONCACAF_REGION_PREFIXES:
            for key in sorted(mapping.keys(), key=str.lower):
                if str(key).startswith(prefix):
                    add(key)
    elif is_international_competition(comp):
        for key in sorted(mapping.keys(), key=str.lower):
            if is_domestic_competition(key):
                add(key)
        for key in sorted(mapping.keys(), key=str.lower):
            if is_international_competition(key):
                add(key)

    return ordered


def lookup_mapped_name(api_name: str, competition: str, mapping: dict) -> tuple[str, str]:
    """Find a canonical team name across related competitions."""
    api_name = str(api_name or "").strip()
    if not api_name or not isinstance(mapping, dict):
        return "", ""

    api_key = normalize_team_key(api_name)
    for source_comp in mapping_lookup_competitions(competition, mapping):
        comp_map = mapping.get(source_comp, {})
        if not isinstance(comp_map, dict):
            continue

        direct = str(comp_map.get(api_name, "")).strip()
        if direct:
            return direct, source_comp

        if api_key:
            for map_key, map_val in comp_map.items():
                if normalize_team_key(map_key) == api_key:
                    mapped = str(map_val).strip()
                    if mapped:
                        return mapped, source_comp
    return "", ""


def candidate_teams_for_competition(competition: str, context: dict) -> list[str]:
    """Teams eligible for fuzzy resolution (same country or global for UEFA)."""
    team_competition_map = context.get("team_competition_map", {})
    available = [str(team).strip() for team in context.get("available_teams", []) if str(team).strip()]
    comp = str(competition or "").strip()

    if is_international_competition(comp):
        return available

    country = competition_country(comp)
    if country:
        country_teams = [
            team
            for team in available
            if competition_country(str(team_competition_map.get(team, ""))) == country
        ]
        if country_teams:
            return country_teams

    same_comp = [team for team in available if str(team_competition_map.get(team, "")).strip() == comp]
    return same_comp if same_comp else available


def fuzzy_resolve_team_name(raw_name: str, valid_names: list[str]) -> str | None:
    """Fuzzy match *raw_name* against *valid_names* using normalized keys."""
    raw_name = str(raw_name or "").strip()
    if not raw_name or not valid_names:
        return None

    key = normalize_team_key(raw_name)
    if not key:
        return None

    by_key = {normalize_team_key(team): team for team in valid_names}
    if key in by_key:
        return by_key[key]

    contained_by_raw = [
        team for team in valid_names if normalize_team_key(team) and normalize_team_key(team) in key
    ]
    if len(contained_by_raw) == 1:
        return contained_by_raw[0]

    contains = [team for team in valid_names if key in normalize_team_key(team)]
    if len(contains) == 1:
        return contains[0]

    close = difflib.get_close_matches(key, list(by_key.keys()), n=1, cutoff=0.88)
    if close:
        return by_key[close[0]]
    return None


def store_team_mapping(
    mapping: dict,
    competition: str,
    api_name: str,
    canonical: str,
    *,
    propagate_country: bool = True,
    propagate_international: bool = False,
) -> None:
    """Persist a mapping and optionally mirror it across related competitions."""
    competition = str(competition or "").strip()
    api_name = str(api_name or "").strip()
    canonical = str(canonical or "").strip()
    if not competition or not api_name:
        return

    mapping.setdefault(competition, {})[api_name] = canonical
    country = competition_country(competition)

    if propagate_country and country:
        for comp_key in list(mapping.keys()):
            if competition_country(comp_key) == country:
                mapping.setdefault(comp_key, {})[api_name] = canonical

    if propagate_international and country and not competition.startswith("CONCACAF/"):
        for comp_key in list(mapping.keys()):
            if is_international_competition(comp_key):
                mapping.setdefault(comp_key, {})[api_name] = canonical
