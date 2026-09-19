from __future__ import annotations

from pathlib import Path

import os as _os_paths_setup
import sys as _sys_paths_setup
_FILES_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.abspath(__file__))
_REGION_DIR = _os_paths_setup.path.dirname(_FILES_DIR)
_SP_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.dirname(_REGION_DIR))
if _SP_DIR not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, _SP_DIR)
from shared import paths as _bts_paths
if str(_bts_paths.SHARED_DIR) not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, str(_bts_paths.SHARED_DIR))
BASE_DIR = Path(str(_bts_paths.SP_DIR))
PREDICTIONS_DIR = str(_bts_paths.OUTPUT_PRED_EUROPE)
PROJECT_DIR = str(_bts_paths.SP_DIR)

"""Build historical season tables from existing processed data.

Reads ``season_teams.json`` (produced by ``Sort_Data.py``) and computes
real final standings for the last 3-5 *completed* seasons of each league.
The output ``historical_tables.json`` is consumed by
``Predict_Match.blend_with_historical_prior`` to temper overconfident
early-season predictions. It also stores a per-country, per-team 3-season
history of league tier + final position (with ``tier: -1`` for a team that
dropped into / came up from a league this program does not track) so
promotion/relegation logic does not recompute that history on the fly.

This script is league-agnostic; the same file is copied into
``pipelines/mls/files/`` and ``pipelines/extra/files/``.
"""



import hashlib
import json
import os
import sys
from datetime import datetime, timezone
TEAM_DATA_DIR = BASE_DIR / "Data" / "Team_Data"
OUTPUT_FILE = TEAM_DATA_DIR / "historical_tables.json"

MAX_COMPLETED_SEASONS = 5

MIN_GAMES_FOR_COMPLETED = 20

HISTORY_SEASONS = 3

# League tier per competition key (int): 1 = top flight, 2 = second flight, ...
# Covers every league tracked by the global, MLS and extra sub-pipelines so the
# copies stay identical. A tracked competition missing from this map gets tier
# 0 rather than being treated as demoted.
LEAGUE_TIERS = {
    "England/Premier League": 1,
    "England/Championship": 2,
    "Spain/La Liga": 1,
    "Spain/La Liga 2": 2,
    "Italy/Serie A": 1,
    "Italy/Serie B": 2,
    "Germany/Bundesliga": 1,
    "Germany/Bundesliga 2": 2,
    "France/Ligue 1": 1,
    "France/Ligue 2": 2,
    "Portugal/Liga Portugal": 1,
    "Netherlands/Eredivisie": 1,
    "Belgium/First Division A": 1,
    "Scotland/Premiership": 1,
    "Turkey/Super Lig": 1,
    "Austria/Bundesliga": 1,
    "Greece/Super League": 1,
    "Norway/Eliteserien": 1,
    "Romania/Liga I": 1,
    "Sweden/Allsvenskan": 1,
    "Poland/Ekstraklasa": 1,
    "United States/MLS": 1,
    "Mexico/Liga MX": 1,
    "Argentina/Primera Division": 1,
    "Belarus/Premier League": 1,
    "Brazil/Brasileirão": 1,
    "Bulgaria/First League": 1,
    "Cyprus/First Division": 1,
    "Czech Republic/First League": 1,
    "Denmark/Danish Superliga": 1,
    "Israel/Premier League": 1,
    "Japan/J1 League": 1,
    "Moldova/Super Liga": 1,
    "Serbia/SuperLiga": 1,
}


def _parse_start_year(season_key: str) -> int:
    """Extract the start year from a season key like 'England\\Premier League\\premstat2024-25'."""
    filename = season_key.rsplit("\\", 1)[-1] if "\\" in season_key else season_key.rsplit("/", 1)[-1]
    filename = filename.replace(".csv", "")
    for part in filename.split("-"):
        digits = "".join(c for c in part if c.isdigit())
        if len(digits) == 4:
            return int(digits)
    for part in filename.split("_"):
        digits = "".join(c for c in part if c.isdigit())
        if len(digits) == 4:
            return int(digits)
    digits = "".join(c for c in filename if c.isdigit())
    if len(digits) >= 4:
        return int(digits[:4])
    return 0


def _competition_from_key(season_key: str) -> str:
    """Return the competition prefix (everything before the filename)."""
    if "\\" in season_key:
        parts = season_key.rsplit("\\", 1)
    elif "/" in season_key:
        parts = season_key.rsplit("/", 1)
    else:
        return "Unknown"
    return parts[0]


def _country_of(competition) -> str:
    """Return the country prefix of a competition key, e.g. 'England\\Premier League' -> 'England'."""
    return str(competition or "Unknown").replace("\\", "/").split("/", 1)[0]


def _season_teams_signature(season_teams: dict) -> str:
    """Fingerprint the season keys present, so the output can detect when a
    new season (or league) has arrived and all prior history must be rebuilt."""
    digest = hashlib.sha1()
    for key in sorted(k for k in season_teams if isinstance(k, str)):
        digest.update(key.encode("utf-8", errors="replace"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _compute_standings(teams_dict: dict) -> list[dict]:
    """Rank teams by points → GD → GF → name (ascending name breaks ties)."""
    def _num(value):
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        if result != result:  # NaN
            return 0.0
        return result

    rows = []
    for team, stats in teams_dict.items():
        if not isinstance(stats, dict):
            continue
        games = int(_num(stats.get("games")) or 0)
        if games == 0:
            continue
        pts = int(_num(stats.get("points")) or 0)
        gf = int(_num(stats.get("goals_scored")) or 0)
        ga = int(_num(stats.get("goals_conceded")) or 0)
        rows.append({
            "team": team,
            "points": pts,
            "gf": gf,
            "ga": ga,
            "gd": gf - ga,
            "games": games,
        })
    rows.sort(key=lambda r: (-r["points"], -r["gd"], -r["gf"], r["team"]))
    for idx, row in enumerate(rows, start=1):
        row["position"] = idx
    return rows


def build_historical_tables(season_teams: dict | None = None) -> dict:
    if season_teams is None:
        season_teams_path = TEAM_DATA_DIR / "season_teams.json"
        if not season_teams_path.exists():
            print(f"[build-historical-tables] season_teams.json not found at {season_teams_path}")
            return {}
        with open(season_teams_path, encoding="utf-8-sig") as fh:
            season_teams = json.load(fh)

    competitions: dict[str, list[dict]] = {}
    for season_key, teams in season_teams.items():
        if not isinstance(teams, dict):
            continue
        comp = _competition_from_key(season_key)
        start_year = _parse_start_year(season_key)
        competitions.setdefault(comp, []).append((start_year, season_key, teams))

    output: dict[str, dict] = {}
    for comp, seasons in competitions.items():
        seasons.sort(key=lambda s: s[0])
        if not seasons:
            continue

        latest_start = seasons[-1][0]
        latest_key = seasons[-1][1]
        latest_teams = seasons[-1][2]
        latest_max_games = max(
            (int(t.get("games", 0) or 0) for t in latest_teams.values() if isinstance(t, dict)),
            default=0,
        )

        completed = []
        for start_year, skey, teams in seasons[:-1]:
            standings = _compute_standings(teams)
            if not standings:
                continue
            completed.append({
                "season_key": skey,
                "start_year": start_year,
                "standings": standings,
            })

        completed = completed[-MAX_COMPLETED_SEASONS:]

        current_games_per_team = latest_max_games
        output[comp] = {
            "current_season": latest_key,
            "current_start_year": latest_start,
            "current_games_per_team": current_games_per_team,
            "completed_seasons": completed,
        }

    return output


def _team_prior_snapshot(competitions: dict) -> dict[str, dict]:
    """Build a global team → {competition, position, points, games, start_year} map
    from the *most recent completed season* per competition."""
    snapshot: dict[str, dict] = {}
    for comp, data in competitions.items():
        completed = data.get("completed_seasons") or []
        if not completed:
            continue
        latest = completed[-1]
        for entry in latest.get("standings") or []:
            team = entry.get("team", "")
            if team:
                snapshot[team] = {
                    "competition": comp,
                    "season_key": latest.get("season_key", ""),
                    "start_year": latest.get("start_year", 0),
                    "position": entry.get("position", 0),
                    "points": entry.get("points", 0),
                    "games": entry.get("games", 0),
                }
    return snapshot


def _build_team_history(competitions: dict) -> dict[str, dict]:
    """Per-country, per-team (tier, position) history for the last 3 completed seasons.

    Keyed by country (e.g. ``'England'``). Each country holds ``seed_season``
    (the in-progress season start year that drives when the window rolls over)
    and ``seasons`` — a list of the most recent ``HISTORY_SEASONS`` completed
    seasons, newest last. Every team ever seen in that country appears in every
    season: teams that played a tracked league that season carry
    ``{competition, tier, position, points, games, season_key}``; teams absent
    from every tracked league that season carry ``{tier: -1, position: 0}``,
    meaning they were in (or came from) a league this program does not track.
    """
    by_country: dict[str, dict] = {}
    for comp, data in competitions.items():
        comp_key = str(comp).replace("\\", "/")
        country = _country_of(comp_key)
        tier = LEAGUE_TIERS.get(comp_key, 0)
        bucket = by_country.setdefault(country, {"seasons": {}, "teams": set(), "seed_season": 0})
        seed = int(data.get("current_start_year", 0) or 0)
        if seed > bucket["seed_season"]:
            bucket["seed_season"] = seed
        for season in data.get("completed_seasons") or []:
            start_year = int(season.get("start_year", 0) or 0)
            records = bucket["seasons"].setdefault(start_year, {})
            for entry in season.get("standings") or []:
                team = entry.get("team", "")
                if not team:
                    continue
                bucket["teams"].add(team)
                records[team] = {
                    "competition": comp,
                    "tier": tier,
                    "season_key": season.get("season_key", ""),
                    "position": entry.get("position", 0),
                    "points": entry.get("points", 0),
                    "games": entry.get("games", 0),
                }

    team_history: dict[str, dict] = {}
    for country, bucket in by_country.items():
        season_years = sorted(bucket["seasons"].keys())
        recent_years = season_years[-HISTORY_SEASONS:]
        seasons_out = []
        for year in recent_years:
            records = dict(bucket["seasons"][year])
            for team in bucket["teams"]:
                if team not in records:
                    records[team] = {"tier": -1, "position": 0, "points": 0, "games": 0}
            seasons_out.append({"start_year": year, "records": records})
        team_history[country] = {
            "seed_season": bucket["seed_season"] or (recent_years[-1] if recent_years else 0),
            "seasons": seasons_out,
        }
    return team_history


def main() -> None:
    season_teams_path = TEAM_DATA_DIR / "season_teams.json"
    if season_teams_path.exists():
        with open(season_teams_path, encoding="utf-8-sig") as fh:
            season_teams = json.load(fh)
    else:
        season_teams = {}

    tables = build_historical_tables(season_teams)
    team_prior = _team_prior_snapshot(tables)
    team_history = _build_team_history(tables)
    output = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "generated_season_signature": _season_teams_signature(season_teams),
        "league_tiers": {c: t for c, t in LEAGUE_TIERS.items() if _country_of(c) in team_history},
        "competitions": tables,
        "team_prior": team_prior,
        "team_history": team_history,
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)
    n_comps = len(tables)
    n_completed = sum(len(d.get("completed_seasons", [])) for d in tables.values())
    n_teams = len(team_prior)
    n_history = sum(len(d.get("seasons", [])) for d in team_history.values())
    print(f"[build-historical-tables] wrote {OUTPUT_FILE.name}: {n_comps} competitions, {n_completed} completed seasons, {n_teams} team priors, {len(team_history)} countries / {n_history} history seasons")


if __name__ == "__main__":
    main()
