"""Competition-specific rules for real standings and cup tables."""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict, deque

import pandas as pd

import config

KNOCKOUT_ROUND_RE = re.compile(
    r"(round of \d+|last \d+|quarter.?final|semi.?final|third place|"
    r"knockout|play-?off|final(?!\s+group)|\bround\b|ro\d+)",
    re.IGNORECASE,
)
GROUP_ROUND_RE = re.compile(
    r"(group\s*stage|group-stage|group\s+[a-z0-9]+|league\s*phase)",
    re.IGNORECASE,
)
GROUP_LABEL_RE = re.compile(r"Group\s+([A-Z0-9]+)", re.IGNORECASE)

MLS_TABLE_VIEWS = {
    "United States/MLS - Eastern Conference": ("United States/MLS", "east"),
    "United States/MLS - Western Conference": ("United States/MLS", "west"),
    "United States/MLS - Supporters Shield Table": ("United States/MLS", "shield"),
}

MLS_EASTERN_CONFERENCE_TEAMS = frozenset({
    "Atlanta Utd", "CF Montreal", "Charlotte", "Chicago Fire", "Columbus Crew",
    "DC United", "FC Cincinnati", "Inter Miami", "Nashville SC", "New England Revolution",
    "New York City", "New York Red Bulls", "Orlando City", "Philadelphia Union", "Toronto FC",
})

MLS_WESTERN_CONFERENCE_TEAMS = frozenset({
    "Austin FC", "Colorado Rapids", "FC Dallas", "Houston Dynamo", "Los Angeles Galaxy",
    "Los Angeles FC", "Minnesota United", "Portland Timbers", "Real Salt Lake", "San Diego FC",
    "San Jose Earthquakes", "Seattle Sounders", "Sporting Kansas City", "St. Louis City",
    "Vancouver Whitecaps",
})

NATIONAL_MATCHES_CSV = os.path.join(
    config.PROJECT_DIR, "Data", "National_Team_Data", "national_team_recent_matches_raw.csv"
)
WORLD_CUP_PROJECTION_FILE = os.path.join(
    config.PROJECT_DIR, "Data", "Predictions", "world_cup_projection.json"
)

_mls_name_cache: dict[str, str] | None = None
_wc_group_cache: dict[str, str] | None = None


def resolve_competition_query(comp_name: str) -> tuple[str, str | None]:
    """Map API competition aliases to base competition + optional view."""
    comp_name = str(comp_name or "").strip()
    if comp_name in MLS_TABLE_VIEWS:
        base, view = MLS_TABLE_VIEWS[comp_name]
        return base, view
    return comp_name, None


def normalize_team_key(name: str) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "", text)
    for token in ("fc", "cf", "sc", "united", "city", "club"):
        text = text.replace(token, "")
    return text


def _load_mls_canonical_names() -> dict[str, str]:
    global _mls_name_cache
    if _mls_name_cache is not None:
        return _mls_name_cache
    mapping: dict[str, str] = {}
    path = config.TEAM_NAME_DISPLAY_MAPPING_FILE
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            comp_map = payload.get("United States/MLS", {}) if isinstance(payload, dict) else {}
            if isinstance(comp_map, dict):
                for raw_name, canonical in comp_map.items():
                    raw = str(raw_name or "").strip()
                    canon = str(canonical or "").strip() or raw
                    if raw:
                        mapping[normalize_team_key(raw)] = canon
        except Exception:
            pass
    _mls_name_cache = mapping
    return mapping


def canonical_team_name(name: str, competition: str = "") -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    if competition == "United States/MLS" or competition.startswith("United States/MLS"):
        mapped = _load_mls_canonical_names().get(normalize_team_key(text))
        if mapped:
            return mapped
    return text


def mls_conference(team_name: str) -> str | None:
    canon = canonical_team_name(team_name, "United States/MLS")
    key = normalize_team_key(canon or team_name)
    for team in MLS_EASTERN_CONFERENCE_TEAMS:
        if normalize_team_key(team) in key or key in normalize_team_key(team):
            return "east"
    for team in MLS_WESTERN_CONFERENCE_TEAMS:
        if normalize_team_key(team) in key or key in normalize_team_key(team):
            return "west"
    return None


def cup_format(comp_name: str) -> dict | None:
    fmt = config._CUP_FORMATS.get(comp_name)
    if fmt:
        return fmt
    if comp_name.startswith("Europe/"):
        uefa_key = "UEFA/" + comp_name.split("/", 1)[1]
        return config._CUP_FORMATS.get(uefa_key)
    return None


def _game_match_key(game: dict) -> tuple:
    return (
        str(game.get("competition", "")).strip().lower(),
        str(game.get("match_date", "")).strip()[:10],
        normalize_team_key(game.get("home_team")),
        normalize_team_key(game.get("away_team")),
    )


def _append_game(games: list[dict], seen: set, game: dict) -> None:
    if not game:
        return
    home = str(game.get("home_team", "")).strip()
    away = str(game.get("away_team", "")).strip()
    if not home or not away:
        return
    hs = game.get("home_score")
    aws = game.get("away_score")
    if hs is None or aws is None:
        return
    try:
        hs_i = int(float(hs))
        aws_i = int(float(aws))
    except (TypeError, ValueError):
        return
    key = _game_match_key(game)
    if key in seen:
        return
    seen.add(key)
    entry = dict(game)
    entry["home_team"] = home
    entry["away_team"] = away
    entry["home_score"] = hs_i
    entry["away_score"] = aws_i
    entry.setdefault("status", "post")
    games.append(entry)


def _csv_settled_games(comp_name: str) -> list[dict]:
    rows = []
    csv_paths = [
        config.GLOBAL_UPCOMING_FILE,
        config.MLS_UPCOMING_FILE,
        config.EXTRA_UPCOMING_FILE,
        config.CUP_UPCOMING_FILE,
        config.NATIONAL_UPCOMING_FILE,
        config.CUP_COMPLETED_FILE,
        config.ALL_UPCOMING_FILE,
        os.path.join(config.PROJECT_DIR, "Output", "Upcoming", "all_upcoming.csv"),
    ]
    for path in csv_paths:
        if not path or not os.path.exists(path):
            continue
        try:
            frame = pd.read_csv(path, dtype=str)
        except Exception:
            continue
        if frame.empty or "competition" not in frame.columns:
            continue
        mask = frame["competition"].astype(str).str.strip() == comp_name
        sub = frame[mask]
        for _, row in sub.iterrows():
            actual = str(row.get("actual_result", "")).strip().upper()
            if actual not in {"H", "D", "A"}:
                continue
            try:
                hs = int(float(row.get("actual_home_goals")))
                aws = int(float(row.get("actual_away_goals")))
            except (TypeError, ValueError):
                continue
            rows.append({
                "competition": comp_name,
                "home_team": str(row.get("home_team", "")).strip(),
                "away_team": str(row.get("away_team", "")).strip(),
                "home_score": hs,
                "away_score": aws,
                "status": "post",
                "round": str(row.get("round", "") or row.get("stage", "")).strip(),
                "stage": str(row.get("stage", "")).strip(),
                "group": str(row.get("group", "")).strip(),
                "match_date": str(row.get("match_date", "")).strip()[:10],
                "match_datetime_utc": str(row.get("match_datetime_utc", "")).strip(),
                "source": "csv",
            })
    return rows


def _past_games(comp_name: str) -> list[dict]:
    rows = []
    if not os.path.exists(config.PAST_GAMES_FILE):
        return rows
    try:
        with open(config.PAST_GAMES_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return rows
    if not isinstance(payload, list):
        return rows
    for g in payload:
        if str(g.get("competition", "")).strip() != comp_name:
            continue
        try:
            hs = int(float(g.get("actual_home_goals") or g.get("home_score")))
            aws = int(float(g.get("actual_away_goals") or g.get("away_score")))
        except (TypeError, ValueError):
            continue
        rows.append({
            "competition": comp_name,
            "home_team": str(g.get("home_team", "")).strip(),
            "away_team": str(g.get("away_team", "")).strip(),
            "home_score": hs,
            "away_score": aws,
            "status": "post",
            "round": str(g.get("round", "") or g.get("stage", "")).strip(),
            "stage": str(g.get("stage", "")).strip(),
            "group": str(g.get("group", "")).strip(),
            "match_date": str(g.get("match_date", "")).strip()[:10],
            "match_datetime_utc": str(g.get("match_datetime_utc", "")).strip(),
            "source": "past_games",
        })
    return rows


def _national_csv_games(comp_name: str) -> list[dict]:
    rows = []
    if not os.path.exists(NATIONAL_MATCHES_CSV):
        return rows
    try:
        frame = pd.read_csv(NATIONAL_MATCHES_CSV, dtype=str)
    except Exception:
        return rows
    if frame.empty or "competition" not in frame.columns:
        return rows
    mask = frame["competition"].astype(str).str.strip() == comp_name
    sub = frame[mask]
    for _, row in sub.iterrows():
        status = str(row.get("status", "")).upper()
        if "FULL_TIME" not in status and "FINAL" not in status:
            continue
        try:
            hs = int(float(row.get("FTHG")))
            aws = int(float(row.get("FTAG")))
        except (TypeError, ValueError):
            continue
        stage = str(row.get("stage", "")).strip()
        rows.append({
            "competition": comp_name,
            "home_team": str(row.get("home_team", "")).strip(),
            "away_team": str(row.get("away_team", "")).strip(),
            "home_score": hs,
            "away_score": aws,
            "status": "post",
            "round": stage,
            "stage": stage,
            "match_date": str(row.get("match_date", "")).strip()[:10],
            "match_datetime_utc": str(row.get("match_datetime_utc", "")).strip(),
            "source": "national_csv",
        })
    return rows


def _world_cup_projection_games(comp_name: str) -> list[dict]:
    if comp_name != "FIFA/World Cup" or not os.path.exists(WORLD_CUP_PROJECTION_FILE):
        return []
    rows = []
    try:
        with open(WORLD_CUP_PROJECTION_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return rows
    fixtures = payload.get("group_fixtures") or []
    if not isinstance(fixtures, list):
        return rows
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        actual = str(fixture.get("actual_result", "")).strip().upper()
        if actual not in {"H", "D", "A"}:
            continue
        try:
            hs = int(float(fixture.get("home_goals", fixture.get("actual_home_goals"))))
            aws = int(float(fixture.get("away_goals", fixture.get("actual_away_goals"))))
        except (TypeError, ValueError):
            continue
        group = str(fixture.get("group", "")).strip()
        rows.append({
            "competition": comp_name,
            "home_team": str(fixture.get("home_team", "")).strip(),
            "away_team": str(fixture.get("away_team", "")).strip(),
            "home_score": hs,
            "away_score": aws,
            "status": "post",
            "round": f"Group Stage - Group {group}" if group else "Group Stage",
            "stage": "group-stage",
            "group": group,
            "match_date": str(fixture.get("match_date", "")).strip()[:10],
            "match_datetime_utc": str(fixture.get("match_datetime_utc", "")).strip(),
            "source": "wc_projection",
        })
    return rows


def infer_groups_from_games(games: list[dict]) -> dict[str, str]:
    """Infer team -> group labels from completed fixtures (4-team components)."""
    graph = defaultdict(set)
    for game in games:
        home = str(game.get("home_team", "")).strip()
        away = str(game.get("away_team", "")).strip()
        if not home or not away:
            continue
        graph[home].add(away)
        graph[away].add(home)

    components = []
    seen = set()
    for team in sorted(graph):
        if team in seen:
            continue
        queue = deque([team])
        seen.add(team)
        component = []
        while queue:
            current = queue.popleft()
            component.append(current)
            for other in graph[current]:
                if other not in seen:
                    seen.add(other)
                    queue.append(other)
        components.append(sorted(component))

    labels = list("ABCDEFGHIJKL")
    team_to_group: dict[str, str] = {}
    group_idx = 0
    for component in sorted(components, key=lambda c: (len(c), c[0] if c else "")):
        if len(component) < 3:
            continue
        if group_idx >= len(labels):
            break
        label = labels[group_idx]
        group_idx += 1
        for team in component:
            team_to_group[team] = label
    return team_to_group


def load_wc_team_groups(games: list[dict] | None = None) -> dict[str, str]:
    """Return canonical team name -> group label (A-L)."""
    global _wc_group_cache
    if _wc_group_cache is not None:
        return _wc_group_cache

    team_to_group: dict[str, str] = {}
    if os.path.exists(WORLD_CUP_PROJECTION_FILE):
        try:
            with open(WORLD_CUP_PROJECTION_FILE, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            for group_entry in payload.get("group_tables", []) or []:
                label = str(group_entry.get("group", "")).strip().upper()
                if not label:
                    continue
                for team_row in group_entry.get("teams", []) or []:
                    team = str(team_row.get("team", "")).strip()
                    if team:
                        team_to_group[team] = label
            fixtures = payload.get("group_fixtures") or []
            graph = defaultdict(set)
            for fixture in fixtures:
                if not isinstance(fixture, dict):
                    continue
                home = str(fixture.get("home_team", "")).strip()
                away = str(fixture.get("away_team", "")).strip()
                group = str(fixture.get("group", "")).strip().upper()
                if home and group:
                    team_to_group.setdefault(home, group)
                if away and group:
                    team_to_group.setdefault(away, group)
                if home and away:
                    graph[home].add(away)
                    graph[away].add(home)
            if not team_to_group and graph:
                labels = list("ABCDEFGHIJKL")
                components = []
                seen = set()
                for team in sorted(graph):
                    if team in seen:
                        continue
                    queue = deque([team])
                    seen.add(team)
                    component = []
                    while queue:
                        current = queue.popleft()
                        component.append(current)
                        for other in graph[current]:
                            if other not in seen:
                                seen.add(other)
                                queue.append(other)
                    components.append(sorted(component))
                for idx, teams in enumerate(components[: len(labels)]):
                    for team in teams:
                        team_to_group[team] = labels[idx]
        except Exception:
            pass

    if games:
        inferred = infer_groups_from_games(games)
        for team, group in inferred.items():
            team_to_group.setdefault(team, group)

    _wc_group_cache = team_to_group
    return team_to_group


def team_group_label(team_name: str, team_to_group: dict[str, str] | None = None) -> str:
    if not team_name:
        return ""
    lookup = team_to_group if team_to_group is not None else load_wc_team_groups()
    if team_name in lookup:
        return lookup[team_name]
    key = normalize_team_key(team_name)
    for name, group in lookup.items():
        if normalize_team_key(name) == key:
            return group
    return ""


def classify_match_stage(game: dict, comp_name: str, team_to_group: dict[str, str] | None = None) -> str:
    """Classify a completed match as group, knockout, or league."""
    round_name = str(game.get("round") or game.get("stage") or "").strip()
    round_lower = round_name.lower()
    group_field = str(game.get("group") or "").strip()

    if group_field:
        return "group"
    if GROUP_ROUND_RE.search(round_lower):
        return "group"
    if KNOCKOUT_ROUND_RE.search(round_lower):
        return "knockout"

    fmt = cup_format(comp_name)
    if comp_name == "FIFA/World Cup":
        lookup = team_to_group if team_to_group is not None else {}
        home = canonical_team_name(game.get("home_team", ""), comp_name)
        away = canonical_team_name(game.get("away_team", ""), comp_name)
        gh = team_group_label(home, lookup)
        ga = team_group_label(away, lookup)
        if gh and ga:
            return "group" if gh == ga else "knockout"
        if gh or ga:
            return "knockout"
        stage_field = str(game.get("stage") or "").lower()
        if "group" in stage_field:
            return "group"
        return "knockout"

    if fmt and fmt.get("format") == "knockout":
        return "knockout"

    if fmt and fmt.get("format") == "group_stage_then_knockout":
        lookup = team_to_group or {}
        home = str(game.get("home_team", "")).strip()
        away = str(game.get("away_team", "")).strip()
        gh = team_group_label(home, lookup) or str(game.get("home_group", "")).strip()
        ga = team_group_label(away, lookup) or str(game.get("away_group", "")).strip()
        if gh and ga:
            return "group" if gh == ga else "knockout"

    if fmt and fmt.get("format") == "league_phase_then_knockout":
        if "league phase" in round_lower or "league-phase" in round_lower:
            return "group"
        if KNOCKOUT_ROUND_RE.search(round_lower):
            return "knockout"
        return "group"

    return "league"


def extract_group_label(game: dict, team_to_group: dict[str, str] | None = None) -> str:
    group_field = str(game.get("group") or "").strip().upper()
    if group_field:
        return group_field[:3]
    round_name = str(game.get("round") or "")
    match = GROUP_LABEL_RE.search(round_name)
    if match:
        return match.group(1).upper()
    lookup = team_to_group if team_to_group is not None else load_wc_team_groups()
    home = str(game.get("home_team", "")).strip()
    away = str(game.get("away_team", "")).strip()
    gh = team_group_label(home, lookup)
    ga = team_group_label(away, lookup)
    if gh and gh == ga:
        return gh
    return ""


def match_winner_team(game: dict) -> str | None:
    explicit = str(game.get("winner") or "").strip()
    if explicit:
        return explicit
    if str(game.get("status", "")).lower() != "post":
        return None
    try:
        hs = int(float(game.get("home_score")))
        aws = int(float(game.get("away_score")))
    except (TypeError, ValueError):
        return None
    if hs > aws:
        return str(game.get("home_team", "")).strip() or None
    if aws > hs:
        return str(game.get("away_team", "")).strip() or None
    period = str(game.get("period") or "").lower()
    if "pen" in period:
        return explicit or None
    return None


def collect_competition_games(comp_name: str) -> list[dict]:
    """Gather completed results for a competition from all historical sources."""
    base_comp, _view = resolve_competition_query(comp_name)
    games: list[dict] = []
    seen: set[tuple] = set()

    history = []
    if os.path.exists(config.LIVE_SCORE_HISTORY_FILE):
        try:
            with open(config.LIVE_SCORE_HISTORY_FILE, "r", encoding="utf-8") as handle:
                history = json.load(handle)
        except Exception:
            history = []
    try:
        from live_poller import _live_scores, _live_scores_lock

        with _live_scores_lock:
            for comp_data in _live_scores.values():
                for g in comp_data.get("games", []):
                    if g.get("status") != "post":
                        continue
                    entry = dict(g)
                    entry.setdefault("competition", comp_name)
                    if entry.get("competition") == base_comp:
                        _append_game(games, seen, entry)
    except Exception:
        pass

    for g in history:
        if g.get("competition") != base_comp or g.get("status") != "post":
            continue
        _append_game(games, seen, g)

    for source_rows in (
        _past_games(base_comp),
        _csv_settled_games(base_comp),
        _national_csv_games(base_comp),
        _world_cup_projection_games(base_comp),
    ):
        for g in source_rows:
            _append_game(games, seen, g)

    if base_comp == "United States/MLS":
        for g in games:
            g["home_team"] = canonical_team_name(g.get("home_team", ""), base_comp)
            g["away_team"] = canonical_team_name(g.get("away_team", ""), base_comp)

    return games


def filter_games_by_stage(games: list[dict], comp_name: str, stage: str) -> list[dict]:
    base_comp, _view = resolve_competition_query(comp_name)
    team_to_group = load_wc_team_groups() if base_comp == "FIFA/World Cup" else {}
    return [g for g in games if classify_match_stage(g, base_comp, team_to_group) == stage]


def current_competition_phase(games: list[dict], comp_name: str) -> str:
    base_comp, _view = resolve_competition_query(comp_name)
    team_to_group = load_wc_team_groups() if base_comp == "FIFA/World Cup" else {}
    stages = {classify_match_stage(g, base_comp, team_to_group) for g in games}
    if "knockout" in stages:
        return "knockout"
    if "group" in stages:
        return "group"
    return "league"


def infer_knockout_round_label(game: dict, knockout_games: list[dict], comp_name: str) -> str:
    existing = str(game.get("round") or "").strip()
    if existing and existing.lower() not in {"match", ""}:
        return existing
    count = len(knockout_games)
    if comp_name == "FIFA/World Cup":
        if count == 1:
            return "Final"
        if count == 2:
            return "Semi-finals"
        if count <= 4:
            return "Quarter-finals"
        if count <= 8:
            return "Round of 16"
        return "Round of 32"
    if count == 1:
        return "Final"
    if count == 2:
        return "Semi-finals"
    if count <= 4:
        return "Quarter-finals"
    if count <= 8:
        return "Round of 16"
    return existing or "Knockout Round"


def annotate_knockout_rounds(matches: list[dict], comp_name: str) -> list[dict]:
    team_to_group = load_wc_team_groups(matches) if comp_name == "FIFA/World Cup" else {}
    knockout_games = [
        g for g in matches
        if classify_match_stage(g, comp_name, team_to_group) == "knockout"
    ]
    for game in matches:
        if classify_match_stage(game, comp_name, team_to_group) != "knockout":
            continue
        round_label = infer_knockout_round_label(game, knockout_games, comp_name)
        game["round"] = round_label
    return matches


def should_use_persisted_table(cached: dict | None, force_refresh: bool = False) -> bool:
    if force_refresh or not cached or not isinstance(cached, dict):
        return False
    source = str(cached.get("source", "")).lower()
    if source in {"roster", "placeholder"}:
        return False
    groups = cached.get("groups") or []
    if not groups:
        return False
    if len(groups) == 1 and str(groups[0].get("name", "")).strip().lower() == "overall":
        competition = str(cached.get("competition", "")).strip()
        if competition in config._CUP_FORMATS or competition == "FIFA/World Cup":
            fmt = cup_format(competition)
            if fmt and fmt.get("format") in {"group_stage_then_knockout", "league_phase_then_knockout"}:
                return False
        if competition == "United States/MLS":
            return False
    return True
