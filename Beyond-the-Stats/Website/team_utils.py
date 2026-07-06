"""Team name normalization and display mapping utilities."""
import os
import json
import config


def _load_team_display_mappings():
    """Load flattened team-name display mappings from mapping master JSON."""
    if not os.path.exists(config.TEAM_NAME_DISPLAY_MAPPING_FILE):
        return {}, {}
    try:
        with open(config.TEAM_NAME_DISPLAY_MAPPING_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
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
    text = str(name or "").strip()
    if not text:
        return ""
    if not config.USE_DISPLAY_NAME_MAPPING:
        return text
    return TEAM_DB_TO_DISPLAY.get(text, text)


def _team_name_for_db(name):
    """Map UI display names back to DB/canonical team names."""
    text = str(name or "").strip()
    if not text:
        return ""
    if not config.USE_DISPLAY_NAME_MAPPING:
        return text
    return TEAM_DISPLAY_TO_DB.get(text, text)


def _normalize_team_key(name):
    """Normalize team name for case-insensitive comparison."""
    return str(name or "").strip().lower()


def _to_float(value, default=0.0):
    """Convert value to float, returning default on error."""
    try:
        return float(value)
    except Exception:
        return default
