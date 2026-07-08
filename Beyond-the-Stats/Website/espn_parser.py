"""ESPN API response parsing (lineups, shots, injuries, stats, game info)."""
import re
import pandas as pd
from zoneinfo import ZoneInfo

from team_utils import _to_int

def _parse_espn_game_info(summary_data):
    """Extract venue, attendance, officials, and weather from ESPN summary ``gameInfo``.

    Returns dict with keys:
        ``venue`` (str), ``attendance`` (int/None), ``officials`` (list),
        ``weather`` (dict: temperature, windSpeed, conditions, humidity)
    or empty dict if unavailable.
    """
    game_info = summary_data.get("gameInfo") or {}
    venue = game_info.get("venue") or {}
    broadcasts = summary_data.get("broadcasts") or []
    result = {}
    if venue.get("fullName"):
        result["venue"] = str(venue["fullName"])
    att = game_info.get("attendance")
    if att is not None:
        try:
            result["attendance"] = int(att)
        except (ValueError, TypeError):
            pass
    officials_list = game_info.get("officials") or []
    if officials_list:
        refs = []
        for off in officials_list:
            name = str(off.get("fullName", ""))
            pos = str(off.get("position", {}).get("displayName", ""))
            if name:
                refs.append({"name": name, "role": pos})
        if refs:
            result["officials"] = refs
    # ── Weather ────────────────────────────────────────────────
    weather = game_info.get("weather") or {}
    if weather:
        w = {}
        for key in ("temperature", "windSpeed", "conditions", "humidity"):
            val = weather.get(key)
            if val is not None:
                try:
                    w[key] = int(val) if key in ("temperature", "humidity") else str(val)
                except (ValueError, TypeError):
                    w[key] = str(val)
        if w:
            result["weather"] = w
    if broadcasts:
        tv = []
        for b in broadcasts:
            media = b.get("media") or {}
            mkt = b.get("market") or {}
            tv.append({
                "network": str(media.get("shortName", media.get("name", ""))),
                "type": str(b.get("type", {}).get("shortName", "")),
                "region": str(mkt.get("type", "")),
            })
        if tv:
            result["broadcasts"] = tv
    return result


def _parse_espn_shot_mapping(summary_data):
    """Extract shot-plot and goal-location data from ESPN summary.

    Returns dict with:
        ``shot_origins``  — list of {x, y, minute, player, team_id, is_goal, on_target}
        ``goal_locations`` — list of {x, y, minute, player, team_id}  (goal-frame coords)
    or empty dict if unavailable.
    """
    shot_origins = []
    goal_locations = []

    # Helper to normalise 2-element coordinate values.
    def _coord(entry, key, default=None):
        raw = (entry.get(key) or {}) if isinstance(entry, dict) else {}
        x = raw.get("x")
        y = raw.get("y")
        if x is not None and y is not None:
            try:
                return (round(float(x), 1), round(float(y), 1))
            except (ValueError, TypeError):
                pass
        return default

    # ── 1. shotChart (common in ESPN v2) ──────────────────────
    sc = summary_data.get("shotChart")
    if isinstance(sc, dict):
        for side_key, team_shots in sc.items():
            if not isinstance(team_shots, list):
                continue
            team_id = {"home": "home", "away": "away"}.get(side_key, "")
            for shot in team_shots:
                if not isinstance(shot, dict):
                    continue
                xy = _coord(shot, "coordinates")
                gl = _coord(shot, "goalLocation")
                ath = shot.get("athlete") or {}
                player = str(ath.get("displayName", ""))
                scoring = bool(shot.get("scoringPlay"))
                t_id = str(team_id)
                minute = str(shot.get("clock", {}).get("displayValue", ""))
                if xy:
                    shot_origins.append({
                        "x": xy[0], "y": xy[1],
                        "minute": minute,
                        "player": player,
                        "team_id": t_id,
                        "is_goal": scoring,
                        "on_target": bool(shot.get("onTarget", scoring)),
                    })
                if gl and scoring:
                    goal_locations.append({
                        "x": gl[0], "y": gl[1],
                        "minute": minute,
                        "player": player,
                        "team_id": t_id,
                    })

    # ── 2. situations (alternative ESPN path) ─────────────────
    situations = summary_data.get("situations")
    if isinstance(situations, list):
        for ev in situations:
            if not isinstance(ev, dict):
                continue
            action = ev.get("lastAction") or ev
            if not isinstance(action, dict):
                continue
            ev_type = str(ev.get("type", {}).get("text", action.get("type", {}).get("text", "")))
            if "shot" not in ev_type.lower() and "goal" not in ev_type.lower() and "save" not in ev_type.lower():
                continue
            xy = _coord(action, "coordinates")
            gl = _coord(action, "goalLocation")
            ath = action.get("athlete") or {}
            player = str(ath.get("displayName", ""))
            team = action.get("team") or ev.get("team") or {}
            t_id = str(team.get("id", ""))
            scoring = bool(action.get("scoringPlay", ev.get("scoringPlay")))
            minute = str(action.get("clock", {}).get("displayValue", ev.get("clock", {}).get("displayValue", "")))
            on_target = "save" in ev_type.lower() or "goal" in ev_type.lower() or "on target" in ev_type.lower()
            if xy:
                shot_origins.append({
                    "x": xy[0], "y": xy[1],
                    "minute": minute,
                    "player": player,
                    "team_id": t_id,
                    "is_goal": scoring,
                    "on_target": on_target or scoring,
                })
            if gl and scoring:
                goal_locations.append({
                    "x": gl[0], "y": gl[1],
                    "minute": minute,
                    "player": player,
                    "team_id": t_id,
                })

    result = {}
    if shot_origins:
        result["shot_origins"] = shot_origins
    if goal_locations:
        result["goal_locations"] = goal_locations
    return result

def _parse_espn_situation(summary_data):
    """Extract possession-zone splits and game-control data from ESPN summary.

    The ``situation`` (singular) block contains territory splits,
    possession percentages, and zone-by-zone breakdowns.

    Returns dict with:
        ``possession`` — overall possession {home: %, away: %}
        ``possession_zones`` — possession by third … {home: {attacking, midfield, defensive}, away: …}
        ``territory`` — territory split by third {home: {attacking, midfield, defensive}, away: …}
    or empty dict if unavailable.
    """
    sit = summary_data.get("situation") or {}
    if not sit:
        return {}

    result = {}

    def _safe_number(v):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return v

    def _parse_third_dict(raw):
        """Normalise a possessive-third dict e.g. {attacking: 30, midfield: 45, defensive: 25}."""
        if not isinstance(raw, dict):
            return None
        out = {}
        for zone in ("attacking", "midfield", "defensive"):
            val = raw.get(zone)
            if val is not None:
                out[zone] = _safe_number(val)
        return out if out else None

    # ── Overall possession ─────────────────────────────────────
    poss = sit.get("possession")
    if isinstance(poss, dict):
        h = _safe_number(poss.get("home"))
        a = _safe_number(poss.get("away"))
        if h is not None and a is not None:
            result["possession"] = {"home": h, "away": a}

    # ── Possession by zone ─────────────────────────────────────
    zones = sit.get("possessionZones") or sit.get("possessionByArea") or {}
    if isinstance(zones, dict):
        hz = _parse_third_dict(zones.get("home"))
        az = _parse_third_dict(zones.get("away"))
        if hz or az:
            result["possession_zones"] = {}
            if hz:
                result["possession_zones"]["home"] = hz
            if az:
                result["possession_zones"]["away"] = az

    # ── Territory split (alternative path) ─────────────────────
    terr = sit.get("territory") or sit.get("territorySplit") or {}
    if isinstance(terr, dict):
        hz = _parse_third_dict(terr.get("home"))
        az = _parse_third_dict(terr.get("away"))
        if hz or az:
            result["territory"] = {}
            if hz:
                result["territory"]["home"] = hz
            if az:
                result["territory"]["away"] = az

    # ── Most-possession indicator ──────────────────────────────
    most = sit.get("mostPossession")
    if isinstance(most, str) and most in ("home", "away"):
        result["most_possession"] = most

    return result

def _parse_espn_injuries_availability(summary_data):
    """Extract player injury and availability data from ESPN summary.

    ESPN soccer summaries **do not** always include a structured injuries
    block, but when available it lives under ``gameInfo.injuries`` or
    ``playerStatus`` / ``availability`` at the summary root.

    Returns dict with:
        ``injuries`` — list of {player, team_id, status, detail}
        ``availability`` — list of {player, team_id, status}
    or empty dict if unavailable.
    """
    result = {}

    # ── gameInfo.injuries ─────────────────────────────────────
    game_info = summary_data.get("gameInfo") or {}
    injuries_raw = game_info.get("injuries") or summary_data.get("injuries") or []
    if isinstance(injuries_raw, list) and injuries_raw:
        parsed = []
        for entry in injuries_raw:
            if not isinstance(entry, dict):
                continue
            ath = entry.get("athlete") or {}
            team = entry.get("team") or {}
            status = entry.get("status", {}).get("text", "") or entry.get("status", "")
            parsed.append({
                "player": str(ath.get("displayName", ath.get("fullName", ""))),
                "team_id": str(team.get("id", "")),
                "status": str(status),
                "detail": str(entry.get("text", entry.get("comment", ""))),
            })
        if parsed:
            result["injuries"] = parsed

    # ── availability / playerStatus (root-level) ──────────────
    avail = summary_data.get("availability") or summary_data.get("playerStatus") or []
    if isinstance(avail, list) and avail:
        parsed = []
        for entry in avail:
            if not isinstance(entry, dict):
                continue
            ath = entry.get("athlete") or {}
            team = entry.get("team") or {}
            parsed.append({
                "player": str(ath.get("displayName", ath.get("fullName", ""))),
                "team_id": str(team.get("id", "")),
                "status": str(entry.get("status", {}).get("text", entry.get("status", ""))),
            })
        if parsed:
            result["availability"] = parsed

    return result

def _parse_espn_team_stats(summary_data):
    """Extract granular per-team stats from the ``teamStats`` block.

    ESPN's ``teamStats`` block provides stats grouped by category
    (e.g. ``offensive``, ``defensive``, ``passing``, ``possession``)
    with richer detail than the flat ``boxscore`` list.

    Returns dict mapping ``"home"`` / ``"away"`` to a dict of
    category → list-of-stats, or empty dict if unavailable.

    Fields commonly found inside each category:
      offensive  → expectedGoals, shotsTotal, shotsOnGoal, shotsOffGoal,
                    blockedShots, shotsInsideBox, shotsOutsideBox
      defensive  → tackles, clearances, interceptions, blocks, aerialsWon,
                    duelsWon, recoveries
      passing    → passesTotal, passesAccurate, passAccuracy, longPasses,
                    crosses, keyPasses, throughBalls
      possession → possessionPct, possession90, dominantThird
      cards      → yellowCards, redCards, fouls
    """
    team_stats = summary_data.get("teamStats")
    if not isinstance(team_stats, dict):
        return {}

    result = {}
    for side_key in ("home", "away"):
        side = team_stats.get(side_key)
        if not isinstance(side, dict):
            continue
        categories = side.get("statistics") or side.get("categories") or []
        if isinstance(categories, list):
            side_out = {}
            for cat in categories:
                if not isinstance(cat, dict):
                    continue
                cat_name = str(cat.get("name", cat.get("displayName", "")))
                stats_list = cat.get("stats") or cat.get("statistics") or []
                if not isinstance(stats_list, list):
                    continue
                parsed_stats = []
                for s in stats_list:
                    if not isinstance(s, dict):
                        continue
                    parsed_stats.append({
                        "name": s.get("name", ""),
                        "display_name": s.get("displayName", s.get("label", "")),
                        "value": s.get("displayValue", s.get("value", "")),
                    })
                if parsed_stats:
                    side_out[cat_name] = parsed_stats
            if side_out:
                result[side_key] = side_out

    return result

def _utc_to_et(utc_str):
    """Convert a UTC datetime string to ET; return empty string on failure."""
    try:
        if not utc_str:
            return ""
        dt = pd.to_datetime(str(utc_str), utc=True)
        if pd.isna(dt):
            return ""
        return dt.tz_convert(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return ""

def _parse_espn_live_event(event):
    """Parse a single ESPN event dict into a minimal live-score payload."""
    try:
        comp = event.get("competitions") or [{}]
        comp_data = comp[0] if comp else {}
        competitors = comp_data.get("competitors") or []
        if len(competitors) < 2:
            return None
        home = competitors[0] if competitors[0].get("homeAway") == "home" else competitors[1]
        away = competitors[1] if competitors[0].get("homeAway") == "home" else competitors[0]
        status = comp_data.get("status") or {}
        type_detail = status.get("type") or {}
        state = type_detail.get("state", "pre")
        detail = type_detail.get("detail", "")
        clock = comp_data.get("clock") or ""
        display_clock = f"{clock} {detail}" if clock else detail

        home_team_name = str(home.get("team", {}).get("displayName", ""))
        away_team_name = str(away.get("team", {}).get("displayName", ""))
        winner_team = ""
        if home.get("winner") is True:
            winner_team = home_team_name
        elif away.get("winner") is True:
            winner_team = away_team_name

        goalscorers = []
        red_cards = []
        details = comp_data.get("details") or []
        home_team_id = str(home.get("id", ""))
        away_team_id = str(away.get("id", ""))
        for d in details:
            side = "home" if str(d.get("team", {}).get("id", "")) == home_team_id else "away"
            if d.get("scoringPlay"):
                athletes = d.get("athletesInvolved") or [{}]
                athlete = athletes[0] if athletes else {}
                goalscorers.append({
                    "team": side,
                    "scorer": str(athlete.get("displayName", d.get("text", ""))),
                    "minute": str(d.get("clock", {}).get("displayValue", "")),
                    "type": str(d.get("type", {}).get("text", "")),
                })
            elif d.get("redCard"):
                athletes = d.get("athletesInvolved") or [{}]
                athlete = athletes[0] if athletes else {}
                red_cards.append({
                    "team": side,
                    "player": str(athlete.get("displayName", "")),
                    "minute": str(d.get("clock", {}).get("displayValue", "")),
                })

        # ── Bracket / round info from ESPN tournament data ────
        tournament_info = comp_data.get("tournament") or {}
        round_info = comp_data.get("round") or {}
        round_name = str(round_info.get("name", "")) or str(tournament_info.get("round", ""))
        bracket_slot = _to_int(round_info.get("number", round_info.get("position", 0)))

        # ── Game statistics ────────────────────────────────
        EXCLUDED_STATS = {"shotAssists", "goalAssists", "appearances"}
        home_stats = {}
        for s in (home.get("statistics") or []):
            if s.get("name") not in EXCLUDED_STATS:
                home_stats[s["name"]] = s.get("displayValue", "")
        away_stats = {}
        for s in (away.get("statistics") or []):
            if s.get("name") not in EXCLUDED_STATS:
                away_stats[s["name"]] = s.get("displayValue", "")

        raw_date = event.get("date", "")
        parsed_dt = pd.to_datetime(raw_date, utc=True, errors="coerce")
        match_date_str = ""
        if pd.notna(parsed_dt):
            try:
                match_date_str = parsed_dt.tz_convert(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            except Exception:
                match_date_str = raw_date[:10] if len(raw_date) >= 10 else ""

        result = {
            "match_id": str(event.get("id", "")),
            "home_team": home_team_name,
            "away_team": away_team_name,
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "home_score": _to_int(home.get("score")),
            "away_score": _to_int(away.get("score")),
            "status": state,
            "period": detail,
            "clock": display_clock.strip(),
            "kickoff_utc": _utc_to_et(raw_date),
            "match_date": match_date_str,
            "goalscorers": goalscorers,
            "red_cards": red_cards,
        }
        if home_stats:
            result["home_stats"] = home_stats
        if away_stats:
            result["away_stats"] = away_stats
        if round_name:
            result["round"] = round_name
            result["round_order"] = bracket_slot
        if winner_team:
            result["winner"] = winner_team
        if "pen" in str(detail).lower():
            result["decided_by_penalties"] = True
        return result
    except Exception:
        return None

def _parse_espn_lineups(summary_data):
    """Extract lineups from ESPN summary endpoint rosters.

    Returns a dict mapping "home" / "away" to:
        {
            "formation": "4-2-3-1",
            "startXI": [{ "name": "...", "number": ..., "position": "...", "grid": "..." }],
            "substitutes": [{ "name": "...", "number": ..., "position": "..." }],
        }
    or empty dict if rosters not available.
    """
    rosters = summary_data.get("rosters") or []
    if not rosters or len(rosters) < 2:
        return {}
    result = {}
    has_lineup_data = False
    for r in rosters:
        side = "home" if r.get("homeAway") == "home" else "away"
        formation = r.get("formation") or ""
        roster = r.get("roster") or []
        startXI = []
        substitutes = []
        for entry in roster:
            athlete = entry.get("athlete") or {}
            position = entry.get("position") or {}
            name = str(athlete.get("displayName") or athlete.get("fullName") or "")
            number = entry.get("jersey")
            if number is not None:
                try:
                    number = int(number)
                except (ValueError, TypeError):
                    number = None
            pos_abbr = str(position.get("abbreviation") or "")
            grid = entry.get("formationPlace") or ""
            player = {"name": name}
            if number is not None:
                player["number"] = number
            if pos_abbr:
                player["position"] = pos_abbr
            if entry.get("starter") and grid:
                player["grid"] = grid
                startXI.append(player)
            else:
                substitutes.append(player)
        result[side] = {
            "formation": formation,
            "startXI": startXI,
            "substitutes": substitutes,
        }
        if startXI:
            has_lineup_data = True
    return result if has_lineup_data else {}

def _parse_espn_head_to_head(summary_data):
    """Extract head-to-head history from summary.

    Returns list of past match results between the two teams, or [] if unavailable.
    """
    h2h = summary_data.get("headToHeadGames") or []
    results = []
    for entry in h2h:
        events = entry.get("events") or []
        for ev in events:
            comps = ev.get("competitions") or []
            for c in comps:
                comps_list = c.get("competitors") or []
                if len(comps_list) < 2:
                    continue
                teams = {}
                for comp in comps_list:
                    side = "home" if comp.get("homeAway") == "home" else "away"
                    teams[side] = comp.get("team", {}).get("displayName", "")
                results.append({
                    "date": ev.get("date", "")[:10],
                    "home_team": teams.get("home", ""),
                    "away_team": teams.get("away", ""),
                    "home_score": comps_list[0].get("score"),
                    "away_score": comps_list[1].get("score"),
                    "winner": "home" if any(c.get("winner") for c in comps_list if c.get("homeAway") == "home") else "away" if any(c.get("winner") for c in comps_list if c.get("homeAway") == "away") else "draw",
                })
    return results

def _parse_espn_last_five(summary_data):
    """Extract last 5 games for each team from summary.

    Returns dict mapping team name to list of recent results.
    """
    last5 = summary_data.get("lastFiveGames") or []
    result = {}
    for entry in last5:
        team = entry.get("team", {}).get("displayName", "")
        events = entry.get("events") or []
        team_results = []
        for ev in events:
            comps = ev.get("competitions") or []
            for c in comps:
                comps_list = c.get("competitors") or []
                if len(comps_list) < 2:
                    continue
                teams = {}
                for comp in comps_list:
                    side = "home" if comp.get("homeAway") == "home" else "away"
                    teams[side] = comp.get("team", {}).get("displayName", "")
                team_results.append({
                    "date": ev.get("date", "")[:10],
                    "home_team": teams.get("home", ""),
                    "away_team": teams.get("away", ""),
                    "home_score": comps_list[0].get("score"),
                    "away_score": comps_list[1].get("score"),
                    "result": "W" if any(c.get("winner") and c.get("homeAway") == "home" and teams.get("home") == team for c in comps_list) or any(c.get("winner") and c.get("homeAway") == "away" and teams.get("away") == team for c in comps_list) else "L" if any(c.get("winner") for c in comps_list) else "D",
                })
        if team:
            result[team] = team_results
    return result

def _parse_espn_key_events(summary_data):
    """Extract key match events from summary.

    Returns list of event dicts with type, text, period, clock, team.
    """
    events = summary_data.get("keyEvents") or []
    result = []
    for ev in events:
        entry = {
            "type": str(ev.get("type", {}).get("text", "")),
            "text": str(ev.get("text", "")),
            "short_text": str(ev.get("shortText", "")),
            "period": ev.get("period", {}).get("number"),
            "clock": str(ev.get("clock", {}).get("displayValue", "")),
            "scoring_play": bool(ev.get("scoringPlay")),
        }
        team = ev.get("team") or {}
        if team.get("id"):
            entry["team_id"] = str(team["id"])
        athlete = ev.get("athlete") or {}
        if athlete.get("id"):
            entry["athlete_id"] = str(athlete["id"])
            entry["athlete_name"] = str(athlete.get("displayName", ""))
        result.append(entry)
    return result

def _parse_espn_boxscore_stats(summary_data):
    """Extract per-team boxscore statistics from summary.

    Returns dict mapping "home" / "away" to list of stat objects
    {name, displayName, displayValue}, or empty dict.
    """
    boxscore = summary_data.get("boxscore") or {}
    teams = boxscore.get("teams") or []
    if not teams or len(teams) < 2:
        return {}
    result = {}
    for t in teams:
        side = "home" if t.get("homeAway") == "home" else "away"
        stats = t.get("statistics") or []
        result[side] = [{
            "name": s.get("name", ""),
            "display_name": s.get("displayName", ""),
            "value": s.get("displayValue", ""),
        } for s in stats if s.get("name")]
    return result

def _parse_elapsed_minutes(clock_str, period_str):
    """Parse elapsed match minutes from ESPN clock/period strings."""
    clock_str = str(clock_str or "0'").strip()
    period_str = str(period_str or "").strip().lower()
    if "halftime" in period_str or ("half" in period_str and "1st" in period_str):
        return 45
    nums = re.findall(r"\d+", clock_str.split("+")[0])
    if not nums:
        return 0
    base = int(nums[0])
    if "+" in clock_str:
        extra = re.findall(r"\d+", clock_str.split("+")[1] if "+" in clock_str else "")
        if extra:
            base += int(extra[0])
    if "2nd" in period_str or "second" in period_str:
        return min(45 + max(0, base), 99)
    return min(base, 50)
