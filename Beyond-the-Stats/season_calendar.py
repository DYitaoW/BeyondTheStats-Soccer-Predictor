"""Season calendar helpers for football-data.co.uk leagues and fixture search windows."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

import pandas as pd

FixtureWindowKind = Literal["european", "calendar_year", "cup"]
DEFAULT_CUP_LOOKAHEAD_DAYS = 180

# Competitions that run Jan–Dec on a single calendar-year file (*statYYYY).
CALENDAR_YEAR_COMPETITION_PREFIXES = (
    "United States/",
    "Mexico/",
    "Brazil/",
    "Japan/",
    "Argentina/",
)

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
    ref = reference_date or date.today()
    return liga_mx_tournament_label_for_date(ref) or ""


def _as_timestamp(value) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp(date.today())
    if isinstance(value, pd.Timestamp):
        return value.normalize()
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return pd.Timestamp(value).normalize()
    return pd.Timestamp(str(value)[:10]).normalize()


def competition_uses_calendar_year(competition_name: str) -> bool:
    """Return True for MLS, Liga MX, Brazil, J1, Argentina style leagues."""
    comp = str(competition_name or "").strip()
    return any(comp.startswith(prefix) for prefix in CALENDAR_YEAR_COMPETITION_PREFIXES)


def fixture_window_kind(competition_name: str, *, is_cup: bool = False) -> FixtureWindowKind:
    if is_cup:
        return "cup"
    if competition_uses_calendar_year(competition_name):
        return "calendar_year"
    return "european"


def european_season_bounds(reference_date=None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Jul 1 of the active European season through May 31 of the end year."""
    ref = _as_timestamp(reference_date)
    if ref.month >= 7:
        start_year = ref.year
    else:
        start_year = ref.year - 1
    end_year = start_year + 1
    start = pd.Timestamp(year=start_year, month=7, day=1)
    end = pd.Timestamp(year=end_year, month=5, day=31)
    return start, end


def calendar_year_bounds(reference_date=None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Jan 1 through Dec 31 of the reference calendar year."""
    ref = _as_timestamp(reference_date)
    start = pd.Timestamp(year=ref.year, month=1, day=1)
    end = pd.Timestamp(year=ref.year, month=12, day=31)
    return start, end


def cup_lookahead_bounds(
    reference_date=None,
    *,
    lookahead_days: int = DEFAULT_CUP_LOOKAHEAD_DAYS,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Rolling cup window from today through *lookahead_days* ahead."""
    ref = _as_timestamp(reference_date)
    days = max(1, int(lookahead_days))
    return ref, ref + pd.Timedelta(days=days)


def fixture_search_bounds(
    competition_name: str,
    reference_date=None,
    *,
    is_cup: bool = False,
    cup_lookahead_days: int = DEFAULT_CUP_LOOKAHEAD_DAYS,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return inclusive (start, end) fixture search bounds for a competition."""
    kind = fixture_window_kind(competition_name, is_cup=is_cup)
    if kind == "cup":
        return cup_lookahead_bounds(reference_date, lookahead_days=cup_lookahead_days)
    if kind == "calendar_year":
        return calendar_year_bounds(reference_date)
    return european_season_bounds(reference_date)


def espn_scan_day_count(
    competition_name: str,
    reference_date=None,
    *,
    is_cup: bool = False,
    cup_lookahead_days: int = DEFAULT_CUP_LOOKAHEAD_DAYS,
) -> int:
    """Number of ESPN scoreboard days to scan for one competition."""
    ref = _as_timestamp(reference_date)
    _, end = fixture_search_bounds(
        competition_name,
        reference_date=ref,
        is_cup=is_cup,
        cup_lookahead_days=cup_lookahead_days,
    )
    return max(1, int((end - ref).days) + 1)


def football_data_api_date_params(
    competition_name: str,
    reference_date=None,
    *,
    is_cup: bool = False,
    cup_lookahead_days: int = DEFAULT_CUP_LOOKAHEAD_DAYS,
) -> dict[str, str]:
    """Build football-data.org ``dateFrom`` / ``dateTo`` query params."""
    start, end = fixture_search_bounds(
        competition_name,
        reference_date=reference_date,
        is_cup=is_cup,
        cup_lookahead_days=cup_lookahead_days,
    )
    return {
        "dateFrom": start.strftime("%Y-%m-%d"),
        "dateTo": end.strftime("%Y-%m-%d"),
    }


def filter_fixtures_to_bounds(
    fixtures,
    competition_name: str,
    reference_date=None,
    *,
    is_cup: bool = False,
    cup_lookahead_days: int = DEFAULT_CUP_LOOKAHEAD_DAYS,
    date_column: str = "match_date",
):
    """Filter a fixture frame to the competition's season search window."""
    if fixtures is None:
        return fixtures
    try:
        import pandas as _pd

        if getattr(fixtures, "empty", True):
            return fixtures
    except Exception:
        return fixtures

    ref = _as_timestamp(reference_date)
    start, end = fixture_search_bounds(
        competition_name,
        reference_date=ref,
        is_cup=is_cup,
        cup_lookahead_days=cup_lookahead_days,
    )
    frame = fixtures.copy()
    frame[date_column] = pd.to_datetime(frame[date_column], errors="coerce").dt.normalize()
    lower = max(ref, start)
    return frame[
        frame[date_column].notna()
        & (frame[date_column] >= lower)
        & (frame[date_column] <= end)
    ].reset_index(drop=True)
