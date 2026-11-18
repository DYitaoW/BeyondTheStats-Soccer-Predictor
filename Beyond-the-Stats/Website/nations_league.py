"""Tiered Nations Leagues (CONCACAF, UEFA): groups by tier, knockout ties, fixtures.

ESPN models these competitions as one league with a group stage split into
tiers (League A / B / C ..., each with groups), plus two-legged knockout ties
(CONCACAF quarterfinals, Gold Cup play-in prelims; UEFA quarterfinals / play-
offs). The backend used to squash everything into one table named
"Group gro" (the first three letters of the ``group-stage`` slug), counting
knockout and play-in games as table games, and had no knockout data at all.

- ``group_membership(comp)``: ordered ``[{name, tier, group, teams}]`` from the
  ESPN standings feed (authoritative tier/group assignment). Real tables are
  computed from our own results, restricted to games inside each group.
- ``knockout_ties(comp)``: two-legged ties built from ESPN season events
  (legs, aggregate, winner from ESPN ``series``).
- ``upcoming_fixtures(comp)``: scheduled games with US-Eastern dates, stage,
  tier and group.

ESPN responses are cached on disk (``shared.paths.OUTPUT_CACHE_DIR``) so the
API never blocks on ESPN; a stale cache is served if ESPN is unreachable.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
from competition_rules import canonical_team_name

try:
    from shared import paths as _paths
except Exception:  # pragma: no cover
    _paths = None

ET = ZoneInfo("America/New_York")
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
ESPN_STANDINGS = "https://site.api.espn.com/apis/v2/sports/soccer/{slug}/standings"
MEMBERSHIP_TTL_S = 6 * 3600
EVENTS_TTL_S = 15 * 60
_lock = threading.Lock()
_mem: dict[str, tuple[float, object]] = {}

# Stage slug (ESPN season slug) -> label / order in the knockout section.
STAGE_LABELS = {
    "gold-cup-play-in-prelims": ("Gold Cup Play-In", 1),
    "gold-cup-prelims": ("Gold Cup Prelims", 1),
    "promotion-relegation-playoffs": ("Promotion / Relegation Play-offs", 1),
    "playoffs": ("Play-offs", 2),
    "quarterfinals": ("Quarterfinals", 3),
    "semifinals": ("Semifinals", 4),
    "third-place": ("Third Place", 5),
    "final": ("Final", 6),
}


def espn_slug(competition: str) -> str:
    return str(getattr(config, "TIERED_NATIONS_LEAGUES", {}).get(competition, "") or "")


def is_tiered_nations_league(competition: str) -> bool:
    return bool(espn_slug(competition))


def _cache_path(name: str) -> str:
    base = str(_paths.OUTPUT_CACHE_DIR) if _paths is not None else os.path.join(config.PROJECT_DIR, "Output", "Cache")
    return os.path.join(base, "nations_league", name)


def _cached(name: str, ttl: float, fetch):
    """Memory -> disk -> fetch; on fetch failure serve the last good value."""
    now = time.time()
    with _lock:
        hit = _mem.get(name)
    if hit and now - hit[0] < ttl:
        return hit[1]
    path = _cache_path(name)
    disk = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
    except Exception:
        disk = None
    if disk is not None and now - float(disk.get("fetched_at", 0)) < ttl:
        with _lock:
            _mem[name] = (float(disk["fetched_at"]), disk["value"])
        return disk["value"]
    try:
        value = fetch()
    except Exception:
        value = None
    if value is None:
        return disk.get("value") if disk else None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": now, "value": value}, fh)
        os.replace(tmp, path)
    except Exception:
        pass
    with _lock:
        _mem[name] = (now, value)
    return value


def _get_json(url: str, params: dict | None = None) -> dict:
    import httpx

    resp = httpx.get(url, params=params, timeout=20, headers={"User-Agent": "BeyondTheStats/1.0"})
    resp.raise_for_status()
    return resp.json() or {}


def _canon(name: str, competition: str) -> str:
    name = str(name or "").strip()
    return canonical_team_name(name, competition) or name


def _fetch_standings(competition: str) -> dict:
    return _get_json(ESPN_STANDINGS.format(slug=espn_slug(competition)))


def group_membership(competition: str) -> list[dict]:
    """``[{name: "League A, Group A", tier: "League A", group: "Group A", teams: [...]}]``."""
    if not is_tiered_nations_league(competition):
        return []

    def fetch():
        data = _fetch_standings(competition)
        groups: list[dict] = []

        def walk(node):
            entries = (node.get("standings") or {}).get("entries") or []
            name = str(node.get("name") or "").strip()
            if entries and name:
                tier, _, group = name.partition(",")
                tier, group = (tier.strip(), group.strip()) if group else ("", name)
                if not tier:
                    # UEFA: "Group A1" = League A, group 1.
                    m = re.match(r"^Group\s+([A-Z])\s*(\d+)$", name)
                    if m:
                        tier = f"League {m.group(1)}"
                groups.append({
                    "name": name,
                    "tier": tier,
                    "group": group,
                    "teams": [_canon((e.get("team") or {}).get("displayName"), competition) for e in entries],
                })
            for child in node.get("children") or []:
                walk(child)

        walk(data)
        season = data.get("season") or {}
        return {"groups": groups, "season_start": season.get("startDate"), "season_end": season.get("endDate"),
                "season_name": season.get("displayName")} if groups else None

    value = _cached(f"{espn_slug(competition)}_membership.json", MEMBERSHIP_TTL_S, fetch) or {}
    return list(value.get("groups") or [])


def season_window(competition: str) -> tuple[datetime | None, datetime | None]:
    value = _cached(f"{espn_slug(competition)}_membership.json", MEMBERSHIP_TTL_S, lambda: None) or {}

    def parse(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None

    return parse(value.get("season_start")), parse(value.get("season_end"))


def _months(start: datetime, end: datetime) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def season_events(competition: str) -> list[dict]:
    """Slim ESPN events of the current season (ESPN rejects date ranges: month queries)."""
    if not is_tiered_nations_league(competition):
        return []
    group_membership(competition)  # primes the season window
    start, end = season_window(competition)
    if not start or not end:
        now = datetime.now(timezone.utc)
        start, end = now.replace(month=1, day=1), now.replace(month=12, day=31)

    def fetch():
        events: dict[str, dict] = {}
        for month in _months(start, end):
            payload = _get_json(ESPN_SITE.format(slug=espn_slug(competition)), {"dates": month, "limit": 1000})
            for ev in payload.get("events") or []:
                slim = _slim_event(ev, competition)
                if slim:
                    events[slim["match_id"]] = slim
        return sorted(events.values(), key=lambda e: (e["kickoff_utc"], e["match_id"])) if events else None

    return list(_cached(f"{espn_slug(competition)}_events.json", EVENTS_TTL_S, fetch) or [])


def _slim_event(ev: dict, competition: str) -> dict | None:
    comp = (ev.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    try:
        kickoff = datetime.fromisoformat(str(ev.get("date", "")).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
    st = (ev.get("status") or {}).get("type") or {}
    series = comp.get("series") or {}
    by_id = {str(c.get("id")): c for c in series.get("competitors") or []}
    home_id, away_id = str(home.get("id") or (home.get("team") or {}).get("id")), str(away.get("id") or (away.get("team") or {}).get("id"))

    def score(c):
        try:
            return int(c.get("score"))
        except (TypeError, ValueError):
            return None

    return {
        "match_id": str(ev.get("id") or ""),
        "stage": str((ev.get("season") or {}).get("slug") or ""),
        "kickoff_utc": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "match_date": kickoff.astimezone(ET).strftime("%Y-%m-%d"),
        "match_time_et": kickoff.astimezone(ET).strftime("%H:%M"),
        "home_team": _canon((home.get("team") or {}).get("displayName"), competition),
        "away_team": _canon((away.get("team") or {}).get("displayName"), competition),
        "home_score": score(home) if st.get("state") != "pre" else None,
        "away_score": score(away) if st.get("state") != "pre" else None,
        "state": st.get("state"),
        "status": st.get("name"),
        "completed": bool(st.get("completed")),
        "home_winner": home.get("winner") is True,
        "away_winner": away.get("winner") is True,
        "leg": (comp.get("leg") or {}).get("value"),
        "note": next((n.get("headline") for n in comp.get("notes") or [] if n.get("headline")), None),
        "series_completed": bool(series.get("completed")),
        "series_winner_home": bool((by_id.get(home_id) or {}).get("winner")),
        "series_winner_away": bool((by_id.get(away_id) or {}).get("winner")),
    }


def _is_group_stage(stage: str) -> bool:
    return not stage or "group" in stage or stage in ("league-stage", "league-phase", "regular-season")


def team_tiers(competition: str) -> dict[str, dict]:
    """team -> {tier, group, name} from the membership."""
    out = {}
    for g in group_membership(competition):
        for t in g["teams"]:
            out[t] = {"tier": g["tier"], "group": g["group"], "name": g["name"]}
    return out


def knockout_ties(competition: str) -> dict[str, list[dict]]:
    """``{stage_slug: [tie, ...]}`` for every non-group stage (two-legged ties merged)."""
    ties: dict[tuple, dict] = {}
    for ev in season_events(competition):
        if _is_group_stage(ev["stage"]):
            continue
        key = (ev["stage"], frozenset((ev["home_team"], ev["away_team"])))
        tie = ties.get(key)
        if tie is None:
            tie = ties[key] = {
                "stage": ev["stage"],
                "stage_label": STAGE_LABELS.get(ev["stage"], (ev["stage"].replace("-", " ").title(), 9))[0],
                "home_team": ev["home_team"], "away_team": ev["away_team"],  # first-leg hosts
                "legs": [], "winner": None, "status": "scheduled",
            }
        tie["legs"].append({
            "leg": ev["leg"], "match_id": ev["match_id"], "date": ev["match_date"], "time_et": ev["match_time_et"],
            "kickoff_utc": ev["kickoff_utc"], "home_team": ev["home_team"], "away_team": ev["away_team"],
            "home_score": ev["home_score"], "away_score": ev["away_score"], "status": ev["status"],
            "state": ev["state"], "note": ev["note"],
            "home_winner": ev["home_winner"], "away_winner": ev["away_winner"],
        })
        if ev["series_completed"] and (ev["series_winner_home"] or ev["series_winner_away"]):
            tie["winner"] = ev["home_team"] if ev["series_winner_home"] else ev["away_team"]
    out: dict[str, list[dict]] = defaultdict(list)
    for tie in ties.values():
        tie["legs"].sort(key=lambda leg: (leg["leg"] or 0, leg["kickoff_utc"]))
        a, b = tie["home_team"], tie["away_team"]
        agg = {a: 0, b: 0}
        away_goals = {a: 0, b: 0}
        # Live legs count toward the aggregate (as ESPN's "lead X-Y" notes do).
        counted = [leg for leg in tie["legs"] if leg["home_score"] is not None and leg["state"] in ("in", "post")]
        played = [leg for leg in counted if leg["state"] == "post"]
        for leg in counted:
            agg[leg["home_team"]] = agg.get(leg["home_team"], 0) + leg["home_score"]
            agg[leg["away_team"]] = agg.get(leg["away_team"], 0) + leg["away_score"]
            away_goals[leg["away_team"]] = away_goals.get(leg["away_team"], 0) + leg["away_score"]
        tie["aggregate"] = agg
        tie["legs_played"] = len(played)
        tie["legs_total"] = len(tie["legs"])
        if not tie["winner"] and tie["legs"] and len(played) == len(tie["legs"]):
            # ESPN does not always mark the series complete: decide it from the
            # legs (aggregate, then away goals, then the last leg's winner flag
            # for extra time / penalties).
            if agg[a] != agg[b]:
                tie["winner"] = a if agg[a] > agg[b] else b
            elif away_goals[a] != away_goals[b]:
                tie["winner"] = a if away_goals[a] > away_goals[b] else b
            else:
                last = played[-1]
                if last.get("home_winner") or last.get("away_winner"):
                    tie["winner"] = last["home_team"] if last.get("home_winner") else last["away_team"]
        if tie["winner"]:
            tie["status"] = "complete"
        elif counted:
            tie["status"] = "in_progress"
        tie["note"] = next((leg["note"] for leg in reversed(tie["legs"]) if leg["note"]), None)
        out[tie["stage"]].append(tie)
    order = {slug: STAGE_LABELS.get(slug, ("", 9))[1] for slug in out}
    return {slug: sorted(out[slug], key=lambda t: t["legs"][0]["kickoff_utc"]) for slug in sorted(out, key=lambda s: order[s])}


def upcoming_fixtures(competition: str) -> list[dict]:
    """Scheduled games (today onward), US-Eastern dates, with stage / tier / group."""
    tiers = team_tiers(competition)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    out = []
    for ev in season_events(competition):
        if ev["state"] != "pre" or ev["match_date"] < today:
            continue
        group_stage = _is_group_stage(ev["stage"])
        home_tier = tiers.get(ev["home_team"], {})
        same_group = group_stage and home_tier and home_tier.get("name") == tiers.get(ev["away_team"], {}).get("name")
        out.append({
            "match_id": ev["match_id"], "match_date": ev["match_date"], "match_time_et": ev["match_time_et"],
            "kickoff_utc": ev["kickoff_utc"], "home_team": ev["home_team"], "away_team": ev["away_team"],
            "stage": ev["stage"],
            "stage_label": "Group Stage" if group_stage else STAGE_LABELS.get(ev["stage"], (ev["stage"].title(), 9))[0],
            "tier": home_tier.get("tier") if same_group else None,
            "group": home_tier.get("group") if same_group else None,
            "leg": ev["leg"], "note": ev["note"],
        })
    return out
