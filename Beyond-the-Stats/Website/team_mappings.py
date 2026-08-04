"""Team mapping diagnostics for manual ESPN / API → predictor name alignment."""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import config
from competition_rules import normalize_team_key
from espn_api import _fetch_espn_json

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import season_calendar
import team_mapping_groups as tmg

# Extra-league ESPN feeds not always present in LIVE_SCORE_COMPETITIONS.
EXTRA_ESPN_COMPETITIONS = {
    "Argentina/Primera Division": "arg.1",
    "Brazil/Brasileirão": "bra.1",
    "Japan/J1 League": "jpn.1",
}

UPCOMING_CSV_SOURCES = {
    "global": config.GLOBAL_UPCOMING_FILE,
    "mls": config.MLS_UPCOMING_FILE,
    "extra": config.EXTRA_UPCOMING_FILE,
    "cups": config.CUP_UPCOMING_FILE,
}


def _load_team_mapping_master() -> dict:
    path = config.TEAM_NAME_DISPLAY_MAPPING_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _espn_competitions_for_scan() -> list[tuple[str, str]]:
    """Return unique (competition_name, espn_id) pairs for ESPN upcoming scans."""
    mapping_keys = set(_load_team_mapping_master().keys())
    by_espn: dict[str, str] = {}
    for comp_name, espn_id in {**config.LIVE_SCORE_COMPETITIONS, **EXTRA_ESPN_COMPETITIONS}.items():
        if not espn_id:
            continue
        existing = by_espn.get(espn_id)
        if existing is None:
            by_espn[espn_id] = comp_name
            continue
        if comp_name in mapping_keys and existing not in mapping_keys:
            by_espn[espn_id] = comp_name
    return sorted(by_espn.items(), key=lambda item: item[1].lower())


def _default_league_lookahead_days() -> int:
    """Season-length ESPN scan for European leagues (Jul through May)."""
    today = date.today()
    _, end = season_calendar.european_season_bounds(today)
    return max(30, min(366, (end.date() - today).days + 1))


def _extract_upcoming_teams_from_scoreboard(data: dict) -> tuple[set[str], int]:
    """Parse ESPN scoreboard JSON; return upcoming team names and fixture count."""
    team_names: set[str] = set()
    fixture_count = 0
    for event in data.get("events") or []:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        comp0 = competitions[0] or {}
        status_state = (
            ((comp0.get("status") or {}).get("type") or {}).get("state", "")
        ).strip().lower()
        if status_state and status_state not in {"pre"}:
            continue

        home_team = ""
        away_team = ""
        for competitor in comp0.get("competitors") or []:
            team_name = str((competitor.get("team") or {}).get("displayName", "")).strip()
            side = str(competitor.get("homeAway", "")).strip().lower()
            if side == "home":
                home_team = team_name
            elif side == "away":
                away_team = team_name
        if not home_team or not away_team:
            continue

        fixture_count += 1
        team_names.add(home_team)
        team_names.add(away_team)
    return team_names, fixture_count


def _fetch_upcoming_espn_teams(competition: str, espn_id: str, lookahead_days: int) -> tuple[set[str], int]:
    """Scan ESPN scoreboards for upcoming (pre) fixtures and collect team display names."""
    today = date.today()
    is_cup = "cup" in competition.lower() or competition.startswith("Europe/") or competition.startswith("Europe/")
    if is_cup:
        _, end = season_calendar.cup_lookahead_bounds(today, lookahead_days=lookahead_days)
    elif season_calendar.competition_uses_calendar_year(competition):
        _, end = season_calendar.calendar_year_bounds(today)
    else:
        _, end = season_calendar.european_season_bounds(today)
    team_names: set[str] = set()
    fixture_count = 0

    scan_days = max(1, min(int(lookahead_days), (end.date() - today).days + 1))
    for offset in range(0, scan_days):
        day = today + timedelta(days=offset)
        if day > end.date():
            break
        url = f"{config.LIVE_SCORE_ESPN_BASE}/{espn_id}/scoreboard?dates={day.strftime('%Y%m%d')}"
        data = _fetch_espn_json(url)
        if not data:
            continue
        day_teams, day_fixtures = _extract_upcoming_teams_from_scoreboard(data)
        team_names.update(day_teams)
        fixture_count += day_fixtures

    return team_names, fixture_count


def _unmapped_from_upcoming_csv(path: str, master: dict) -> list[dict]:
    """Collect API/display team names from an upcoming predictions CSV that still need mapping."""
    if not path or not os.path.exists(path):
        return []

    rows_out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                competition = str(row.get("competition", "")).strip()
                if not competition:
                    continue
                comp_map = master.get(competition, {}) if isinstance(master.get(competition), dict) else {}
                schedule_only = str(row.get("schedule_only", "")).strip() in {"1", "true", "True"}
                for api_col, canonical_col in (
                    ("display_home_team", "home_team"),
                    ("display_away_team", "away_team"),
                ):
                    api_name = str(row.get(api_col, "")).strip()
                    canonical = str(row.get(canonical_col, "")).strip()
                    if not api_name:
                        continue
                    is_mapped, reason, mapped_to = _mapping_status(comp_map, api_name, competition, master)
                    if is_mapped and mapped_to == canonical and canonical:
                        continue
                    if schedule_only or not is_mapped:
                        rows_out.append(
                            {
                                "competition": competition,
                                "api_name": api_name,
                                "canonical_in_csv": canonical,
                                "reason": "schedule_only" if schedule_only else reason,
                                "mapped_to": mapped_to,
                                "source_file": os.path.basename(path),
                            }
                        )
    except Exception:
        return []
    return rows_out


def _dedupe_unmapped_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        key = (row.get("competition"), row.get("api_name"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return sorted(out, key=lambda item: (str(item.get("competition", "")).lower(), str(item.get("api_name", "")).lower()))


def _mapping_status(comp_map: dict, api_name: str, competition: str, master: dict) -> tuple[bool, str, str | None]:
    """Return (is_mapped, reason, mapped_to) for one API team name."""
    canonical, source = tmg.lookup_mapped_name(api_name, competition, master)
    if canonical:
        return True, f"mapped_via_{source}", canonical

    if not isinstance(comp_map, dict) or api_name not in comp_map:
        return False, "missing", None
    mapped_to = str(comp_map.get(api_name, "")).strip()
    if not mapped_to:
        return False, "blank_mapping", ""
    return True, "mapped", mapped_to


def build_unmapped_espn_payload(
    *,
    lookahead_days: int | None = None,
    competition_filter: str | None = None,
    include_prediction_csv: bool = True,
) -> dict:
    """Build payload of upcoming API/ESPN team names that still need manual mapping."""
    if lookahead_days is None:
        lookahead_days = _default_league_lookahead_days()
    lookahead_days = max(1, min(int(lookahead_days), 366))
    master = _load_team_mapping_master()
    competitions_for_scan = _espn_competitions_for_scan()
    competitions_out = []
    total_unmapped = 0

    for espn_id, competition in competitions_for_scan:
        if competition_filter and competition != competition_filter:
            continue

        comp_map = master.get(competition, {})
        comp_lookahead = lookahead_days
        if "cup" in competition.lower() or competition.startswith("Europe/") or competition.startswith("Europe/"):
            comp_lookahead = min(lookahead_days, season_calendar.DEFAULT_CUP_LOOKAHEAD_DAYS)
        espn_teams, fixture_count = _fetch_upcoming_espn_teams(competition, espn_id, comp_lookahead)
        unmapped_rows = []
        for api_name in sorted(espn_teams, key=str.lower):
            is_mapped, reason, mapped_to = _mapping_status(comp_map, api_name, competition, master)
            if is_mapped:
                continue
            unmapped_rows.append(
                {
                    "api_name": api_name,
                    "reason": reason,
                    "mapped_to": mapped_to,
                    "source": "espn",
                }
            )

        if unmapped_rows:
            total_unmapped += len(unmapped_rows)
            competitions_out.append(
                {
                    "competition": competition,
                    "espn_id": espn_id,
                    "has_mapping_section": competition in master,
                    "upcoming_fixture_count": fixture_count,
                    "unmapped_count": len(unmapped_rows),
                    "unmapped_teams": unmapped_rows,
                }
            )

    csv_unmapped: list[dict] = []
    if include_prediction_csv:
        for source_label, csv_path in UPCOMING_CSV_SOURCES.items():
            for row in _unmapped_from_upcoming_csv(csv_path, master):
                row["pipeline"] = source_label
                csv_unmapped.append(row)
        csv_unmapped = _dedupe_unmapped_rows(csv_unmapped)
        total_unmapped += len(csv_unmapped)

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "lookahead_days": lookahead_days,
        "competition_filter": competition_filter or None,
        "mapping_file": config.TEAM_NAME_DISPLAY_MAPPING_FILE,
        "mapping_instructions": (
            "Edit the JSON mapping file: each competition key maps API/ESPN display names "
            "(keys) to canonical predictor team names (values). Leave a blank string for names "
            "you have not resolved yet."
        ),
        "summary": {
            "competitions_scanned": len(competitions_for_scan),
            "competitions_with_unmapped": len(competitions_out),
            "csv_unmapped_count": len(csv_unmapped),
            "total_unmapped_teams": total_unmapped,
        },
        "competitions": competitions_out,
        "unmapped_from_prediction_csv": csv_unmapped,
    }


def _load_predictor_teams_for_mode(mode: str, predictions_mod) -> tuple[list[str], dict[str, str], str]:
    """Return sorted team list, team→competition map, and data source label."""
    pm_mod = {
        "global": predictions_mod.pm_global,
        "mls": predictions_mod.pm_mls,
        "extra": predictions_mod.pm_extra,
    }.get(mode)
    if pm_mod is None:
        return [], {}, "unknown_mode"

    if config.STATIC_PREDICTIONS:
        _, teams = predictions_mod._get_static_predictions(mode)
        if teams:
            return sorted(teams), {}, "static_predictions"
        fallback = predictions_mod._load_teams_from_team_data(pm_mod)
        return fallback, {}, "team_data_fallback"

    try:
        ctx = predictions_mod.get_context(mode)
        teams = sorted({str(team).strip() for team in ctx.available_teams if str(team).strip()})
        comp_map = {
            str(team).strip(): str(comp).strip()
            for team, comp in (ctx.team_competition_map or {}).items()
            if str(team).strip()
        }
        return teams, comp_map, "context"
    except Exception:
        fallback = predictions_mod._load_teams_from_team_data(pm_mod)
        return fallback, {}, "team_data_fallback"


def _duplicate_normalized_keys(teams: list[str]) -> list[dict]:
    """Find teams that collapse to the same normalized key within one mode."""
    buckets: dict[str, list[str]] = defaultdict(list)
    for team in teams:
        key = normalize_team_key(team)
        if not key:
            continue
        buckets[key].append(team)

    duplicates = []
    for key, names in sorted(buckets.items(), key=lambda item: item[0]):
        unique_names = sorted({name for name in names}, key=str.lower)
        if len(unique_names) > 1:
            duplicates.append({"normalized_key": key, "teams": unique_names})
    return duplicates


def _mapping_canonical_teams_by_competition(master: dict) -> dict[str, list[str]]:
    """Collect non-empty canonical predictor names from the mapping master."""
    out: dict[str, list[str]] = {}
    for competition, comp_map in master.items():
        if not isinstance(comp_map, dict):
            continue
        names = sorted(
            {
                str(canonical).strip()
                for canonical in comp_map.values()
                if str(canonical or "").strip()
            },
            key=str.lower,
        )
        if names:
            out[str(competition)] = names
    return out


def _mapping_canonical_teams_flat(master: dict) -> list[str]:
    names: set[str] = set()
    for comp_map in master.values():
        if not isinstance(comp_map, dict):
            continue
        for canonical in comp_map.values():
            text = str(canonical or "").strip()
            if text:
                names.add(text)
    return sorted(names, key=str.lower)


def build_predictor_teams_payload() -> dict:
    """Aggregate canonical predictor team names across global, MLS, and extra backends."""
    import predictions as predictions_mod

    master = _load_team_mapping_master()
    mapping_by_competition = _mapping_canonical_teams_by_competition(master)
    mapping_canonical_teams = _mapping_canonical_teams_flat(master)

    modes_out = {}
    all_teams: set[str] = set()
    mode_team_sets: dict[str, set[str]] = {}

    for mode in ("global", "mls", "extra"):
        teams, comp_map, source = _load_predictor_teams_for_mode(mode, predictions_mod)
        mode_team_sets[mode] = set(teams)
        all_teams.update(teams)
        modes_out[mode] = {
            "team_count": len(teams),
            "source": source,
            "teams": teams,
            "teams_with_competition": [
                {"team": team, "competition": comp_map.get(team, "")}
                for team in teams
            ],
            "duplicate_normalized_keys": _duplicate_normalized_keys(teams),
        }

    cross_mode_overlaps = []
    mode_keys = list(mode_team_sets.keys())
    for left_idx, left_mode in enumerate(mode_keys):
        for right_mode in mode_keys[left_idx + 1 :]:
            overlap = sorted(mode_team_sets[left_mode] & mode_team_sets[right_mode], key=str.lower)
            if overlap:
                cross_mode_overlaps.append(
                    {
                        "modes": [left_mode, right_mode],
                        "shared_teams": overlap,
                        "shared_count": len(overlap),
                    }
                )

    predictor_union = set(all_teams)
    mapping_not_in_predictor = sorted(
        [name for name in mapping_canonical_teams if name not in predictor_union],
        key=str.lower,
    )
    predictor_not_in_mapping = sorted(
        [name for name in predictor_union if name not in set(mapping_canonical_teams)],
        key=str.lower,
    )

    global_duplicates = modes_out.get("global", {}).get("duplicate_normalized_keys", [])
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "summary": {
            "global_team_count": modes_out["global"]["team_count"],
            "mls_team_count": modes_out["mls"]["team_count"],
            "extra_team_count": modes_out["extra"]["team_count"],
            "all_unique_team_count": len(all_teams),
            "mapping_canonical_team_count": len(mapping_canonical_teams),
            "mapping_canonical_not_in_predictor_count": len(mapping_not_in_predictor),
            "predictor_not_in_mapping_count": len(predictor_not_in_mapping),
            "global_normalized_duplicate_groups": len(global_duplicates),
            "cross_mode_overlap_pairs": len(cross_mode_overlaps),
        },
        "modes": modes_out,
        "mapping_canonical_teams": mapping_canonical_teams,
        "mapping_canonical_by_competition": mapping_by_competition,
        "mapping_canonical_not_in_predictor": mapping_not_in_predictor,
        "predictor_not_in_mapping": predictor_not_in_mapping,
        "cross_mode_overlaps": cross_mode_overlaps,
        "all_unique_teams": sorted(all_teams, key=str.lower),
    }
