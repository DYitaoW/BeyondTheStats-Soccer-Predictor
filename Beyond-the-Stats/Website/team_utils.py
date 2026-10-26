"""Team name normalization and display mapping utilities."""
import os
import sys
import json
import config

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    import team_mapping_groups as tmg  # noqa: E402
except ImportError:
    tmg = None


def _load_team_display_mappings():
    """Load flattened team-name display mappings from mapping master JSON."""
    if not os.path.exists(config.TEAM_NAME_DISPLAY_MAPPING_FILE) or not tmg:
        return {}, {}
    try:
        payload = tmg.load_team_mapping(config.TEAM_NAME_DISPLAY_MAPPING_FILE)
    except Exception:
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}

    db_to_display = {}
    display_to_db = {}
    for _, comp_map in payload.items():
        if not isinstance(comp_map, dict):
            continue
        for raw_name, mapped_name in comp_map.items():
            db_name = str(raw_name or "").strip()
            display_name = str(mapped_name or "").strip()
            if not db_name or not display_name:
                continue
            db_to_display.setdefault(db_name, display_name)
            display_to_db.setdefault(display_name, db_name)
    return db_to_display, display_to_db


TEAM_DB_TO_DISPLAY, TEAM_DISPLAY_TO_DB = _load_team_display_mappings()


def _team_name_for_display(name):
    """Map DB/canonical team names to UI display names."""
    if isinstance(name, dict):
        text = str(
            name.get("name")
            or name.get("team")
            or name.get("team_name")
            or name.get("display_name")
            or ""
        ).strip()
    else:
        text = str(name or "").strip()
    if not text:
        return ""
    if not config.USE_DISPLAY_NAME_MAPPING:
        return text
    return TEAM_DB_TO_DISPLAY.get(text, text)


def _team_name_for_db(name):
    """Map UI display names back to DB/canonical team names."""
    if isinstance(name, dict):
        text = str(
            name.get("name")
            or name.get("team")
            or name.get("team_name")
            or name.get("display_name")
            or ""
        ).strip()
    else:
        text = str(name or "").strip()
    if not text:
        return ""
    if not config.USE_DISPLAY_NAME_MAPPING:
        return text
    return TEAM_DISPLAY_TO_DB.get(text, text)


def _normalize_team_key(name):
    """Normalize team name for case-insensitive comparison."""
    if isinstance(name, dict):
        text = str(
            name.get("name")
            or name.get("team")
            or name.get("team_name")
            or name.get("display_name")
            or ""
        ).strip().lower()
        return text
    return str(name or "").strip().lower()


def _to_float(value, default=0.0):
    """Convert value to float, returning default on error."""
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value):
    """Convert value to int, returning None on error."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


# Standardized 3-tier team naming integrations
try:
    from standardized_teams import (
        resolve_team_info,
        decorate_game_team_names,
        decorate_standings_entry,
        decorate_standings_table,
        get_standardized_teams_list,
        generate_fallback_team_info,
        get_standardized_team_manager,
    )
except ImportError:
    try:
        from Website.standardized_teams import (
            resolve_team_info,
            decorate_game_team_names,
            decorate_standings_entry,
            decorate_standings_table,
            get_standardized_teams_list,
            generate_fallback_team_info,
            get_standardized_team_manager,
        )
    except Exception:
        def resolve_team_info(team_name, competition=""):
            return {
                "full_name": str(team_name),
                "short_name": str(team_name),
                "abbreviation": str(team_name)[:3].upper(),
                "id": str(team_name).lower(),
                "logo_file": "",
                "league": competition or "Unknown",
                "aliases": [str(team_name)],
            }

        def decorate_game_team_names(game, competition=""):
            return game

        def decorate_standings_entry(entry, competition=""):
            return entry

        def decorate_standings_table(table_data, competition=""):
            return table_data

        def get_standardized_teams_list():
            return []

        def get_standardized_team_manager():
            return None

        def generate_fallback_team_info(raw_name, competition=""):
            return {
                "full_name": str(raw_name),
                "short_name": str(raw_name),
                "abbreviation": str(raw_name)[:3].upper(),
                "id": str(raw_name).lower(),
                "logo_file": "",
                "league": competition or "Unknown",
                "aliases": [str(raw_name)],
            }
