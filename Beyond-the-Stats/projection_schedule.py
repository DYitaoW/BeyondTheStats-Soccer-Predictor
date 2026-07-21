"""Shared fixture-schedule helpers for league table projections.

When the current-season results CSV is missing (preseason PATH B), projections
synthesize remaining fixtures from the roster instead of relying on ESPN
upcoming scoreboards.

Default league format: double round-robin — each club plays every other club
home and away → ``(n - 1) * 2`` games per team (e.g. Premier League 38).

Special formats override that default (MLS conferences, Liga MX short
tournament, Scottish three-round first phase, etc.).
"""
from __future__ import annotations


# Matches Website/competition_rules.H2H_TIEBREAKER_COMPETITIONS (kept local so
# pipeline scripts do not need the Website package on PYTHONPATH).
H2H_TIEBREAKER_COMPETITIONS = frozenset({
    "Spain/La Liga",
    "Spain/La Liga 2",
    "Italy/Serie A",
    "Italy/Serie B",
    "Portugal/Liga Portugal",
    "Belgium/First Division A",
    "Turkey/Super Lig",
    "Mexico/Liga MX",
})


def uses_h2h_tiebreaker(competition: str) -> bool:
    return str(competition or "").strip() in H2H_TIEBREAKER_COMPETITIONS


def prefer_current_season_csv(csv_start_year, expected_year) -> bool:
    """True only when the on-disk CSV is the expected current season (PATH A).

    Any prior-season file (or unknown year) must use PATH B so finished
    results do not block synthetic remaining fixtures.
    """
    if csv_start_year is None or expected_year is None:
        return False
    try:
        return int(csv_start_year) == int(expected_year)
    except (TypeError, ValueError):
        return False


def schedule_meetings_per_pair(competition: str) -> int:
    """How many times each unordered pair should meet in the regular slate.

    2 = classic home-and-away double round-robin (default).
    1 = single round-robin (Liga MX short tournament).
    3 = Scottish Premiership first phase (33 games for 12 clubs).
    """
    comp = str(competition or "").strip()
    if comp.startswith("United States/MLS"):
        # MLS uses a conference-aware builder — callers should not use RR.
        return 0
    if comp == "Mexico/Liga MX" or comp.startswith("Mexico/"):
        return 1
    if "Scotland" in comp or "Scottish" in comp:
        return 3
    # Belgian / everyone else: full home-and-away regular season.
    return 2


def expected_games_per_team(competition: str, n_teams: int) -> int:
    """Games each team should play in the synthesized regular-season slate."""
    n = max(0, int(n_teams or 0))
    if n <= 1:
        return 0
    meetings = schedule_meetings_per_pair(competition)
    if meetings <= 0:
        return 0
    return (n - 1) * meetings


def build_round_robin_fixtures(teams: list[str], meetings_per_pair: int = 2) -> list[tuple[str, str]]:
    """Return directed (home, away) fixtures for a round-robin slate.

    ``meetings_per_pair=2`` → every ordered pair once (home + away).
    ``meetings_per_pair=1`` → each unordered pair once (home assigned stably).
    ``meetings_per_pair=3`` → double RR plus one extra single cycle (Scottish).
    """
    clubs = [str(t).strip() for t in (teams or []) if str(t).strip()]
    if len(clubs) < 2:
        return []
    meetings = max(1, int(meetings_per_pair or 1))
    pairs: list[tuple[str, str]] = []

    # Always include full home-and-away when at least two meetings are required.
    if meetings >= 2:
        for home in clubs:
            for away in clubs:
                if home != away:
                    pairs.append((home, away))
        extra_singles = meetings - 2
    else:
        extra_singles = 1

    for round_idx in range(extra_singles):
        for i, a in enumerate(clubs):
            for j, b in enumerate(clubs):
                if i >= j:
                    continue
                # Stable home assignment that flips across extra rounds.
                if (i + j + round_idx) % 2 == 0:
                    pairs.append((a, b))
                else:
                    pairs.append((b, a))

    return pairs


def build_fixtures_for_competition(competition: str, teams: list[str]) -> list[tuple[str, str]]:
    """Format-aware fixture list for PATH B / gap-fill (excluding MLS)."""
    meetings = schedule_meetings_per_pair(competition)
    if meetings <= 0:
        return []
    return build_round_robin_fixtures(teams, meetings_per_pair=meetings)


def fill_missing_fixtures(
    competition: str,
    teams: list[str],
    seen_pairs: set,
    future_pairs: list,
    future_dates: list | None = None,
) -> int:
    """Append unseen directed fixtures for *competition* into *future_pairs*.

    Returns the number of fixtures added.
    """
    added = 0
    for home, away in build_fixtures_for_competition(competition, teams):
        if (home, away) in seen_pairs:
            continue
        seen_pairs.add((home, away))
        future_pairs.append((home, away))
        if future_dates is not None:
            future_dates.append("")
        added += 1
    return added
