"""Cross-competition team mapping helpers (country leagues, cups, UEFA)."""
from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata

INTERNATIONAL_PREFIXES = (
    "Europe/",
    "International/",
    "North America/",
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
        "ii": "b",
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

    if comp.startswith("North America/"):
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
    raw_lower = api_name.lower()
    is_sociedad_ref = (
        "sociedad" in raw_lower
        or api_key in {"sociedad", "realsociedad", "sociedadb", "realsociedadb"}
    )
    is_explicit_b = bool(re.search(r"\b(b|ii|2)\b", raw_lower)) or api_name.strip().endswith(" B") or api_name.strip().endswith(" II")

    for source_comp in mapping_lookup_competitions(competition, mapping):
        comp_map = mapping.get(source_comp, {})
        if not isinstance(comp_map, dict):
            continue

        direct = str(comp_map.get(api_name, "")).strip()
        if direct:
            if is_sociedad_ref or direct in {"Sociedad", "Sociedad B"}:
                if competition == "Spain/La Liga 2":
                    return "Sociedad B", source_comp
                elif not is_explicit_b:
                    return "Sociedad", source_comp
            return direct, source_comp

        if api_key:
            for map_key, map_val in comp_map.items():
                if normalize_team_key(map_key) == api_key:
                    mapped = str(map_val).strip()
                    if mapped:
                        if is_sociedad_ref or mapped in {"Sociedad", "Sociedad B"}:
                            if competition == "Spain/La Liga 2":
                                return "Sociedad B", source_comp
                            elif not is_explicit_b:
                                return "Sociedad", source_comp
                        return mapped, source_comp
    return "", ""


def candidate_teams_for_competition(competition: str, context: dict) -> list[str]:
    """Teams eligible for fuzzy resolution (same country or global for UEFA)."""
    team_competition_map = context.get("team_competition_map", {})
    available = [str(team).strip() for team in context.get("available_teams", []) if str(team).strip()]
    comp = str(competition or "").strip()

    if is_international_competition(comp):
        # International competitions (Champions League, Europa League, Conference League, World Cup, etc.)
        # never feature domestic reserve / "B" teams.
        return [
            team for team in available
            if not re.search(r"\b(B|II|2)\b$", team) and not team.endswith(" B") and not team.endswith(" II")
        ]

    country = competition_country(comp)
    if country:
        country_teams = [
            team
            for team in available
            if competition_country(str(team_competition_map.get(team, ""))) == country
        ]
        if comp == "Spain/La Liga":
            # Real Sociedad B is in La Liga 2, not La Liga
            country_teams = [t for t in country_teams if t != "Sociedad B"]
        elif comp == "Spain/La Liga 2":
            # Real Sociedad is in La Liga, not La Liga 2
            country_teams = [t for t in country_teams if t != "Sociedad"]
        if country_teams:
            return country_teams

    same_comp = [team for team in available if str(team_competition_map.get(team, "")).strip() == comp]
    return same_comp if same_comp else available


def fuzzy_resolve_team_name(raw_name: str, valid_names: list[str]) -> str | None:
    """Fuzzy match *raw_name* against *valid_names* using normalized keys."""
    raw_name = str(raw_name or "").strip()
    if not raw_name or not valid_names:
        return None

    # Explicit Real Sociedad vs Real Sociedad B disambiguation if both or either is present
    raw_lower = raw_name.lower()
    raw_parts = set(re.findall(r"\b\w+\b", raw_lower))
    if "sociedad" in raw_parts:
        is_reserve = bool(raw_parts & {"b", "ii", "2"}) or raw_lower.endswith(" b") or raw_lower.endswith(" ii")
        if is_reserve and "Sociedad B" in valid_names:
            return "Sociedad B"
        if not is_reserve and "Sociedad" in valid_names:
            return "Sociedad"

    key = normalize_team_key(raw_name)
    if not key:
        return None

    by_key = {normalize_team_key(team): team for team in valid_names}
    if key in by_key:
        return by_key[key]

    # Require minimum normalized key length of 4 characters to prevent 1-3 char tokens
    # (like "b" or "sc") from erroneously matching inside unrelated team names.
    contained_by_raw = [
        team for team in valid_names
        if normalize_team_key(team)
        and len(normalize_team_key(team)) >= 4
        and normalize_team_key(team) in key
    ]
    if contained_by_raw:
        # Prefer the longest key so "Inter" never beats "Inter Miami" when the
        # raw name is "Inter Miami" / "Inter Miami CF".
        contained_by_raw.sort(key=lambda t: len(normalize_team_key(t) or ""), reverse=True)
        best = contained_by_raw[0]
        best_key = normalize_team_key(best) or ""
        # Reject a strict prefix of another valid name that also matches the raw key
        # (e.g. "Inter" inside "Inter Miami") unless it is the only candidate.
        longer_also = [
            t for t in valid_names
            if (normalize_team_key(t) or "").startswith(best_key)
            and len(normalize_team_key(t) or "") > len(best_key)
            and (normalize_team_key(t) or "") in key
        ]
        if longer_also:
            longer_also.sort(key=lambda t: len(normalize_team_key(t) or ""), reverse=True)
            return longer_also[0]
        if len(contained_by_raw) == 1 or len(normalize_team_key(contained_by_raw[0]) or "") > len(
            normalize_team_key(contained_by_raw[1]) or ""
        ):
            return best

    contains = [
        team for team in valid_names
        if len(key) >= 4 and key in normalize_team_key(team)
    ]
    if contains:
        contains.sort(key=lambda t: len(normalize_team_key(t) or ""), reverse=True)
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

    is_reserve = canonical in {"Sociedad B", "Celta B", "Barcelona B", "Villarreal B", "Sevilla B", "Real Madrid B"} or canonical.endswith(" B") or canonical.endswith(" II")

    if propagate_country and country:
        for comp_key in list(mapping.keys()):
            if competition_country(comp_key) == country:
                # Do NOT propagate reserve teams to top-tier leagues or cups where reserve teams are banned
                if is_reserve and comp_key in {"Spain/La Liga", "Spain/Copa del Rey"}:
                    continue
                # Do NOT propagate senior team to lower reserve divisions
                if canonical == "Sociedad" and comp_key == "Spain/La Liga 2":
                    continue
                mapping.setdefault(comp_key, {})[api_name] = canonical

    if propagate_international and country and not competition.startswith("North America/"):
        for comp_key in list(mapping.keys()):
            if is_international_competition(comp_key):
                # Do NOT propagate reserve teams to UEFA or international competitions
                if is_reserve:
                    continue
                mapping.setdefault(comp_key, {})[api_name] = canonical


# --- Shared mapping file helpers ---------------------------------------------
#
# The git-main master file (team_name_mapping_master.json) is NEVER modified by
# the pipeline.  Auto-learned mappings are appended to
# team_name_mapping_master_new.json in the same directory so they can be
# reviewed and merged into the master manually.  Reads always overlay the new
# file on top of the master, with master entries taking priority.

NEW_MAPPING_FILENAME = "team_name_mapping_master_new.json"


def mapping_new_file_path(master_path):
    """Path of the *_new.json log next to a master mapping file."""
    return os.path.join(os.path.dirname(master_path), NEW_MAPPING_FILENAME)


def _read_mapping_json(path, encoding="utf-8-sig"):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding=encoding) as fh:
            payload = json.load(fh)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_team_mapping(master_path):
    """Load a mapping master, then overlay *_new.json on top.

    Master entries always win over *_new.json entries for the same
    (competition, api_name) key, so the reviewed master is never overridden by
    pipeline-learned mappings.
    """
    merged = _read_mapping_json(master_path)
    new_data = _read_mapping_json(mapping_new_file_path(master_path))
    for competition, entries in new_data.items():
        if not isinstance(entries, dict):
            continue
        section = merged.setdefault(str(competition), {})
        if not isinstance(section, dict):
            section = {}
            merged[str(competition)] = section
        for api_name, mapped_name in entries.items():
            key = str(api_name).strip()
            if not key or key in section:
                continue
            section[key] = str(mapped_name).strip()
    return merged


def save_team_mapping(master_path, mapping):
    """Persist only NEW mapping entries to *_new.json; never touch the master.

    Returns the number of entries newly appended to *_new.json.  Entries already
    present in the master (or already in *_new.json) are skipped so the master
    file is left untouched and no pipeline-learned entry overrides it.
    """
    if not isinstance(mapping, dict) or not mapping:
        return 0
    master = load_team_mapping(master_path)
    new_path = mapping_new_file_path(master_path)
    log_data = _read_mapping_json(new_path, encoding="utf-8")
    new_additions = {}
    for competition, entries in mapping.items():
        if not isinstance(entries, dict):
            continue
        comp = str(competition)
        master_section = master.get(comp, {})
        if not isinstance(master_section, dict):
            master_section = {}
        log_section = log_data.setdefault(comp, {})
        if not isinstance(log_section, dict):
            log_section = {}
            log_data[comp] = log_section
        for api_name, mapped_name in entries.items():
            key = str(api_name).strip()
            if not key or key in master_section or key in log_section:
                continue
            log_section[key] = str(mapped_name).strip()
            new_additions.setdefault(comp, {})[key] = str(mapped_name).strip()
    if new_additions:
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        with open(new_path, "w", encoding="utf-8") as fh:
            json.dump(log_data, fh, indent=2, ensure_ascii=False)
        return sum(len(entries) for entries in new_additions.values())
    return 0


def load_name_mapping_flat(master_path):
    """Flatten master + *_new.json into {lower_api_name: canonical}."""
    flat = {}
    merged = load_team_mapping(master_path)
    for entries in merged.values():
        if not isinstance(entries, dict):
            continue
        for api_name, mapped_name in entries.items():
            flat[str(api_name).strip().lower()] = str(mapped_name).strip()
    return flat
