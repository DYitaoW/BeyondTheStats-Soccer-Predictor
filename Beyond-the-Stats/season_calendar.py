"""Season calendar helpers for football-data.co.uk *statYYYY.csv leagues."""
from __future__ import annotations

import re

CALENDAR_YEAR_STAT_PREFIXES = (
    "mlsstat",
    "mexstat",
    "brastat",
    "jpnstat",
    "argstat",
)
SEASON_FILE_PATTERN = re.compile(r"^(.+stat)(\d{4})\.csv$", re.IGNORECASE)


def uses_calendar_year_season(file_name: str) -> bool:
    """Return True for leagues whose season file year matches the calendar year."""
    base = str(file_name or "").lower()
    return any(base.startswith(prefix) for prefix in CALENDAR_YEAR_STAT_PREFIXES)


def is_in_progress_season(season_start_year: int, file_name: str, *, current_year: int) -> bool:
    """Return True when a season file should use the relaxed in-progress row minimum."""
    if uses_calendar_year_season(file_name):
        return season_start_year == current_year
    return season_start_year == (current_year - 1)


def season_key_for_fixture_year(competition: str, competition_latest_key: str, fixture_year: int) -> str:
    """Bump a competition season key forward when fixtures belong to a newer calendar year."""
    if not competition_latest_key or fixture_year <= 0:
        return competition_latest_key
    base = str(competition_latest_key).replace("\\", "/").split("/")[-1]
    match = SEASON_FILE_PATTERN.match(base if base.endswith(".csv") else f"{base}.csv")
    if not match:
        return competition_latest_key
    latest_year = int(match.group(2))
    if fixture_year <= latest_year:
        return competition_latest_key
    return f"{competition}/{match.group(1)}{fixture_year}"


def _year_month_from_date(value) -> tuple[int, int] | None:
    if value is None:
        return None
    if hasattr(value, "year") and hasattr(value, "month"):
        return int(value.year), int(value.month)
    text = str(value).strip()[:10]
    if len(text) < 7:
        return None
    try:
        return int(text[:4]), int(text[5:7])
    except ValueError:
        return None


def liga_mx_tournament_label_for_date(match_date) -> str | None:
    """Return the Liga MX short-tournament label for a fixture date, e.g. ``Apertura 2026``."""
    parsed = _year_month_from_date(match_date)
    if not parsed:
        return None
    year, month = parsed
    if month >= 7:
        return f"Apertura {year}"
    return f"Clausura {year}"


def active_liga_mx_tournament_label(reference_date=None) -> str:
    """Return the active Liga MX short-tournament label for today (or a reference date)."""
    from datetime import date

    ref = reference_date or date.today()
    return liga_mx_tournament_label_for_date(ref) or ""
