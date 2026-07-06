"""ESPN HTTP requests (schedule, teams, standings, leaders, event summaries)."""
import json
import time
import urllib.request
from datetime import datetime, timezone

import config
from espn_parser import _parse_espn_live_event

LIVE_SCORE_FETCH_TIMEOUT = 15

_TEAMS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_ROSTER_CACHE: dict[str, tuple[float, dict]] = {}
_SCHEDULE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_ESPN_CACHE_TTL = 600  # 10 minutes for stable data (teams, rosters)

_STANDINGS_STAT_NAMES = {
    "points": "points",
    "rank": "rank",
    "gamesPlayed": "played",
    "wins": "wins",
    "losses": "losses",
    "ties": "draws",
    "goalsFor": "goals_for",
    "goalsAgainst": "goals_against",
    "goalDifference": "goal_difference",
    "form": "form",
    "winPct": "win_pct",
    "gamesBehind": "gb",
    "streak": "streak",
}

def _fetch_competition_scores(comp_name, espn_id, today_str):
    """Fetch ESPN scoreboard for one competition/date, return parsed games."""
    url = f"{config.LIVE_SCORE_ESPN_BASE}/{espn_id}/scoreboard?dates={today_str}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=LIVE_SCORE_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []
    events = data.get("events") or []
    games = []
    for ev in events:
        parsed = _parse_espn_live_event(ev)
        if parsed:
            parsed["competition"] = comp_name
            games.append(parsed)
    return games

def _fetch_espn_json(url):
    """Fetch and parse JSON from an ESPN API URL. Returns None on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=LIVE_SCORE_FETCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None

def _fetch_competition_teams(comp_name, espn_id):
    """Return list of teams in a competition from ESPN."""
    cache_key = f"teams_{comp_name}"
    now = time.time()
    cached = _TEAMS_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _ESPN_CACHE_TTL:
        return cached[1]
    data = _fetch_espn_json(f"{config.LIVE_SCORE_ESPN_BASE}/{espn_id}/teams")
    if data is None:
        return None
    teams = []
    for team in (data.get("sports") or [{}])[0].get("leagues") or [{}]:
        for t in team.get("teams") or []:
            entry = t.get("team", t)
            teams.append({
                "id": str(entry.get("id", "")),
                "abbreviation": str(entry.get("abbreviation", "")),
                "display_name": str(entry.get("displayName", "")),
                "short_name": str(entry.get("shortDisplayName", "")),
                "logo": str(entry.get("logo", "")),
            })
    _TEAMS_CACHE[cache_key] = (now, teams)
    return teams

def _fetch_team_info(comp_name, espn_id, team_id):
    """Return full team info including roster from ESPN."""
    cache_key = f"roster_{comp_name}_{team_id}"
    now = time.time()
    cached = _ROSTER_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _ESPN_CACHE_TTL:
        return cached[1]
    data = _fetch_espn_json(f"{config.LIVE_SCORE_ESPN_BASE}/{espn_id}/teams/{team_id}")
    if data is None:
        return None
    team = data.get("team", data)
    result = {
        "id": str(team.get("id", "")),
        "display_name": str(team.get("displayName", "")),
        "abbreviation": str(team.get("abbreviation", "")),
        "location": str(team.get("location", "")),
        "logo": str(team.get("logo", "")),
        "color": str(team.get("color", "")),
        "record": _parse_espn_team_record(team),
    }
    # Athletes / roster
    athletes = []
    for entry in team.get("athletes") or []:
        for a in (entry if isinstance(entry, list) else [entry]):
            athlete = a.get("athlete", a) if isinstance(a, dict) else a
            athletes.append({
                "id": str(athlete.get("id", "")),
                "full_name": str(athlete.get("fullName", "")),
                "short_name": str(athlete.get("shortName", "")),
                "jersey": str(athlete.get("jersey", "")),
                "position": str(athlete.get("position", {}).get("abbreviation", "")),
                "position_name": str(athlete.get("position", {}).get("displayName", "")),
                "age": athlete.get("age"),
                "height": str(athlete.get("height", "")),
                "weight": str(athlete.get("weight", "")),
                "birth_place": str(athlete.get("birthPlace", {}).get("city", "")),
                "nationality": str(athlete.get("nationality", "")),
                "headshot": str(athlete.get("headshot", {}).get("href", "")),
            })
    if athletes:
        result["roster"] = athletes

    # Season stats
    stats = []
    for s in (team.get("seasonStats") or []):
        stats.append({
            "name": str(s.get("name", "")),
            "display_name": str(s.get("displayName", "")),
            "value": s.get("displayValue", ""),
        })
    if stats:
        result["season_stats"] = stats

    _ROSTER_CACHE[cache_key] = (now, result)
    return result

def _parse_espn_team_record(team):
    """Extract win/loss/draw record from an ESPN team response."""
    record_summary = str(team.get("recordSummary", ""))
    record_data = {}
    records = team.get("record") or []
    for r in records:
        rtype = str(r.get("type", ""))
        stats = r.get("stats") or []
        for s in stats:
            record_data[rtype] = record_data.get(rtype, {})
            record_data[rtype][str(s.get("name", ""))] = s.get("value", s.get("displayValue", ""))
    return {
        "summary": record_summary,
        "details": record_data,
    }

def _fetch_competition_schedule(comp_name, espn_id, days_forward=90):
    """Fetch full schedule for a competition from today to *days_forward* out."""
    cache_key = f"sched_{comp_name}"
    now = time.time()
    cached = _SCHEDULE_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _ESPN_CACHE_TTL:
        return cached[1]
    today_str = date.today().strftime("%Y%m%d")
    end = (date.today() + timedelta(days=days_forward)).strftime("%Y%m%d")
    data = _fetch_espn_json(f"{config.LIVE_SCORE_ESPN_BASE}/{espn_id}/scoreboard?dates={today_str}-{end}")
    if data is None:
        return None
    games = []
    for ev in (data.get("events") or []):
        parsed = _parse_espn_live_event(ev)
        if parsed:
            parsed["competition"] = comp_name
            games.append(parsed)
    _SCHEDULE_CACHE[cache_key] = (now, games)
    return games

def _parse_standings_entry(entry):
    """Parse a single ESPN standings entry into a normalized dict."""
    team = entry.get("team") or {}
    stats_raw = {s["name"]: s["value"] for s in (entry.get("stats") or []) if s.get("name") is not None}
    result = {"team": str(team.get("displayName", "")), "team_id": str(team.get("id", ""))}
    for espn_name, our_name in _STANDINGS_STAT_NAMES.items():
        val = stats_raw.get(espn_name)
        if val is not None:
            try:
                result[our_name] = int(float(val))
            except (ValueError, TypeError):
                result[our_name] = str(val)
    return result

def _fetch_standings(comp_name, espn_id):
    """Fetch and parse ESPN standings for a competition.

    Tries the basic URL first, then falls back to season-specific URLs
    to handle competitions whose season has ended (European leagues in June,
    etc.).  Returns a normalized dict or None.
    """
    now = datetime.now()
    # Derive candidate season years: current year, then the most recent
    # European season start (current_year-1 if before August, else current_year).
    candidate_seasons = [str(now.year)]
    if now.month < 8:
        candidate_seasons.append(str(now.year - 1))
    else:
        candidate_seasons.append(str(now.year))
    candidate_urls = [f"{config.LIVE_SCORE_ESPN_BASE}/{espn_id}/standings"]
    for s in candidate_seasons:
        candidate_urls.append(f"{config.LIVE_SCORE_ESPN_BASE}/{espn_id}/standings?season={s}")

    data = None
    for url in candidate_urls:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=LIVE_SCORE_FETCH_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
            if data.get("standings"):
                break
        except Exception:
            continue

    if data is None:
        return None

    standings_list = data.get("standings") or []
    if not standings_list:
        return None

    # Find the "total" or "overall" standing type (ignore home/away splits).
    primary = standings_list[0]
    for s in standings_list:
        if s.get("type") in ("total", "overall"):
            primary = s
            break

    groups = []
    children = primary.get("children")
    if children:
        # Group/tournament format (World Cup groups, UCL groups, etc.)
        for child in children:
            group_name = str(child.get("name", "")) or str(child.get("abbreviation", ""))
            entries = [_parse_standings_entry(e) for e in (child.get("entries") or [])]
            entries.sort(key=lambda x: x.get("rank", 999))
            groups.append({"name": group_name, "entries": entries})
    else:
        # Single league table format
        entries = [_parse_standings_entry(e) for e in (primary.get("entries") or [])]
        entries.sort(key=lambda x: x.get("rank", 999))
        groups.append({"name": str(primary.get("name", "Overall")), "entries": entries})

    if not groups:
        return None

    return {
        "competition": comp_name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "groups": groups,
    }

def _fetch_leaders(comp_name, espn_id):
    """Fetch ESPN statistical leaders for a competition.

    Returns a normalized dict with:
        competition, updated_at, categories: {category_key: [{rank, player, team, value}]}
    Returns None if no leaders data is available.
    """
    now = datetime.now()
    candidate_seasons = [str(now.year)]
    if now.month < 8:
        candidate_seasons.append(str(now.year - 1))
    else:
        candidate_seasons.append(str(now.year))
    candidate_urls = [f"{config.LIVE_SCORE_ESPN_BASE}/{espn_id}/statistics/leaders"]
    for s in candidate_seasons:
        candidate_urls.append(f"{config.LIVE_SCORE_ESPN_BASE}/{espn_id}/statistics/leaders?season={s}")

    data = None
    for url in candidate_urls:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=LIVE_SCORE_FETCH_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
            if data.get("leaders"):
                break
        except Exception:
            continue

    if data is None:
        return None

    leaders_list = data.get("leaders") or []
    if not leaders_list:
        return None

    categories = {}
    for cat in leaders_list:
        abbr = cat.get("abbreviation", "") or cat.get("shortDisplayName", "")
        if not abbr:
            continue
        entries = cat.get("leaders") or []
        parsed_entries = []
        for rank_idx, entry in enumerate(entries, 1):
            athlete = entry.get("athlete") or {}
            team_info = athlete.get("team") or {}
            player_name = str(athlete.get("displayName", "") or athlete.get("shortName", ""))
            team_name = str(team_info.get("displayName", "") or "")
            raw_val = entry.get("value", entry.get("displayValue", ""))
            try:
                val = int(float(raw_val))
            except (ValueError, TypeError):
                val = raw_val
            if player_name:
                parsed_entries.append({
                    "rank": rank_idx,
                    "player": player_name,
                    "team": team_name,
                    "value": val,
                })
        if parsed_entries:
            label = LEADER_CATEGORY_LABELS.get(abbr, abbr)
            categories[abbr] = {
                "label": label,
                "entries": parsed_entries,
            }

    if not categories:
        return None

    return {
        "competition": comp_name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "categories": categories,
    }

def _fetch_event_summary(comp_name, espn_id, event_id):
    """Fetch ESPN summary for a single event to get lineups."""
    url = "%s/%s/summary?event=%s" % (config.LIVE_SCORE_ESPN_BASE, espn_id, event_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=LIVE_SCORE_FETCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None
