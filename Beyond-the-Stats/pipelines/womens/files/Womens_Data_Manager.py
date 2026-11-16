"""Womens Data Manager — real NWSL / WSL fixtures and results from ESPN.

Downloads every season's schedule and results from the ESPN scoreboard API
(``usa.nwsl``, ``eng.w.1``), month by month, and writes them as the raw season
CSVs the rest of the womens pipeline reads (``Raw_Data/<country>/<league>/
<prefix>stat<start year>.csv``). Completed results of the current season are
synced into SQLite ``past_games`` (real scores only, no invented predictions).

History: this module used to GENERATE every season (round-robin fixtures from
a shuffled roster, Poisson scores from hard-coded ratings, fake odds/shots) and
wrote those fictional results into production ``past_games``. ``main()`` now
removes those rows (``purge_generated_rows``) so only real data remains.

Raw CSV columns: Country, League, Season, Date (dd/mm/YYYY, US Eastern),
Time (HH:MM ET), KickoffUTC (ISO-8601), MatchId (ESPN event id), Stage
(``regular`` / ``playoffs``), Status (ESPN status name), Home, Away (canonical
team names), HG, AG, Res (blank until played), HS, HST, AS, AST, HC, AC, HF,
AF, HPoss, APoss, AvgH, AvgD, AvgA (no bookmaker odds on ESPN: blank).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_FILES_DIR = Path(__file__).resolve().parent
_REGION_DIR = _FILES_DIR.parent
_SP_DIR = _REGION_DIR.parent.parent
for _p in (_SP_DIR, _SP_DIR / "shared", _SP_DIR / "Website"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from shared import sqlite_store as store  # noqa: E402
from competition_rules import canonical_team_name, make_match_sync_key  # noqa: E402

RAW_DATA_DIR = _REGION_DIR / "Data" / "Raw_Data"
ET = ZoneInfo("America/New_York")
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
FIRST_SEASON = 2021

# ESPN states / statuses
_COMPLETED_STATUSES = {
    "STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET", "STATUS_FINAL_PEN",
    "STATUS_END_OF_EXTRATIME", "STATUS_FINAL_ET",
}
_DROPPED_STATUSES = {
    "STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_ABANDONED",
    "STATUS_FORFEIT", "STATUS_DELAYED",
}

LEAGUES = {
    "United States/NWSL": {
        "slug": "usa.nwsl",
        "country": "USA",
        "league": "NWSL",
        "raw_dir": RAW_DATA_DIR / "United States" / "NWSL",
        "prefix": "nwsl",
        # Calendar-year season: (start month, end month) within the start year.
        "season_months": lambda y: [(y, m) for m in range(1, 13)],
        "season_of": lambda d: d.year,
    },
    "England/Women's Super League": {
        "slug": "eng.w.1",
        "country": "England",
        "league": "WSL",
        "raw_dir": RAW_DATA_DIR / "England" / "WSL",
        "prefix": "wsl",
        # Aug(start year) .. Jul(start year + 1); labelled by start year.
        "season_months": lambda y: [(y, m) for m in range(8, 13)] + [(y + 1, m) for m in range(1, 8)],
        "season_of": lambda d: d.year if d.month >= 8 else d.year - 1,
    },
}

RAW_COLUMNS = [
    "Country", "League", "Season", "Date", "Time", "KickoffUTC", "MatchId", "Stage", "Status",
    "Home", "Away", "HG", "AG", "Res", "HS", "HST", "AS", "AST", "HC", "AC", "HF", "AF",
    "HPoss", "APoss", "AvgH", "AvgD", "AvgA",
]


def current_season(competition: str, today: date | None = None) -> int:
    today = today or datetime.now(ET).date()
    return LEAGUES[competition]["season_of"](today)


def _http_get_json(client, url: str, params: dict, retries: int = 3) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json() or {}
            last_exc = RuntimeError(f"HTTP {resp.status_code} for {url} {params}")
        except Exception as exc:  # network hiccup
            last_exc = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"ESPN request failed: {last_exc}")


def fetch_season_events(competition: str, season: int, client=None) -> list[dict]:
    """All ESPN events of one season (deduplicated by event id)."""
    import httpx

    cfg = LEAGUES[competition]
    own_client = client is None
    client = client or httpx.Client(timeout=30, headers={"User-Agent": "BeyondTheStats/1.0"})
    events: dict[str, dict] = {}
    try:
        for year, month in cfg["season_months"](season):
            payload = _http_get_json(
                client, ESPN_SCOREBOARD.format(slug=cfg["slug"]),
                {"dates": f"{year}{month:02d}", "limit": 1000},
            )
            for ev in payload.get("events") or []:
                if ev.get("id"):
                    events[str(ev["id"])] = ev
    finally:
        if own_client:
            client.close()
    # Keep only events ESPN files under this season (a month query can return
    # the neighbouring season's games around the turn of the season).
    out = []
    for ev in events.values():
        try:
            kickoff = datetime.fromisoformat(str(ev.get("date", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if cfg["season_of"](kickoff.astimezone(ET).date()) == season:
            out.append(ev)
    out.sort(key=lambda e: (e.get("date") or "", str(e.get("id"))))
    return out


def _stat(competitor: dict, name: str):
    for s in competitor.get("statistics") or []:
        if s.get("name") == name:
            val = s.get("displayValue", s.get("value"))
            try:
                return float(val) if "." in str(val) else int(val)
            except (TypeError, ValueError):
                return ""
    return ""


def _canonical(name: str, competition: str) -> str:
    return canonical_team_name(name, competition) or str(name or "").strip()


def event_to_raw_row(ev: dict, competition: str, season: int) -> dict | None:
    """One raw CSV row for an ESPN event (None for postponed / cancelled games)."""
    cfg = LEAGUES[competition]
    comp = (ev.get("competitions") or [{}])[0]
    status = ((ev.get("status") or {}).get("type") or {})
    status_name = str(status.get("name") or "")
    state = str(status.get("state") or "")
    if status_name in _DROPPED_STATUSES:
        return None
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    kickoff = datetime.fromisoformat(str(ev.get("date", "")).replace("Z", "+00:00")).astimezone(timezone.utc)
    kickoff_et = kickoff.astimezone(ET)
    completed = state == "post" and (status_name in _COMPLETED_STATUSES or bool(status.get("completed")))
    if state == "post" and not completed:
        return None
    slug = str((ev.get("season") or {}).get("slug") or "")
    # NWSL slugs: "regular-season", "playoffs---quarterfinals", ...; WSL: "2026-27-english-womens-super-league".
    stage = "playoffs" if "playoff" in slug else "regular"

    row = {
        "Country": cfg["country"],
        "League": cfg["league"],
        "Season": str(season),
        "Date": kickoff_et.strftime("%d/%m/%Y"),
        "Time": kickoff_et.strftime("%H:%M"),
        "KickoffUTC": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "MatchId": str(ev.get("id") or ""),
        "Stage": stage,
        "Status": status_name,
        "Home": _canonical(home["team"].get("displayName", ""), competition),
        "Away": _canonical(away["team"].get("displayName", ""), competition),
        **{k: "" for k in RAW_COLUMNS if k not in (
            "Country", "League", "Season", "Date", "Time", "KickoffUTC", "MatchId", "Stage", "Status", "Home", "Away")},
    }
    if completed:
        try:
            hg, ag = int(home.get("score")), int(away.get("score"))
        except (TypeError, ValueError):
            return None
        row.update({
            "HG": hg, "AG": ag, "Res": "H" if hg > ag else ("A" if ag > hg else "D"),
            "HS": _stat(home, "totalShots"), "HST": _stat(home, "shotsOnTarget"),
            "AS": _stat(away, "totalShots"), "AST": _stat(away, "shotsOnTarget"),
            "HC": _stat(home, "wonCorners"), "AC": _stat(away, "wonCorners"),
            "HF": _stat(home, "foulsCommitted"), "AF": _stat(away, "foulsCommitted"),
            "HPoss": _stat(home, "possessionPct"), "APoss": _stat(away, "possessionPct"),
        })
    return row


def _atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def build_league_data(competition: str, first_season: int = FIRST_SEASON, client=None) -> dict:
    """Download every season of a league and write its raw CSVs.

    Returns {"seasons": {season: rows}, "current": season, "current_rows": [...]}.
    A failed or empty CURRENT season raises (never fall back to stale data);
    an empty past season is skipped.
    """
    cfg = LEAGUES[competition]
    current = current_season(competition)
    out: dict = {"seasons": {}, "current": current, "current_rows": []}
    for season in range(first_season, current + 1):
        try:
            events = fetch_season_events(competition, season, client=client)
        except Exception as exc:
            if season == current:
                raise
            print(f"[Womens_Data_Manager] {competition} {season}: fetch failed ({exc}); skipped")
            continue
        rows = [r for r in (event_to_raw_row(ev, competition, season) for ev in events) if r]
        if not rows:
            if season == current:
                raise RuntimeError(f"{competition}: ESPN returned no fixtures for current season {season}")
            continue
        df = pd.DataFrame(rows, columns=RAW_COLUMNS)
        path = cfg["raw_dir"] / f"{cfg['prefix']}stat{season}.csv"
        _atomic_write_csv(df, path)
        played = int((df["Res"] != "").sum())
        print(f"[Womens_Data_Manager] {competition} {season}: {len(df)} fixtures ({played} played) -> {path}")
        out["seasons"][season] = len(df)
        if season == current:
            out["current_rows"] = rows
    # Remove raw/processed files of seasons ESPN does not have (old generated files).
    keep = {f"{cfg['prefix']}stat{s}.csv" for s in out["seasons"]}
    for folder in (cfg["raw_dir"], Path(str(cfg["raw_dir"]).replace("Raw_Data", "Processed_Data"))):
        if folder.is_dir():
            for f in folder.glob(f"{cfg['prefix']}stat*.csv"):
                if f.name not in keep:
                    f.unlink()
                    print(f"[Womens_Data_Manager] removed stale {f}")
    return out


def raw_row_to_past_row(row: dict, competition: str) -> dict:
    date_iso = datetime.strptime(row["Date"], "%d/%m/%Y").strftime("%Y-%m-%d")
    home, away = row["Home"], row["Away"]
    return {
        "match_id": row["MatchId"],
        "match_date": date_iso,
        "match_date_iso": date_iso,
        "match_datetime_utc": row["KickoffUTC"],
        "competition": competition,
        "home_team": home,
        "away_team": away,
        "canonical_home_team": home,
        "canonical_away_team": away,
        "sync_key": make_match_sync_key(date_iso, competition, home, away),
        "actual_home_goals": int(row["HG"]),
        "actual_away_goals": int(row["AG"]),
        "actual_result": row["Res"],
        "stage": row.get("Stage", "regular"),
        "has_prediction": False,
        "schedule_only": "1",
        "prediction_quality": "no_prediction",
        "source": "womens_espn",
        "settled_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _existing_past_match_ids(competition_aliases: set[str]) -> set[str]:
    conn = store._open_ready(None)
    try:
        q = ",".join("?" * len(competition_aliases))
        rows = conn.execute(
            f"SELECT json_extract(payload_json, '$.match_id') AS mid FROM past_games "
            f"WHERE competition IN ({q})", tuple(competition_aliases),
        ).fetchall()
    finally:
        conn.close()
    return {str(r["mid"]) for r in rows if r["mid"]}


def sync_current_season_results(competition: str, rows: list[dict]) -> int:
    """Upsert the current season's completed real results into past_games.

    Games the live poller already wrote (same ESPN match_id, any competition
    label) are left alone so its pre-match prediction stays attached.
    """
    import config

    aliases = set(config.competition_live_aliases(competition)) | {competition}
    resolved = config.resolve_live_competition(competition)
    if resolved:
        aliases |= set(config.competition_live_aliases(resolved)) | {resolved}
    existing = _existing_past_match_ids(aliases)
    past_rows = [
        raw_row_to_past_row(r, competition)
        for r in rows
        if r.get("Res") in ("H", "D", "A") and str(r.get("MatchId")) not in existing
    ]
    if not past_rows:
        return 0
    res = store.upsert_past_games(past_rows)
    return int(res.get("upserted", len(past_rows)) or 0)


def mark_playoff_rows(rows: list[dict]) -> int:
    """Stamp ``stage = 'playoffs'`` on stored copies of playoff games.

    The live poller writes finals before it knew the ESPN season slug, so its
    past_games / live_score_history rows have no stage; league tables (NWSL
    Shield) must not count them.
    """
    playoff_ids = sorted({str(r["MatchId"]) for r in rows if r.get("Stage") == "playoffs" and r.get("MatchId")})
    if not playoff_ids:
        return 0
    marked = 0
    q = ",".join("?" * len(playoff_ids))
    with store._WRITE_LOCK:
        conn = store._open_ready(None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table, id_expr in (
                ("past_games", "json_extract(payload_json, '$.match_id')"),
                ("live_score_history", "match_id"),
            ):
                marked += conn.execute(
                    f"UPDATE {table} SET payload_json = json_set(payload_json, '$.stage', 'playoffs') "
                    f"WHERE {id_expr} IN ({q}) "
                    "AND coalesce(json_extract(payload_json, '$.stage'), '') != 'playoffs'",
                    tuple(playoff_ids),
                ).rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return marked


_WOMENS_COMPETITION_LABELS = (
    "United States/NWSL", "North America/NWSL", "England/Women's Super League", "England/WSL",
)


def purge_generated_rows() -> dict:
    """Delete rows produced by the old synthetic generator.

    Generated past_games rows have ``source = 'womens'`` (real ones written
    here use ``womens_espn``); generated upcoming rows have ``source =
    'womens'`` and no ``kickoff_utc``. Idempotent: a no-op once clean.

    past_games: generated results; upcoming_games: predictions for generated
    fixtures (Predict_Upcoming_Matchweek rewrites the real ones);
    league_projection_history: projections computed from generated tables
    (only cleared in the run that finds generated results, i.e. once).
    """
    removed = {"past_games": 0, "upcoming_games": 0, "league_projection_history": 0}
    with store._WRITE_LOCK:
        conn = store._open_ready(None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            removed["past_games"] = conn.execute(
                "DELETE FROM past_games WHERE json_extract(payload_json, '$.source') = 'womens'"
            ).rowcount
            # Real predictions keep source 'womens' (the mode label the API
            # reads) but always carry the ESPN kickoff_utc; generated ones never did.
            removed["upcoming_games"] = conn.execute(
                "DELETE FROM upcoming_games WHERE json_extract(payload_json, '$.source') = 'womens' "
                "AND coalesce(json_extract(payload_json, '$.kickoff_utc'), '') = ''"
            ).rowcount
            q = ",".join("?" * len(_WOMENS_COMPETITION_LABELS))
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if removed["past_games"] and "league_projection_history" in tables:
                removed["league_projection_history"] = conn.execute(
                    f"DELETE FROM league_projection_history WHERE competition IN ({q})",
                    _WOMENS_COMPETITION_LABELS,
                ).rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return removed


def update_roster_seeds(rosters: dict[str, list[str]]) -> None:
    """Keep the app's team catalog rosters equal to the real current season."""
    import config

    for path in (config.LEAGUE_TEAMS_FILE, config.CURRENT_SEASON_TEAMS_FILE):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw_text = fh.read()
            data = json.loads(raw_text)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        for comp, teams in rosters.items():
            if teams and data.get(comp) != teams:
                data[comp] = teams
                changed = True
        if changed:
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                if raw_text.endswith("\n"):
                    fh.write("\n")
            os.replace(tmp, path)
            print(f"[Womens_Data_Manager] updated rosters in {path}")


def main(first_season: int = FIRST_SEASON) -> dict:
    print("=== Womens Data Manager: downloading real NWSL / WSL data from ESPN ===")
    store.ensure_store()
    purged = purge_generated_rows()
    if any(purged.values()):
        print(f"[Womens_Data_Manager] removed generated rows: {purged}")

    summary: dict = {"purged": purged, "leagues": {}}
    rosters: dict[str, list[str]] = {}
    for competition in LEAGUES:
        result = build_league_data(competition, first_season=first_season)
        synced = sync_current_season_results(competition, result["current_rows"])
        marked = mark_playoff_rows(result["current_rows"])
        if marked:
            print(f"[Womens_Data_Manager] {competition}: marked {marked} stored playoff rows")
        regular = [r for r in result["current_rows"] if r.get("Stage") == "regular"]
        rosters[competition] = sorted({r["Home"] for r in regular} | {r["Away"] for r in regular})
        summary["leagues"][competition] = {
            "seasons": result["seasons"], "current": result["current"],
            "past_games_synced": synced, "teams": len(rosters[competition]),
        }
        print(f"[Womens_Data_Manager] {competition}: synced {synced} results; {len(rosters[competition])} teams")
    update_roster_seeds(rosters)
    print("=== Womens Data Manager complete ===")
    return summary


if __name__ == "__main__":
    main()
