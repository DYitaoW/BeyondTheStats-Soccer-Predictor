"""Competition-specific rules for real standings and cup tables."""
from __future__ import annotations

import json
import os
import re
import time
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

MLS_SEASON_FILE_RE = re.compile(r"^mlsstat(\d{4})\.csv$", re.IGNORECASE)
LIGA_MX_SEASON_FILE_RE = re.compile(r"^mexstat(\d{4})\.csv$", re.IGNORECASE)

_UEFA_COMPETITIONS = frozenset({
    "UEFA/Champions League", "UEFA/Europa League", "UEFA/Conference League",
    "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
})

NATIONAL_MATCHES_CSV = os.path.join(
    config.PROJECT_DIR, "Data", "National_Team_Data", "national_team_recent_matches_raw.csv"
)
WORLD_CUP_PROJECTION_FILE = os.path.join(
    config.PROJECT_DIR, "Data", "Predictions", "world_cup_projection.json"
)

_mls_name_cache: dict[str, str] | None = None
_wc_group_cache: dict[str, str] | None = None

# Batch games cache: single-pass load avoids N× file I/O
_ALL_GAMES_BY_COMPETITION: dict[str, list[dict]] | None = None
_ALL_GAMES_CACHE_TIME: float = 0
_ALL_GAMES_CACHE_TTL: float = 120  # seconds


def _batch_load_all_games() -> dict[str, list[dict]]:
    """Load all completed games from every source in a single pass.

    Returns a dict {competition_name: [game_dict, ...]} so callers never
    need to re-read files per competition.
    """
    by_comp: dict[str, list[dict]] = defaultdict(list)
    seen_by_comp: dict[str, set] = defaultdict(set)

    # ── 1. Live-score history file ───────────────────────────────
    history = []
    if os.path.exists(config.LIVE_SCORE_HISTORY_FILE):
        try:
            with open(config.LIVE_SCORE_HISTORY_FILE, "r", encoding="utf-8") as handle:
                history = json.load(handle)
        except Exception:
            history = []
    for g in history:
        comp = g.get("competition", "")
        if not comp or g.get("status") != "post":
            continue
        _append_game(by_comp[comp], seen_by_comp[comp], g)

    # ── 2. Live poller in-memory scores ──────────────────────────
    try:
        from live_poller import _live_scores, _live_scores_lock

        with _live_scores_lock:
            for comp_data in _live_scores.values():
                for g in comp_data.get("games", []):
                    if g.get("status") != "post":
                        continue
                    entry = dict(g)
                    comp = entry.get("competition", "")
                    if comp:
                        _append_game(by_comp[comp], seen_by_comp[comp], entry)
    except Exception:
        pass

    # ── 3. past_games.json ───────────────────────────────────────
    if os.path.exists(config.PAST_GAMES_FILE):
        try:
            with open(config.PAST_GAMES_FILE, "r", encoding="utf-8") as handle:
                past = json.load(handle)
        except Exception:
            past = []
        if isinstance(past, list):
            for g in past:
                comp = str(g.get("competition", "")).strip()
                if not comp:
                    continue
                try:
                    hs = int(float(g.get("actual_home_goals") or g.get("home_score")))
                    aws = int(float(g.get("actual_away_goals") or g.get("away_score")))
                except (TypeError, ValueError):
                    continue
                _append_game(by_comp[comp], seen_by_comp[comp], {
                    "competition": comp,
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

    # ── 4. Settled CSVs — batch read every file once ─────────────
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
        for comp_name, group in frame.groupby(
            frame["competition"].astype(str).str.strip()
        ):
            if not comp_name:
                continue
            for _, row in group.iterrows():
                actual = str(row.get("actual_result", "")).strip().upper()
                if actual not in {"H", "D", "A"}:
                    continue
                try:
                    hs = int(float(row.get("actual_home_goals")))
                    aws = int(float(row.get("actual_away_goals")))
                except (TypeError, ValueError):
                    continue
                _append_game(by_comp[comp_name], seen_by_comp[comp_name], {
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

    # ── 5. National-team CSV ─────────────────────────────────────
    if os.path.exists(NATIONAL_MATCHES_CSV):
        try:
            nat_frame = pd.read_csv(NATIONAL_MATCHES_CSV, dtype=str)
        except Exception:
            nat_frame = None
        if nat_frame is not None and not nat_frame.empty and "competition" in nat_frame.columns:
            for comp_name, group in nat_frame.groupby(
                nat_frame["competition"].astype(str).str.strip()
            ):
                if not comp_name:
                    continue
                for _, row in group.iterrows():
                    status = str(row.get("status", "")).upper()
                    if "FULL_TIME" not in status and "FINAL" not in status:
                        continue
                    try:
                        hs = int(float(row.get("FTHG")))
                        aws = int(float(row.get("FTAG")))
                    except (TypeError, ValueError):
                        continue
                    stage = str(row.get("stage", "")).strip()
                    _append_game(by_comp[comp_name], seen_by_comp[comp_name], {
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

    # ── 6. World Cup projection JSON ─────────────────────────────
    wc_comp = "FIFA/World Cup"
    if os.path.exists(WORLD_CUP_PROJECTION_FILE):
        try:
            with open(WORLD_CUP_PROJECTION_FILE, "r", encoding="utf-8") as handle:
                wc_payload = json.load(handle)
        except Exception:
            wc_payload = None
        if isinstance(wc_payload, dict):
            fixtures = wc_payload.get("group_fixtures") or []
            if isinstance(fixtures, list):
                for fixture in fixtures:
                    if not isinstance(fixture, dict):
                        continue
                    actual = str(fixture.get("actual_result", "")).strip().upper()
                    if actual not in {"H", "D", "A"}:
                        continue
                    try:
                        hs_wc = int(float(fixture.get("home_goals", fixture.get("actual_home_goals"))))
                        aws_wc = int(float(fixture.get("away_goals", fixture.get("actual_away_goals"))))
                    except (TypeError, ValueError):
                        continue
                    group = str(fixture.get("group", "")).strip()
                    _append_game(by_comp[wc_comp], seen_by_comp[wc_comp], {
                        "competition": wc_comp,
                        "home_team": str(fixture.get("home_team", "")).strip(),
                        "away_team": str(fixture.get("away_team", "")).strip(),
                        "home_score": hs_wc,
                        "away_score": aws_wc,
                        "status": "post",
                        "round": f"Group Stage - Group {group}" if group else "Group Stage",
                        "stage": "group-stage",
                        "group": group,
                        "match_date": str(fixture.get("match_date", "")).strip()[:10],
                        "match_datetime_utc": str(fixture.get("match_datetime_utc", "")).strip(),
                        "source": "wc_projection",
                    })

    # ── 7. MLS season CSV ────────────────────────────────────────
    _batch_mls_or_liga_mx(by_comp, seen_by_comp, "United States/MLS",
                          _find_latest_mls_season_file(), resolve_mls_team_name)

    # ── 8. Liga MX season CSV (map every name like MLS) ───────────
    _batch_mls_or_liga_mx(by_comp, seen_by_comp, config.LIGA_MX_COMPETITION,
                          _find_latest_liga_mx_season_file(), resolve_liga_mx_team_name)

    # ── 9. MLS / Liga MX name resolution (all sources, not just CSV) ──
    for g in by_comp.get("United States/MLS", []):
        g["home_team"] = resolve_mls_team_name(g.get("home_team", ""))
        g["away_team"] = resolve_mls_team_name(g.get("away_team", ""))
    for g in by_comp.get(config.LIGA_MX_COMPETITION, []):
        g["home_team"] = resolve_liga_mx_team_name(g.get("home_team", ""))
        g["away_team"] = resolve_liga_mx_team_name(g.get("away_team", ""))

    return dict(by_comp)


def _batch_mls_or_liga_mx(
    by_comp: dict,
    seen_by_comp: dict,
    comp_name: str,
    path: str | None,
    team_resolver=None,
) -> None:
    """Reusable helper for MLS / Liga MX season CSV loading."""
    if not path:
        return
    try:
        frame = pd.read_csv(path, dtype=str)
    except Exception:
        return
    if frame.empty:
        return

    processed = "HomeTeam" in frame.columns and "FTHG" in frame.columns
    for _, row in frame.iterrows():
        if processed:
            home_raw = row.get("HomeTeam")
            away_raw = row.get("AwayTeam")
            result = str(row.get("FTR", "")).strip().upper()
            home_goals = row.get("FTHG")
            away_goals = row.get("FTAG")
        else:
            home_raw = row.get("Home")
            away_raw = row.get("Away")
            result = str(row.get("Res", "")).strip().upper()
            home_goals = row.get("HG")
            away_goals = row.get("AG")

        if result not in {"H", "D", "A"}:
            continue
        try:
            hs = int(float(home_goals))
            aws = int(float(away_goals))
        except (TypeError, ValueError):
            continue

        if team_resolver:
            home = team_resolver(home_raw)
            away = team_resolver(away_raw)
        else:
            home = str(home_raw or "").strip()
            away = str(away_raw or "").strip()
        if not home or not away:
            continue

        date_raw = str(row.get("Date", "")).strip()
        match_date = ""
        if date_raw:
            parsed = pd.to_datetime(date_raw, errors="coerce", dayfirst=(not processed))
            if pd.notna(parsed):
                match_date = parsed.strftime("%Y-%m-%d")

        _append_game(by_comp[comp_name], seen_by_comp[comp_name], {
            "competition": comp_name,
            "home_team": home,
            "away_team": away,
            "home_score": hs,
            "away_score": aws,
            "status": "post",
            "match_date": match_date,
            "source": f"season_csv:{os.path.basename(path)}",
        })


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
    # Competition mapping: exact alias match first; key match only when unique
    # (aggressive token stripping can collide, e.g. Man United vs Man City).
    path = config.TEAM_NAME_DISPLAY_MAPPING_FILE
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            base_comp, _view = resolve_competition_query(competition)
            comp_map = payload.get(base_comp) or payload.get(competition) or {}
            if isinstance(comp_map, dict):
                lower = text.lower()
                lower_and = lower.replace(" & ", " and ").replace("&", " and ")
                key = normalize_team_key(text)
                key_to_canons: dict[str, set[str]] = {}
                for raw_name, canonical in comp_map.items():
                    raw = str(raw_name or "").strip()
                    canon = str(canonical or "").strip()
                    if not canon:
                        continue
                    raw_l = raw.lower()
                    canon_l = canon.lower()
                    raw_and = raw_l.replace(" & ", " and ").replace("&", " and ")
                    canon_and = canon_l.replace(" & ", " and ").replace("&", " and ")
                    if lower in {raw_l, canon_l} or lower_and in {raw_and, canon_and}:
                        return canon
                    for candidate in (raw, canon):
                        ck = normalize_team_key(candidate)
                        if ck:
                            key_to_canons.setdefault(ck, set()).add(canon)
                hits = key_to_canons.get(key) or set()
                if len(hits) == 1:
                    return next(iter(hits))
        except Exception:
            pass
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
    """Identity for completed-game dedupe.

    Team sides are mapped through ``canonical_team_name`` first so ESPN
    ``Manchester City`` and football-data ``Man City`` collapse to one match.
    ``normalize_team_key`` alone is unsafe (strips ``city``/``united`` and can
    collide Man City vs Man United, while failing to equate Man City vs Manchester City).
    """
    comp = str(game.get("competition", "")).strip()
    home = canonical_team_name(game.get("home_team"), comp) or str(game.get("home_team") or "")
    away = canonical_team_name(game.get("away_team"), comp) or str(game.get("away_team") or "")
    return (
        comp.lower(),
        str(game.get("match_date", "")).strip()[:10],
        home.strip().lower(),
        away.strip().lower(),
    )


def _append_game(games: list[dict], seen: set, game: dict) -> None:
    if not game:
        return
    comp = str(game.get("competition", "")).strip()
    home = str(game.get("home_team", "")).strip()
    away = str(game.get("away_team", "")).strip()
    if not home or not away:
        return
    # Store canonical football-data names so real tables match predicted.
    home = canonical_team_name(home, comp) or home
    away = canonical_team_name(away, comp) or away
    hs = game.get("home_score")
    aws = game.get("away_score")
    if hs is None or aws is None:
        return
    try:
        hs_i = int(float(hs))
        aws_i = int(float(aws))
    except (TypeError, ValueError):
        return
    key = _game_match_key({
        "competition": comp,
        "match_date": game.get("match_date"),
        "home_team": home,
        "away_team": away,
    })
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


def resolve_mls_team_name(raw_name: str) -> str:
    raw = str(raw_name or "").strip()
    if not raw:
        return ""
    mapped = canonical_team_name(raw, "United States/MLS")
    if mapped:
        return mapped
    key = normalize_team_key(raw)
    for team in list(MLS_EASTERN_CONFERENCE_TEAMS) + list(MLS_WESTERN_CONFERENCE_TEAMS):
        team_key = normalize_team_key(team)
        if key == team_key or key in team_key or team_key in key:
            return team
    return raw


def resolve_liga_mx_team_name(raw_name: str) -> str:
    """Map ESPN/display Liga MX names onto football-data CSV canons."""
    raw = str(raw_name or "").strip()
    if not raw:
        return ""
    mapped = canonical_team_name(raw, config.LIGA_MX_COMPETITION)
    if mapped and mapped != raw:
        return mapped
    if mapped:
        return mapped
    return raw


def _find_latest_mls_season_file() -> str | None:
    candidates: list[tuple[int, str]] = []
    for base in (
        os.path.join(config.PROJECT_DIR, "MLS", "Data", "Processed_Data"),
        os.path.join(config.PROJECT_DIR, "MLS", "Data", "Raw_Data"),
    ):
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for name in files:
                match = MLS_SEASON_FILE_RE.match(name)
                if not match:
                    continue
                candidates.append((int(match.group(1)), os.path.join(root, name)))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _find_latest_liga_mx_season_file() -> str | None:
    base = os.path.join(config.PROJECT_DIR, "MLS", "Data", "Raw_Data", "Mexico", "Liga MX")
    if not os.path.isdir(base):
        return None
    candidates: list[tuple[int, str]] = []
    for name in os.listdir(base):
        match = LIGA_MX_SEASON_FILE_RE.match(name)
        if match:
            candidates.append((int(match.group(1)), os.path.join(base, name)))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _mls_season_csv_games(comp_name: str) -> list[dict]:
    """Load completed MLS results from the latest mlsstatYYYY.csv season file."""
    if comp_name != "United States/MLS":
        return []
    path = _find_latest_mls_season_file()
    if not path:
        return []
    try:
        frame = pd.read_csv(path, dtype=str)
    except Exception:
        return []
    if frame.empty:
        return []

    rows: list[dict] = []
    processed = "HomeTeam" in frame.columns and "FTHG" in frame.columns
    for _, row in frame.iterrows():
        if processed:
            home_raw = row.get("HomeTeam")
            away_raw = row.get("AwayTeam")
            result = str(row.get("FTR", "")).strip().upper()
            home_goals = row.get("FTHG")
            away_goals = row.get("FTAG")
        else:
            home_raw = row.get("Home")
            away_raw = row.get("Away")
            result = str(row.get("Res", "")).strip().upper()
            home_goals = row.get("HG")
            away_goals = row.get("AG")

        if result not in {"H", "D", "A"}:
            continue
        try:
            hs = int(float(home_goals))
            aws = int(float(away_goals))
        except (TypeError, ValueError):
            continue

        home = resolve_mls_team_name(home_raw)
        away = resolve_mls_team_name(away_raw)
        if not home or not away:
            continue

        date_raw = str(row.get("Date", "")).strip()
        match_date = ""
        if date_raw:
            parsed = pd.to_datetime(date_raw, errors="coerce", dayfirst=False)
            if pd.notna(parsed):
                match_date = parsed.strftime("%Y-%m-%d")

        rows.append({
            "competition": comp_name,
            "home_team": home,
            "away_team": away,
            "home_score": hs,
            "away_score": aws,
            "status": "post",
            "match_date": match_date,
            "source": f"mls_season_csv:{os.path.basename(path)}",
        })
    return rows


def _liga_mx_season_csv_games(comp_name: str) -> list[dict]:
    """Load completed Liga MX results from the latest mexstatYYYY.csv season file."""
    if comp_name != config.LIGA_MX_COMPETITION:
        return []
    path = _find_latest_liga_mx_season_file()
    if not path:
        return []
    try:
        frame = pd.read_csv(path, dtype=str)
    except Exception:
        return []
    if frame.empty:
        return []

    rows: list[dict] = []
    processed = "HomeTeam" in frame.columns and "FTHG" in frame.columns
    for _, row in frame.iterrows():
        if processed:
            home_raw = row.get("HomeTeam")
            away_raw = row.get("AwayTeam")
            result = str(row.get("FTR", "")).strip().upper()
            home_goals = row.get("FTHG")
            away_goals = row.get("FTAG")
        else:
            home_raw = row.get("Home")
            away_raw = row.get("Away")
            result = str(row.get("Res", "")).strip().upper()
            home_goals = row.get("HG")
            away_goals = row.get("AG")

        if result not in {"H", "D", "A"}:
            continue
        try:
            hs = int(float(home_goals))
            aws = int(float(away_goals))
        except (TypeError, ValueError):
            continue

        home = str(home_raw or "").strip()
        away = str(away_raw or "").strip()
        if not home or not away:
            continue

        date_raw = str(row.get("Date", "")).strip()
        match_date = ""
        if date_raw:
            parsed = pd.to_datetime(date_raw, errors="coerce", dayfirst=True)
            if pd.notna(parsed):
                match_date = parsed.strftime("%Y-%m-%d")

        rows.append({
            "competition": comp_name,
            "home_team": home,
            "away_team": away,
            "home_score": hs,
            "away_score": aws,
            "status": "post",
            "match_date": match_date,
            "source": f"liga_mx_season_csv:{os.path.basename(path)}",
        })
    return rows


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
    """Gather completed results for a competition from all historical sources.

    Uses a process-lifetime batch cache (TTL-sec) so that all source files
    are read **once** instead of once per competition — critical when the
    caller iterates over dozens of leagues.
    """
    global _ALL_GAMES_BY_COMPETITION, _ALL_GAMES_CACHE_TIME
    now = time.time()
    if (
        _ALL_GAMES_BY_COMPETITION is None
        or (now - _ALL_GAMES_CACHE_TIME) > _ALL_GAMES_CACHE_TTL
    ):
        _ALL_GAMES_BY_COMPETITION = _batch_load_all_games()
        _ALL_GAMES_CACHE_TIME = now
    base_comp, _view = resolve_competition_query(comp_name)
    return _ALL_GAMES_BY_COMPETITION.get(base_comp, [])


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


# ── Real standings layout & tiebreaker rules (canonical) ─────────────
# Built into standings_cache.json at pipeline write time. API readers always
# re-sanitize (dedupe + roster align) before serving so alias duplicates cannot ship.

STANDINGS_LAYOUT_SINGLE = "single_table"
STANDINGS_LAYOUT_MLS = "mls_conferences"
STANDINGS_LAYOUT_BELGIAN = "belgian_two_phase"
STANDINGS_LAYOUT_SCOTTISH = "scottish_split"
STANDINGS_LAYOUT_LEAGUE_PHASE = "league_phase"
STANDINGS_LAYOUT_CUP_GROUPS = "cup_groups"
STANDINGS_LAYOUT_LIGA_MX = "liga_mx_tournament"

# Leagues that rank tied teams by head-to-head record before goal difference.
H2H_TIEBREAKER_COMPETITIONS = frozenset({
    "Spain/La Liga",
    "Spain/La Liga 2",
    "Italy/Serie A",
    "Italy/Serie B",
    "Portugal/Liga Portugal",
    "Belgium/First Division A",
    "Turkey/Super Lig",
    config.LIGA_MX_COMPETITION,
})


def standings_layout_for(comp_name: str) -> str:
    """Return the structural layout id for real standings JSON."""
    base_comp, mls_view = resolve_competition_query(comp_name)
    if base_comp == config.LIGA_MX_COMPETITION:
        return STANDINGS_LAYOUT_LIGA_MX
    if base_comp == "United States/MLS" or mls_view:
        return STANDINGS_LAYOUT_MLS
    if "belgium" in base_comp.lower():
        return STANDINGS_LAYOUT_BELGIAN
    if "scotland" in base_comp.lower() or "scottish" in base_comp.lower():
        return STANDINGS_LAYOUT_SCOTTISH
    if base_comp in _UEFA_COMPETITIONS:
        return STANDINGS_LAYOUT_LEAGUE_PHASE
    fmt = cup_format(base_comp)
    if fmt and fmt.get("format") == "league_phase_then_knockout":
        return STANDINGS_LAYOUT_LEAGUE_PHASE
    if fmt and fmt.get("format") == "group_stage_then_knockout":
        return STANDINGS_LAYOUT_CUP_GROUPS
    return STANDINGS_LAYOUT_SINGLE


def uses_h2h_tiebreaker(comp_name: str) -> bool:
    """True when tied teams are separated by head-to-head before goal difference."""
    base_comp, _view = resolve_competition_query(comp_name)
    return base_comp in H2H_TIEBREAKER_COMPETITIONS


def active_liga_mx_tournament_label(reference_date=None) -> str:
    """Return the active short-tournament label, e.g. ``Clausura 2026``."""
    from datetime import date

    ref = reference_date or date.today()
    if ref.month >= 7:
        return f"Apertura {ref.year}"
    return f"Clausura {ref.year}"


def _liga_mx_game_tournament(game: dict) -> str | None:
    raw = str(game.get("match_date") or game.get("match_datetime_utc") or "")[:10]
    if len(raw) < 7:
        return None
    try:
        year = int(raw[:4])
        month = int(raw[5:7])
    except ValueError:
        return None
    if month >= 7:
        return f"Apertura {year}"
    return f"Clausura {year}"


def filter_games_to_liga_mx_tournament(
    games: list[dict],
    tournament_label: str | None = None,
) -> list[dict]:
    """Keep only matches belonging to the requested Liga MX short tournament."""
    label = tournament_label or active_liga_mx_tournament_label()
    return [g for g in games if _liga_mx_game_tournament(g) == label]


def competition_format_spec(comp_name: str) -> dict:
    """Machine-readable competition rules for clients (layout, tiebreakers, extras)."""
    base_comp, mls_view = resolve_competition_query(comp_name)
    layout = standings_layout_for(comp_name)
    tiebreaker = "h2h" if uses_h2h_tiebreaker(comp_name) else "gd"
    cup_fmt = cup_format(base_comp)

    spec: dict = {
        "competition": comp_name,
        "base_competition": base_comp,
        "competition_type": "cup" if cup_fmt else "league",
        "standings_layout": layout,
        "tiebreaker": tiebreaker,
        "notes": [],
        "extensions": {},
    }
    if mls_view:
        spec["mls_view"] = mls_view

    if layout == STANDINGS_LAYOUT_SINGLE:
        spec["notes"].append("Single round-robin table ranked by points, then tiebreakers.")
    elif layout == STANDINGS_LAYOUT_MLS:
        spec["notes"].append(
            "Supporters Shield overall table plus separate Eastern and Western conference tables."
        )
        spec["extensions"]["playoff_format"] = "mls_cup"
        spec["extensions"]["conferences"] = ["Eastern Conference", "Western Conference"]
    elif layout == STANDINGS_LAYOUT_LIGA_MX:
        active = active_liga_mx_tournament_label()
        spec["extensions"]["active_tournament"] = active
        spec["extensions"]["tournaments_per_season"] = ["Apertura", "Clausura"]
        spec["extensions"]["regular_season_matches"] = 17
        spec["extensions"]["playoff_format"] = "liguilla"
        if active.startswith("Clausura 2026"):
            spec["extensions"]["liguilla"] = {"direct_qualifiers": 8, "play_in": None}
            spec["notes"].append(
                "Clausura 2026: top 8 qualify directly for the Liguilla (play-in removed for World Cup)."
            )
        else:
            spec["extensions"]["liguilla"] = {"direct_qualifiers": 6, "play_in": [7, 10]}
            spec["notes"].append(
                "Top 6 reach Liguilla quarter-finals; places 7–10 contest a play-in for the last two spots."
            )
        spec["notes"].append(
            "Mexico plays two independent short tournaments per year; tables show the active tournament only."
        )
    elif layout == STANDINGS_LAYOUT_BELGIAN:
        spec["notes"].append(
            "Regular season then championship / Europe / relegation playoff groups after 30 matches."
        )
    elif layout == STANDINGS_LAYOUT_SCOTTISH:
        spec["notes"].append("Single table until 33 matches, then top-six / bottom-six split groups.")
    elif layout == STANDINGS_LAYOUT_LEAGUE_PHASE:
        spec["notes"].append("Single league-phase table; top sides advance to two-legged knockout rounds.")
        spec["extensions"]["knockout"] = True
    elif layout == STANDINGS_LAYOUT_CUP_GROUPS:
        spec["notes"].append("Group-stage tables with knockout rounds for advancing teams.")
        spec["extensions"]["knockout"] = True

    if cup_fmt:
        spec["format"] = cup_fmt.get("format")
        spec["cup_format"] = cup_fmt

    if tiebreaker == "h2h":
        spec["notes"].append("Among tied teams: head-to-head points before overall goal difference.")
    else:
        spec["notes"].append("Among tied teams: goal difference before head-to-head.")

    return spec


def expected_standings_group_names(comp_name: str) -> list[str] | None:
    """Required group names for a valid persisted table (None = flexible)."""
    base_comp, mls_view = resolve_competition_query(comp_name)
    layout = standings_layout_for(comp_name)
    if layout == STANDINGS_LAYOUT_MLS:
        if mls_view == "east":
            return ["Eastern Conference"]
        if mls_view == "west":
            return ["Western Conference"]
        if mls_view == "shield":
            return ["Supporters Shield"]
        return ["Supporters Shield", "Eastern Conference", "Western Conference"]
    if layout == STANDINGS_LAYOUT_LEAGUE_PHASE:
        return ["League Phase"]
    if layout == STANDINGS_LAYOUT_BELGIAN:
        return None
    if layout == STANDINGS_LAYOUT_SCOTTISH:
        return None
    if layout == STANDINGS_LAYOUT_CUP_GROUPS:
        return None
    if layout == STANDINGS_LAYOUT_LIGA_MX:
        return None
    return ["Overall"]


def standings_shape_is_valid(cached: dict, comp_name: str) -> bool:
    """True when persisted JSON already matches the competition's required layout."""
    if not isinstance(cached, dict):
        return False
    groups = cached.get("groups") or []
    if not groups:
        return False
    names = {str(g.get("name", "")).strip() for g in groups if isinstance(g, dict)}
    if not names:
        return False
    # Reject caches that still carry duplicate team rows (ESPN + football-data
    # aliases for the same club). Force a recompute / sanitize path instead.
    for group in groups:
        if not isinstance(group, dict):
            continue
        seen_keys: set[str] = set()
        for entry in group.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            team = str(entry.get("team", "")).strip()
            if not team:
                continue
            canon = canonical_team_name(team, comp_name) or team
            key = canon.lower()
            if key in seen_keys:
                return False
            seen_keys.add(key)
    layout = standings_layout_for(comp_name)
    expected = expected_standings_group_names(comp_name)
    if expected is not None:
        return set(expected).issubset(names)
    if layout == STANDINGS_LAYOUT_BELGIAN:
        return "Overall" not in names
    if layout == STANDINGS_LAYOUT_CUP_GROUPS:
        return not (len(groups) == 1 and names == {"Overall"})
    if layout == STANDINGS_LAYOUT_LIGA_MX:
        active = active_liga_mx_tournament_label()
        if "Overall" in names:
            return False
        return active in names
    return True


def package_real_standings(
    comp_name: str,
    groups: list[dict],
    source: str,
    *,
    current_phase: str | None = None,
) -> dict:
    """Assemble the canonical real-standings JSON object for persistence."""
    from datetime import datetime, timezone

    base_comp, mls_view = resolve_competition_query(comp_name)
    layout = standings_layout_for(comp_name)
    payload: dict = {
        "competition": comp_name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "groups": groups,
        "source": source,
        "standings_layout": layout,
        "tiebreaker": "h2h" if uses_h2h_tiebreaker(comp_name) else "gd",
    }
    if mls_view:
        payload["mls_view"] = mls_view
    cup_fmt = cup_format(base_comp)
    if cup_fmt:
        payload["format"] = cup_fmt.get("format")
    if current_phase:
        payload["current_phase"] = current_phase
    if layout == STANDINGS_LAYOUT_MLS and not mls_view:
        payload["playoff_format"] = "mls_cup"
    if layout in {STANDINGS_LAYOUT_LEAGUE_PHASE, STANDINGS_LAYOUT_CUP_GROUPS} or cup_fmt:
        try:
            from knockout import _build_knockout_framework

            ko = _build_knockout_framework(base_comp)
            if ko:
                payload["knockout_rounds"] = ko
        except Exception:
            pass
    return payload


def _zero_standing_entry(team: str, rank: int) -> dict:
    return {
        "team": team,
        "rank": rank,
        "position": rank,
        "P": 0,
        "W": 0,
        "D": 0,
        "L": 0,
        "GF": 0,
        "GA": 0,
        "GD": 0,
        "Pts": 0,
    }


def _entries_from_teams(teams: list[str]) -> list[dict]:
    ordered = sorted({str(t).strip() for t in teams if str(t).strip()})
    return [_zero_standing_entry(team, idx + 1) for idx, team in enumerate(ordered)]


def build_structured_standings_groups(comp_name: str, teams: list[str]) -> list[dict]:
    """Build competition-appropriate group shells (zero points) for *teams*."""
    base_comp, mls_view = resolve_competition_query(comp_name)
    team_list = sorted({str(t).strip() for t in teams if str(t).strip()})
    if not team_list:
        return []

    layout = standings_layout_for(comp_name)

    if layout == STANDINGS_LAYOUT_MLS:
        east = [t for t in team_list if mls_conference(t) == "east"]
        west = [t for t in team_list if mls_conference(t) == "west"]
        if mls_view == "east":
            return [{"name": "Eastern Conference", "entries": _entries_from_teams(east)}]
        if mls_view == "west":
            return [{"name": "Western Conference", "entries": _entries_from_teams(west)}]
        if mls_view == "shield":
            return [{"name": "Supporters Shield", "entries": _entries_from_teams(team_list)}]
        return [
            {"name": "Supporters Shield", "entries": _entries_from_teams(team_list)},
            {"name": "Eastern Conference", "entries": _entries_from_teams(east)},
            {"name": "Western Conference", "entries": _entries_from_teams(west)},
        ]

    if layout == STANDINGS_LAYOUT_LIGA_MX:
        return [{"name": active_liga_mx_tournament_label(), "entries": _entries_from_teams(team_list)}]

    if layout == STANDINGS_LAYOUT_BELGIAN:
        return [{"name": "Regular Season", "entries": _entries_from_teams(team_list)}]

    if layout == STANDINGS_LAYOUT_SCOTTISH:
        return [{"name": "Overall", "entries": _entries_from_teams(team_list)}]

    if layout == STANDINGS_LAYOUT_LEAGUE_PHASE:
        return [{"name": "League Phase", "entries": _entries_from_teams(team_list)}]

    if base_comp == "FIFA/World Cup":
        fmt = cup_format(base_comp)
        team_to_group = load_wc_team_groups()
        groups_map: dict[str, list[str]] = defaultdict(list)
        unassigned: list[str] = []
        for team in team_list:
            label = team_group_label(team, team_to_group)
            if label:
                groups_map[label].append(team)
            else:
                unassigned.append(team)
        labels = list(fmt.get("group_labels") or "ABCDEFGHIJKL") if fmt else list("ABCDEFGHIJKL")
        label_idx = 0
        for team in sorted(unassigned):
            while label_idx < len(labels) and len(groups_map.get(labels[label_idx], [])) >= 4:
                label_idx += 1
            if label_idx >= len(labels):
                break
            groups_map[labels[label_idx]].append(team)
        return [
            {"name": f"Group {label}", "entries": _entries_from_teams(groups_map[label])}
            for label in sorted(groups_map)
            if groups_map[label]
        ]

    cup_fmt = cup_format(base_comp)
    if layout == STANDINGS_LAYOUT_CUP_GROUPS or (
        cup_fmt and cup_fmt.get("format") == "group_stage_then_knockout"
    ):
        group_count = int((cup_fmt or {}).get("group_count") or 0)
        if group_count > 1:
            buckets: list[list[str]] = [[] for _ in range(group_count)]
            for idx, team in enumerate(team_list):
                buckets[idx % group_count].append(team)
            labels = list("ABCDEFGHIJKL")[:group_count]
            return [
                {"name": f"Group {labels[idx]}", "entries": _entries_from_teams(buckets[idx])}
                for idx in range(group_count)
                if buckets[idx]
            ]
        # Generic group-stage cups: chunk teams into groups of four when possible.
        if len(team_list) >= 8:
            labels = list("ABCDEFGHIJKL")
            buckets = [[] for _ in range(min(len(labels), max(1, len(team_list) // 4)))]
            for idx, team in enumerate(team_list):
                buckets[idx % len(buckets)].append(team)
            return [
                {"name": f"Group {labels[idx]}", "entries": _entries_from_teams(buckets[idx])}
                for idx in range(len(buckets))
                if buckets[idx]
            ]

    return [{"name": "Overall", "entries": _entries_from_teams(team_list)}]


def filter_games_to_active_season(
    games: list[dict],
    competition: str,
    reference_date=None,
) -> list[dict]:
    """Keep only completed games that belong to the active season window.

    European fall–spring leagues: Jul 1 of the active start year → May 31 end year
    (active start year flips on Jul 15 via ``season_calendar``).
    Calendar-year leagues: Jan 1–Dec 31 of the current calendar year.

    In Jul–Aug before the new season CSV exists, this correctly drops the prior
    season's May finales so real tables start empty (preseason).
    """
    if not games:
        return []
    try:
        import sys as _sys
        import os as _os
        _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        import season_calendar as sc
    except Exception:
        return list(games)

    base_comp, _view = resolve_competition_query(competition)
    try:
        start, end = sc.fixture_search_bounds(base_comp, reference_date=reference_date)
    except Exception:
        return list(games)

    start_s = start.strftime("%Y-%m-%d")
    end_s = end.strftime("%Y-%m-%d")
    out = []
    for game in games:
        md = str(game.get("match_date") or game.get("match_datetime_utc") or "")[:10]
        if len(md) < 10:
            continue
        if start_s <= md <= end_s:
            out.append(game)
    return out


def should_use_persisted_table(cached: dict | None, force_refresh: bool = False) -> bool:
    if force_refresh or not cached or not isinstance(cached, dict):
        return False
    source = str(cached.get("source", "")).lower()
    if source in {"roster", "placeholder"}:
        return False
    comp_name = str(cached.get("competition", "")).strip()
    if not comp_name:
        return False
    return standings_shape_is_valid(cached, comp_name)
