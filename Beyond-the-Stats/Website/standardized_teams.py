"""
Standardized team naming module for BeyondTheStats APIs.

Provides consistent 3-tier team naming (full_name, short_name, abbreviation)
and metadata (id, logo_file, league) based on standardized_team_mappings.json
used by the frontend app and website.

Includes deterministic fallback heuristics for teams not yet present in the JSON,
and non-destructive payload decorators so backend pipelines, training data,
and database keys remain completely isolated and intact.
"""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_ROOT_DIR, "Data")

# Primary and fallback file locations
CANDIDATE_MAPPING_PATHS = [
    r"p:\standardized_team_mappings.json",
    os.path.join(_DATA_DIR, "standardized_team_mappings.json"),
    os.path.join(_ROOT_DIR, "Website", "Data", "standardized_team_mappings.json"),
    os.path.join(_ROOT_DIR, "standardized_team_mappings.json"),
]


def strip_accents(text: str) -> str:
    """Normalize unicode string and strip diacritical marks."""
    if not text:
        return ""
    try:
        norm = unicodedata.normalize("NFKD", text)
        return "".join(c for c in norm if not unicodedata.combining(c))
    except Exception:
        return text


def normalize_lookup_key(text: str) -> str:
    """Produce clean alphanumeric lowercase key for insensitive matching."""
    if not text:
        return ""
    clean = strip_accents(str(text)).lower()
    clean = re.sub(r"[^a-z0-9]+", " ", clean)
    return " ".join(clean.split())


def generate_team_slug(name: str) -> str:
    """Generate a clean URL-safe kebab-case slug for a team name."""
    clean = strip_accents(str(name or "")).lower()
    clean = re.sub(r"[^a-z0-9]+", "-", clean)
    clean = clean.strip("-")
    return clean or "unknown-team"


# Generic club descriptor words to strip when creating short names
_CLUB_DESCRIPTOR_PATTERNS = [
    r"\bF\.?C\.?\b",
    r"\bC\.?F\.?\b",
    r"\bS\.?C\.?\b",
    r"\bA\.?F\.?C\.?\b",
    r"\bA\.?C\.?\b",
    r"\bB\.?C\.?\b",
    r"\bC\.?D\.?\b",
    r"\bR\.?C\.?\b",
    r"\bU\.?D\.?\b",
    r"\bU\.?S\.?\b",
    r"\bS\.?S\.?\b",
    r"\bS\.?V\.?\b",
    r"\bF\.?K\.?\b",
    r"\bS\.?K\.?\b",
    r"\bVfB\b",
    r"\bVfL\b",
    r"\bTSG\b",
    r"\bFSV\b",
    r"\bClub\b",
    r"\bCalcio\b",
    r"\bde Fútbol\b",
    r"\bde Futbol\b",
    r"\bBalompié\b",
    r"\bBalompie\b",
    r"\b1907\b",
    r"\b1909\b",
    r"\b1913\b",
    r"\b1899\b",
    r"\b1846\b",
    r"\b05\b",
    r"\b04\b",
    r"^\s*1\.\s*",
]

# Stopwords to ignore when generating 3-letter abbreviation from words
_ABBR_STOPWORDS = {
    "fc", "cf", "sc", "afc", "ac", "bc", "cd", "rc", "ud", "us", "ss", "sv",
    "fk", "sk", "vfb", "vfl", "tsg", "fsv", "club", "calcio", "de", "del", "la",
    "the", "and", "und", "et", "of", "1", "05", "04", "1907", "1909", "1913", "1899", "1846"
}


def generate_fallback_team_info(raw_name: str, competition: str = "") -> dict:
    """
    Generate deterministic, professional 3-tier names and metadata
    for a team not present in standardized_team_mappings.json.
    """
    name = str(raw_name or "").strip()
    if not name:
        return {
            "id": "unknown",
            "full_name": "Unknown",
            "short_name": "Unknown",
            "abbreviation": "UNK",
            "league": competition or "Unknown",
            "logo_file": "unknown.png",
            "aliases": ["Unknown"],
        }

    # 1. Clean full_name
    # Expand common abbreviations
    full = name
    full = re.sub(r"\bUtd\b", "United", full, flags=re.IGNORECASE)
    full = re.sub(r"\bAth\b", "Athletic", full, flags=re.IGNORECASE)
    full = re.sub(r"\bWand\b", "Wanderers", full, flags=re.IGNORECASE)
    full = re.sub(r"\bRovers\b", "Rovers", full, flags=re.IGNORECASE)
    
    # Capitalize acronyms correctly
    for acr in ("FC", "CF", "SC", "AFC", "AC", "BC", "CD", "RC", "UD", "US", "SS", "SV", "FK", "SK", "VfB", "VfL", "TSG", "FSV", "USA", "LA", "NY"):
        full = re.sub(rf"\b{acr}\b", acr, full, flags=re.IGNORECASE)
    full = " ".join(full.split())

    # 2. Derive short_name
    short = full
    for pat in _CLUB_DESCRIPTOR_PATTERNS:
        short = re.sub(pat, "", short, flags=re.IGNORECASE)
    short = " ".join(short.split())
    # If stripping descriptors left nothing or just 1 letter, fall back to full
    if len(short) < 2:
        short = full

    # 3. Derive 3-character abbreviation
    # Strip accents for abbreviation calculation
    clean_abbr_src = strip_accents(full)
    # Split into words, ignoring punctuation
    words = [w for w in re.findall(r"[A-Za-z0-9]+", clean_abbr_src) if w.lower() not in _ABBR_STOPWORDS]
    if not words:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", clean_abbr_src)]

    abbr = ""
    if len(words) >= 3:
        # First letters of first 3 words
        abbr = (words[0][0] + words[1][0] + words[2][0]).upper()
    elif len(words) == 2:
        w1, w2 = words[0], words[1]
        if len(w1) >= 2:
            abbr = (w1[:2] + w2[0]).upper()
        else:
            abbr = (w1[0] + w2[:2]).upper()
    elif len(words) == 1:
        w = words[0]
        if len(w) >= 3:
            abbr = w[:3].upper()
        else:
            abbr = (w + "X" * (3 - len(w))).upper()
    else:
        abbr = "UNK"

    # Enforce exactly 3 alphanumeric characters
    abbr = re.sub(r"[^A-Z0-9]", "X", abbr.upper())
    if len(abbr) < 3:
        abbr = (abbr + "XXX")[:3]
    elif len(abbr) > 3:
        abbr = abbr[:3]

    slug = generate_team_slug(full)

    return {
        "id": slug,
        "full_name": full,
        "short_name": short,
        "abbreviation": abbr,
        "league": competition or "Other",
        "logo_file": f"{slug}.png",
        "aliases": [full, short, name],
    }


class StandardizedTeamManager:
    """Thread-safe catalog and resolver for standardized team mappings."""

    _instance: Optional[StandardizedTeamManager] = None
    _lock = threading.Lock()

    def __init__(self):
        self._by_id: dict[str, dict] = {}
        self._by_name: dict[str, dict] = {}
        self._by_abbr: dict[str, list[dict]] = {}
        self._teams: list[dict] = []
        self._loaded_path: str = ""
        self._load_mappings()

    @classmethod
    def get_instance(cls) -> StandardizedTeamManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _load_mappings(self) -> None:
        """Load mapping file from candidate locations."""
        loaded_data = None
        for path in CANDIDATE_MAPPING_PATHS:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                        if isinstance(data, dict) and "teams" in data:
                            loaded_data = data
                            self._loaded_path = path
                            break
                except Exception:
                    continue

        if not loaded_data:
            return

        teams = loaded_data.get("teams", [])
        for entry in teams:
            if not isinstance(entry, dict):
                continue
            t_id = str(entry.get("id") or "").strip()
            full_name = str(entry.get("full_name") or "").strip()
            short_name = str(entry.get("short_name") or "").strip()
            abbr = str(entry.get("abbreviation") or "").strip().upper()
            league = str(entry.get("league") or "").strip()
            logo_file = str(entry.get("logo_file") or "").strip()
            aliases = entry.get("aliases") or []

            clean_entry = {
                "id": t_id or generate_team_slug(full_name or short_name),
                "full_name": full_name or short_name,
                "short_name": short_name or full_name,
                "abbreviation": abbr or generate_fallback_team_info(full_name)["abbreviation"],
                "league": league or "Unknown",
                "logo_file": logo_file or f"{t_id}.png",
                "aliases": list(aliases) if isinstance(aliases, list) else [full_name, short_name],
            }

            self._teams.append(clean_entry)
            if clean_entry["id"]:
                self._by_id[clean_entry["id"]] = clean_entry

            # Index abbreviations
            if clean_entry["abbreviation"]:
                self._by_abbr.setdefault(clean_entry["abbreviation"], []).append(clean_entry)

            # Index names and aliases under normalized keys
            names_to_index = [
                full_name,
                short_name,
                clean_entry["id"],
                strip_accents(full_name),
                strip_accents(short_name),
            ]
            if isinstance(aliases, list):
                names_to_index.extend(aliases)
                names_to_index.extend([strip_accents(a) for a in aliases])

            for n in names_to_index:
                k = normalize_lookup_key(n)
                if k and k not in self._by_name:
                    self._by_name[k] = clean_entry

    def resolve_team_info(self, raw_input: Any, competition: str = "") -> dict:
        """
        Resolve team info from raw input (string or dict).
        Returns { id, full_name, short_name, abbreviation, league, logo_file }.
        """
        if isinstance(raw_input, dict):
            text = str(
                raw_input.get("full_name")
                or raw_input.get("name")
                or raw_input.get("team")
                or raw_input.get("team_name")
                or raw_input.get("display_name")
                or raw_input.get("short_name")
                or ""
            ).strip()
            t_id = str(raw_input.get("id") or "").strip()
            if t_id and t_id in self._by_id:
                return dict(self._by_id[t_id])
        else:
            text = str(raw_input or "").strip()

        if not text:
            return generate_fallback_team_info("Unknown", competition)

        # 1. Exact lookup by ID
        slug_key = generate_team_slug(text)
        if slug_key in self._by_id:
            return dict(self._by_id[slug_key])

        # 2. Normalized key lookup
        norm_key = normalize_lookup_key(text)
        if norm_key in self._by_name:
            return dict(self._by_name[norm_key])

        # 3. Canonical name fallback lookup
        try:
            from competition_rules import canonical_team_name
            canon = canonical_team_name(text, competition)
            if canon:
                canon_key = normalize_lookup_key(canon)
                if canon_key in self._by_name:
                    return dict(self._by_name[canon_key])
        except Exception:
            pass

        # 4. Abbreviation lookup (e.g. "ARS", "BHA", "MCI")
        text_upper = text.upper().strip()
        if len(text_upper) == 3 and text_upper in self._by_abbr:
            candidates = self._by_abbr[text_upper]
            if len(candidates) == 1:
                return dict(candidates[0])
            # If multiple candidates, try to match by competition/league
            if competition:
                comp_norm = normalize_lookup_key(competition)
                for cand in candidates:
                    if normalize_lookup_key(cand.get("league", "")) in comp_norm:
                        return dict(cand)
            return dict(candidates[0])

        # 5. Fuzzy match against aliases (e.g. stripped words)
        words = norm_key.split()
        if len(words) > 1:
            # Try without common words
            stripped_words = [w for w in words if w not in _ABBR_STOPWORDS]
            if stripped_words:
                stripped_key = " ".join(stripped_words)
                if stripped_key in self._by_name:
                    return dict(self._by_name[stripped_key])

        # 6. Fallback generation for unmapped team
        fallback = generate_fallback_team_info(text, competition)
        self._by_name[norm_key] = fallback
        return dict(fallback)

    def decorate_game_team_names(self, game: dict, competition: str = "") -> dict:
        """
        Add standardized 3-tier names, id, and logo to a game payload
        without mutating or removing any existing keys.
        """
        if not isinstance(game, dict):
            return game
        if game.get("home_team_info") and game.get("away_team_info"):
            return game

        comp = game.get("competition") or competition or ""
        home_raw = game.get("home_team") or game.get("canonical_home_team") or ""
        away_raw = game.get("away_team") or game.get("canonical_away_team") or ""

        h_info = self.resolve_team_info(home_raw, comp)
        a_info = self.resolve_team_info(away_raw, comp)

        # Home team variations
        game["home_team_full"] = h_info["full_name"]
        game["home_team_full_name"] = h_info["full_name"]
        game["home_team_short"] = h_info["short_name"]
        game["home_team_short_name"] = h_info["short_name"]
        game["home_team_abbr"] = h_info["abbreviation"]
        game["home_team_abbreviation"] = h_info["abbreviation"]
        game["home_team_id"] = h_info["id"]
        game["home_team_logo"] = h_info["logo_file"]
        game["home_team_info"] = h_info

        # Away team variations
        game["away_team_full"] = a_info["full_name"]
        game["away_team_full_name"] = a_info["full_name"]
        game["away_team_short"] = a_info["short_name"]
        game["away_team_short_name"] = a_info["short_name"]
        game["away_team_abbr"] = a_info["abbreviation"]
        game["away_team_abbreviation"] = a_info["abbreviation"]
        game["away_team_id"] = a_info["id"]
        game["away_team_logo"] = a_info["logo_file"]
        game["away_team_info"] = a_info

        return game

    def decorate_standings_entry(self, entry: dict, competition: str = "") -> dict:
        """
        Add standardized 3-tier names, id, and logo to a league table row
        without mutating existing score/ranking keys.
        """
        if not isinstance(entry, dict):
            return entry
        if entry.get("team_info"):
            return entry

        team_raw = entry.get("team") or entry.get("team_name") or entry.get("name") or ""
        t_info = self.resolve_team_info(team_raw, competition)

        entry["team_full"] = t_info["full_name"]
        entry["team_full_name"] = t_info["full_name"]
        entry["team_short"] = t_info["short_name"]
        entry["team_short_name"] = t_info["short_name"]
        entry["team_abbr"] = t_info["abbreviation"]
        entry["team_abbreviation"] = t_info["abbreviation"]
        entry["team_id"] = t_info["id"]
        entry["team_logo"] = t_info["logo_file"]
        entry["team_info"] = t_info

        return entry

    def decorate_standings_table(self, table_data: Any, competition: str = "") -> Any:
        """
        Recursively/flexibly decorate league table structures:
        - Dict with "groups": iterates groups and decorates each entry in group["entries"]
        - Dict of tables (comp_name -> list_of_entries or comp_name -> table_dict)
        - List of entries: decorates each entry
        """
        if not table_data:
            return table_data

        if isinstance(table_data, list):
            for item in table_data:
                if isinstance(item, dict):
                    if "team" in item or "team_name" in item or "name" in item:
                        self.decorate_standings_entry(item, competition)
                    elif "entries" in item and isinstance(item.get("entries"), list):
                        for entry in item["entries"]:
                            if isinstance(entry, dict):
                                self.decorate_standings_entry(entry, competition)
            return table_data

        if isinstance(table_data, dict):
            # Dict with "groups"
            if "groups" in table_data and isinstance(table_data["groups"], list):
                comp = table_data.get("competition") or competition
                for group in table_data["groups"]:
                    if isinstance(group, dict) and isinstance(group.get("entries"), list):
                        for entry in group["entries"]:
                            if isinstance(entry, dict):
                                self.decorate_standings_entry(entry, comp)
            # Dict with "tables"
            elif "tables" in table_data and isinstance(table_data["tables"], dict):
                for comp, tbl in table_data["tables"].items():
                    self.decorate_standings_table(tbl, comp)
            # Generic dict mapping competitions to table dicts or lists
            elif "competition" not in table_data:
                for comp, tbl in table_data.items():
                    if isinstance(tbl, (dict, list)):
                        self.decorate_standings_table(tbl, comp)

        return table_data

    def get_all_teams(self) -> list[dict]:
        """Return list of all registered standardized team objects."""
        return [dict(t) for t in self._teams]


# Module-level convenience functions
_MANAGER = StandardizedTeamManager.get_instance()

def get_standardized_team_manager() -> StandardizedTeamManager:
    return _MANAGER

def resolve_team_info(team_name: Any, competition: str = "") -> dict:
    return _MANAGER.resolve_team_info(team_name, competition)

def decorate_game_team_names(game: dict, competition: str = "") -> dict:
    return _MANAGER.decorate_game_team_names(game, competition)

def decorate_standings_entry(entry: dict, competition: str = "") -> dict:
    return _MANAGER.decorate_standings_entry(entry, competition)

def decorate_standings_table(table_data: Any, competition: str = "") -> Any:
    return _MANAGER.decorate_standings_table(table_data, competition)

def get_standardized_teams_list() -> list[dict]:
    return _MANAGER.get_all_teams()
