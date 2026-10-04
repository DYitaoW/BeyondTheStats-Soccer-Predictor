"""
Feature engineering module for team fixture congestion and rest metrics.
Calculates days of rest, recent match frequency, and fatigue penalties.
"""
from __future__ import annotations

import os
from datetime import datetime
from collections import defaultdict
import pandas as pd

_REST_DAYS_CACHE: dict[str, dict[str, list[datetime]]] = {}


def build_team_schedule_index(matches_df: pd.DataFrame) -> dict[str, list[datetime]]:
    """Build a mapping of team -> sorted list of match datetimes."""
    schedule = defaultdict(list)
    if matches_df.empty or "Date" not in matches_df.columns:
        return schedule

    df = matches_df.copy()
    df["ParsedDate"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df[df["ParsedDate"].notna()]

    for _, row in df.iterrows():
        dt = row["ParsedDate"]
        home = str(row.get("HomeTeam", "")).strip()
        away = str(row.get("AwayTeam", "")).strip()
        if home:
            schedule[home].append(dt)
        if away:
            schedule[away].append(dt)

    for team in schedule:
        schedule[team].sort()
    return dict(schedule)


def calculate_team_rest_and_congestion(
    team: str,
    match_date: datetime,
    team_schedule: dict[str, list[datetime]],
    lookback_days: int = 14,
) -> dict[str, float]:
    """Calculate rest days and fixture congestion for a team at a specific date.

    Returns:
        days_rest: Days since the team's last played match (capped at 14, min 1).
        matches_last_14d: Count of matches played in the preceding 14 days.
        fatigue_index: Continuous score (0.0 = fully rested, 1.0 = severe congestion).
    """
    dates = team_schedule.get(team, [])
    if not dates:
        return {
            "days_rest": 7.0,
            "matches_last_14d": 1.0,
            "fatigue_index": 0.0,
        }

    past_dates = [d for d in dates if d < match_date]
    if not past_dates:
        return {
            "days_rest": 7.0,
            "matches_last_14d": 1.0,
            "fatigue_index": 0.0,
        }

    last_match = past_dates[-1]
    delta_days = max(1.0, (match_date - last_match).total_seconds() / 86400.0)
    days_rest = min(14.0, delta_days)

    cutoff = match_date - pd.Timedelta(days=lookback_days)
    matches_in_window = sum(1 for d in past_dates if d >= cutoff)

    # Fatigue index scales with short rest (<= 3 days) and high frequency (>= 3 matches in 14 days)
    rest_penalty = max(0.0, (4.0 - days_rest) / 4.0) if days_rest < 4.0 else 0.0
    congestion_penalty = max(0.0, (matches_in_window - 2) / 3.0)
    fatigue_index = min(1.0, round(rest_penalty * 0.6 + congestion_penalty * 0.4, 4))

    return {
        "days_rest": round(days_rest, 1),
        "matches_last_14d": float(matches_in_window),
        "fatigue_index": fatigue_index,
    }
