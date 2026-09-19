"""Unified ``/api/cup-data`` response builder.

Mirrors ``league_data.py`` closely so frontend parsers can reuse the same
shapes, with cup-specific additions/removals driven by ``format.format_style``:

- ``knockout`` — no table; winner + stage reach position odds
- ``table_knockout`` — league/dual phase table + knockout finish odds (UEFA, Leagues Cup)
- ``group_knockout`` — group tables + knockout finish odds
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from datetime import datetime, timezone

import config
from competition_rules import (
    CUP_FORMAT_STYLE_GROUP_KNOCKOUT,
    CUP_FORMAT_STYLE_KNOCKOUT,
    CUP_FORMAT_STYLE_TABLE_KNOCKOUT,
    CUP_POSITION_STAGE_FINAL,
    CUP_POSITION_STAGE_WINNER,
    competition_format_spec,
    cup_format,
    cup_format_style_for,
    cup_position_stages_for,
    is_cup_competition,
    resolve_competition_query,
    standings_layout_for,
    STANDINGS_LAYOUT_KNOCKOUT,
    STANDINGS_LAYOUT_LEAGUES_CUP,
    _normalize_cup_stage_key,
)
from knockout import (
    _build_cup_knockout_payload,
    _build_knockout_framework,
    _enrich_league_data_cup_fields,
    _gather_competition_cup_matches,
)
from league_data import (
    _build_bracket_section,
    _build_position_odds,
    _load_fixtures,
    _load_predicted_groups,
    _load_real_standings,
    _load_usable_projected_table,
    _slugify_competition,
)
from predictions import _load_json_payload
from standings import _dedupe_standings_groups


_CUP_DATA_MEM: dict[str, tuple[float, dict]] = {}
_CUP_DATA_MEM_LOCK = threading.Lock()
_CUP_DATA_BUILD_LOCKS: dict[str, threading.Lock] = {}
_CUP_DATA_BUILD_LOCKS_GUARD = threading.Lock()
# mtime cache for real_cup_tables.csv (pandas parse is the other cold-path cost).
_REAL_CUP_TABLE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_REAL_CUP_TABLE_CACHE_LOCK = threading.Lock()


def _cup_data_cache_path(comp_name: str) -> str:
    slug = _slugify_competition(comp_name)
    return os.path.join(config.CUP_DATA_DIR, f"{slug}.json")


def _cup_data_ttl_seconds() -> float:
    return float(getattr(config, "CACHE_TTL_LONG", 600))


def _mem_get_cup_data(comp_name: str) -> dict | None:
    now = time.time()
    with _CUP_DATA_MEM_LOCK:
        entry = _CUP_DATA_MEM.get(comp_name)
        if not entry:
            return None
        expires_at, payload = entry
        if now >= expires_at:
            _CUP_DATA_MEM.pop(comp_name, None)
            return None
        return payload


def _mem_set_cup_data(comp_name: str, payload: dict) -> None:
    expires_at = time.time() + _cup_data_ttl_seconds()
    with _CUP_DATA_MEM_LOCK:
        _CUP_DATA_MEM[comp_name] = (expires_at, payload)


def _build_lock_for(comp_name: str) -> threading.Lock:
    with _CUP_DATA_BUILD_LOCKS_GUARD:
        lock = _CUP_DATA_BUILD_LOCKS.get(comp_name)
        if lock is None:
            lock = threading.Lock()
            _CUP_DATA_BUILD_LOCKS[comp_name] = lock
        return lock


def _load_cup_data_from_cache(comp_name: str) -> dict | None:
    mem = _mem_get_cup_data(comp_name)
    if mem is not None:
        if _cup_data_cache_is_stale_undersimmed(comp_name, mem):
            with _CUP_DATA_MEM_LOCK:
                _CUP_DATA_MEM.pop(comp_name, None)
        else:
            return mem
    path = _cup_data_cache_path(comp_name)
    if not os.path.exists(path):
        return None
    try:
        age = datetime.now(timezone.utc).timestamp() - os.path.getmtime(path)
        if age > _cup_data_ttl_seconds():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            return None
        if _cup_data_cache_is_stale_undersimmed(comp_name, payload):
            return None
        _mem_set_cup_data(comp_name, payload)
        return payload
    except Exception:
        return None


def _cup_data_cache_is_stale_undersimmed(comp_name: str, payload: dict) -> bool:
    """True when cached cup table rows are sticky live-only (sim_runs <= 1).

    After empty upcoming + missing pending seed, Track wrote ``sim_runs=1``
    placeholders that stuck in CupData forever. Treat them as a cache miss so
    the next request rebuilds once real Monte Carlo rows land.
    """
    if not isinstance(payload, dict):
        return True
    table = ((payload.get("predicted") or {}).get("table")) or []
    if not table:
        return False
    try:
        return all(float(r.get("sim_runs") or 0) <= 1 for r in table)
    except Exception:
        return True


def _write_cup_data_cache(comp_name: str, payload: dict) -> None:
    """Publish to process memory immediately; persist compact JSON in the background.

    League-data keeps a process-local mem cache in front of disk. Cup payloads
    are large (knockout + odds), so blocking the API on ``json.dump`` made cold
    ``/api/cup-data`` noticeably slower than ``/api/league-data``.
    """
    if isinstance(payload, dict):
        _mem_set_cup_data(comp_name, payload)

    path = _cup_data_cache_path(comp_name)

    def _persist() -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception:
            pass

    threading.Thread(target=_persist, name=f"cup-data-cache:{comp_name}", daemon=True).start()


def clear_cup_data_caches() -> int:
    """Drop in-memory and on-disk CupData caches."""
    with _CUP_DATA_MEM_LOCK:
        _CUP_DATA_MEM.clear()
    with _REAL_CUP_TABLE_CACHE_LOCK:
        _REAL_CUP_TABLE_CACHE.clear()
    try:
        from predictions import clear_json_payload_cache

        clear_json_payload_cache()
    except Exception:
        pass
    removed = 0
    cache_dir = getattr(config, "CUP_DATA_DIR", "") or ""
    if cache_dir and os.path.isdir(cache_dir):
        for name in os.listdir(cache_dir):
            if not name.endswith(".json"):
                continue
            try:
                os.remove(os.path.join(cache_dir, name))
                removed += 1
            except OSError:
                pass
    return removed


def warm_cup_data_mem_from_disk() -> int:
    """Load fresh on-disk CupData JSON into the process mem cache (gunicorn warm)."""
    cache_dir = getattr(config, "CUP_DATA_DIR", "") or ""
    if not cache_dir or not os.path.isdir(cache_dir):
        return 0
    ttl = _cup_data_ttl_seconds()
    now = datetime.now(timezone.utc).timestamp()
    loaded = 0
    for name in os.listdir(cache_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cache_dir, name)
        try:
            age = now - os.path.getmtime(path)
            if age > ttl:
                continue
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                continue
            comp = str(payload.get("competition") or "").strip()
            if not comp:
                continue
            _mem_set_cup_data(comp, payload)
            loaded += 1
        except Exception:
            continue
    return loaded


def cup_data_competitions() -> list[str]:
    """Known cup competitions for listing / rebuild."""
    comps = set(getattr(config, "CUP_COMPETITIONS", set()) or set())
    comps.update(getattr(config, "_CUP_FORMATS", {}) or {})
    comps.discard("International/World Cup")  # dedicated WC APIs for now
    return sorted(c for c in comps if c and is_cup_competition(c))


def rebuild_cup_data_caches(
    competitions: list[str] | None = None,
    *,
    clear_first: bool = True,
    max_workers: int = 4,
) -> dict[str, bool]:
    """Clear (optional) and rebuild CupData caches after pipeline publish.

    Mirrors ``rebuild_league_data_caches``: warm the competition-games index and
    drop sticky real-standings caches so table cups do not wait on cold history
    scans after publish.
    """
    if clear_first:
        removed = clear_cup_data_caches()
        print(f"[cup-data] cleared caches ({removed} disk files)")

    try:
        from competition_rules import warm_competition_games_cache

        warm_competition_games_cache(force=True)
    except Exception as exc:
        print(f"[cup-data] games-cache warm failed: {exc}")

    try:
        from standings import _clear_all_real_data_caches

        _clear_all_real_data_caches()
    except Exception:
        pass

    comps = list(competitions) if competitions is not None else cup_data_competitions()
    if not comps:
        return {}
    results: dict[str, bool] = {}
    workers = max(1, min(int(max_workers or 1), 8))

    def _one(comp: str) -> tuple[str, bool]:
        try:
            _build_cup_data_payload_uncached(comp)
            return comp, True
        except Exception as exc:
            print(f"[cup-data] rebuild failed for {comp}: {exc}")
            return comp, False

    print(f"[cup-data] rebuilding {len(comps)} competition cache(s) (workers={workers})")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for comp, ok in pool.map(_one, comps):
            results[comp] = ok
    passed = sum(1 for ok in results.values() if ok)
    print(f"[cup-data] rebuild done: {passed}/{len(results)} ok")
    return results


def _cup_format_block(comp_name: str) -> dict:
    """Cup-oriented format block (league-data format + format_style)."""
    base = competition_format_spec(comp_name)
    style = cup_format_style_for(comp_name) or CUP_FORMAT_STYLE_KNOCKOUT
    cup_fmt = cup_format(comp_name) or {}
    stages = list(cup_fmt.get("stages") or [])
    ko_rounds = list(cup_fmt.get("knockout_rounds") or [])
    position_stages = cup_position_stages_for(comp_name)
    has_table = style in {CUP_FORMAT_STYLE_TABLE_KNOCKOUT, CUP_FORMAT_STYLE_GROUP_KNOCKOUT}
    has_groups = style == CUP_FORMAT_STYLE_GROUP_KNOCKOUT or (
        style == CUP_FORMAT_STYLE_TABLE_KNOCKOUT
        and standings_layout_for(comp_name) == STANDINGS_LAYOUT_LEAGUES_CUP
    )
    draw_rules = {
        "two_leg_rounds": list(cup_fmt.get("two_leg_rounds") or []),
        "final_neutral": bool(cup_fmt.get("final_neutral")),
        "no_draws": bool(cup_fmt.get("no_draws")),
        "draw_type": cup_fmt.get("draw_type"),
        "knockout_seedings": cup_fmt.get("knockout_seedings"),
        "advance_per_table": cup_fmt.get("advance_per_table"),
    }
    # Drop empty draw fields for cleaner payloads.
    draw_rules = {k: v for k, v in draw_rules.items() if v not in (None, [], False)}

    out = dict(base)
    out.update({
        "competition_type": "cup",
        "format_style": style,
        "has_table": has_table,
        "has_groups": has_groups,
        "stages": stages,
        "knockout_rounds": ko_rounds or stages,
        "position_stages": position_stages,
        "draw_rules": draw_rules,
    })
    notes = list(out.get("notes") or [])
    if style == CUP_FORMAT_STYLE_KNOCKOUT:
        notes.append(
            "Pure knockout cup: no league table. predicted.position_odds are "
            "stage-reach odds (Winner / Final / SF / QF / …), not table places."
        )
    elif style == CUP_FORMAT_STYLE_TABLE_KNOCKOUT:
        notes.append(
            "Table/league-phase then knockout. Table position_odds cover the "
            "phase table; predicted.position_odds also expose knockout stage-reach odds."
        )
    elif style == CUP_FORMAT_STYLE_GROUP_KNOCKOUT:
        notes.append(
            "Group stage then knockout. Group tables plus knockout stage-reach odds."
        )
    out["notes"] = notes
    return out


def _load_projected_cup_entry(comp_name: str) -> dict:
    data = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    if not isinstance(data, dict):
        return {}
    comps = data.get("competitions", data)
    if not isinstance(comps, dict):
        return {}
    base_comp, _ = resolve_competition_query(comp_name)
    entry = comps.get(comp_name) or comps.get(base_comp) or {}
    return entry if isinstance(entry, dict) else {}


def _pct_map_to_100(raw: dict) -> dict[str, float]:
    """Normalize a team→prob map to percentages summing ~100."""
    cleaned: dict[str, float] = {}
    for team, val in (raw or {}).items():
        name = str(team or "").strip()
        if not name:
            continue
        try:
            pct = float(val)
        except (TypeError, ValueError):
            continue
        # Bracket sims store fractions (0-1) or already percentages.
        if 0.0 <= pct <= 1.5:
            pct *= 100.0
        cleaned[name] = pct
    total = sum(cleaned.values())
    if total <= 0:
        return {}
    if abs(total - 100.0) > 1.0:
        scale = 100.0 / total
        cleaned = {t: round(p * scale, 2) for t, p in cleaned.items()}
    else:
        cleaned = {t: round(p, 2) for t, p in cleaned.items()}
    return cleaned


def _elim_pct_map(elim_entry: dict) -> dict[str, float]:
    """Normalize one team's elimination-round map to stable stage → pct."""
    out: dict[str, float] = {}
    if not isinstance(elim_entry, dict):
        return out
    for rnd, frac in elim_entry.items():
        key = _normalize_cup_stage_key(rnd)
        if not key:
            # "Champion" often means won the cup (terminal finish = Winner).
            text = str(rnd or "").strip().lower()
            if text in {"champion", "winner", "win"}:
                key = CUP_POSITION_STAGE_WINNER
            else:
                continue
        try:
            pct = float(frac)
        except (TypeError, ValueError):
            continue
        if 0.0 <= pct <= 1.5:
            pct *= 100.0
        out[key] = max(out.get(key, 0.0), pct)
    return out


def _most_likely_cup_finish(
    team: str,
    odds: dict[str, float],
    stages: list[str],
    elim_by_team: dict[str, dict[str, float]],
) -> tuple[str, float]:
    """Pick most likely terminal finish (not max reach — reach is nested).

    Prefer elimination-round mass when present; otherwise infer from nested
    reach diffs (reach[stage] − reach[deeper]).
    """
    elim = elim_by_team.get(team) or {}
    if elim:
        best_stage, best_pct = max(elim.items(), key=lambda kv: kv[1])
        if best_pct > 0:
            return best_stage, round(best_pct, 2)

    # Infer terminal mass from nested reach: P(finish at stage) ≈
    # reach[stage] − reach[next_deeper], with Winner = reach[Winner].
    best_stage = stages[-1] if stages else CUP_POSITION_STAGE_WINNER
    best_pct = 0.0
    for idx, stage in enumerate(stages):
        reach = float(odds.get(stage, 0.0) or 0.0)
        deeper = 0.0
        if idx > 0:
            # stages are Winner → Final → SF → … (deeper first)
            # For idx>0, "deeper" finish is the previous (earlier in list) stage.
            deeper = float(odds.get(stages[idx - 1], 0.0) or 0.0)
        terminal = max(0.0, reach - deeper) if idx > 0 else reach
        if terminal >= best_pct:
            best_pct = terminal
            best_stage = stage
    return best_stage, round(best_pct, 2)


def _build_cup_stage_position_odds(comp_name: str, entry: dict) -> dict:
    """Build league-style position_odds from cup simulation reach/winner maps.

    Stages are cup-specific (Winner, Final, SF, QF, RO16, …). Values are the
    likelihood a team *reaches* that stage (Winner = lift the trophy).
    """
    stages = cup_position_stages_for(comp_name)
    winner_probs = _pct_map_to_100(entry.get("winner_probabilities") or {})

    # Aggregate round_reach onto stable keys.
    reach_by_stage: dict[str, dict[str, float]] = {s: {} for s in stages}
    if CUP_POSITION_STAGE_WINNER in reach_by_stage:
        reach_by_stage[CUP_POSITION_STAGE_WINNER] = dict(winner_probs)

    raw_reach = entry.get("round_reach_probabilities") or {}
    if isinstance(raw_reach, dict):
        for rnd, team_map in raw_reach.items():
            key = _normalize_cup_stage_key(rnd)
            if not key or key not in reach_by_stage:
                continue
            reach_by_stage[key] = _pct_map_to_100(team_map if isinstance(team_map, dict) else {})

    raw_elim = entry.get("elimination_round_probabilities") or entry.get("elimination_round_odds") or {}
    elim_by_team: dict[str, dict[str, float]] = {}
    if isinstance(raw_elim, dict):
        for team, rounds in raw_elim.items():
            name = str(team or "").strip()
            if not name:
                continue
            elim_by_team[name] = _elim_pct_map(rounds if isinstance(rounds, dict) else {})

    # Final reach ≈ teams that appear in Final round_reach; if missing, use
    # champion + runner-up proxies from elimination "Champion"/"Final".
    if CUP_POSITION_STAGE_FINAL in reach_by_stage and not reach_by_stage[CUP_POSITION_STAGE_FINAL]:
        final_reach: dict[str, float] = dict(winner_probs)
        for team, rounds in elim_by_team.items():
            if CUP_POSITION_STAGE_FINAL in rounds:
                final_reach[team] = max(
                    final_reach.get(team, 0.0),
                    rounds[CUP_POSITION_STAGE_FINAL],
                )
            # Teams that win also reached the final.
            if CUP_POSITION_STAGE_WINNER in rounds:
                final_reach[team] = max(
                    final_reach.get(team, 0.0),
                    rounds[CUP_POSITION_STAGE_WINNER],
                )
        reach_by_stage[CUP_POSITION_STAGE_FINAL] = final_reach or dict(winner_probs)

    # detailed: per-team odds dict across stages
    teams: set[str] = set()
    for stage_map in reach_by_stage.values():
        teams.update(stage_map.keys())
    teams.update(elim_by_team.keys())

    detailed: list[dict] = []
    for team in sorted(teams):
        odds = {}
        for stage in stages:
            odds[stage] = float(reach_by_stage.get(stage, {}).get(team, 0.0) or 0.0)
        most_stage, most_pct = _most_likely_cup_finish(team, odds, stages, elim_by_team)
        detailed.append({
            "team": team,
            "odds": odds,
            "most_likely_position": most_stage,
            "most_likely_position_pct": most_pct,
        })

    simple: dict[str, list] = {}
    for stage in stages:
        rows = [
            {"team": team, "pct": float(reach_by_stage.get(stage, {}).get(team, 0.0) or 0.0)}
            for team in teams
            if float(reach_by_stage.get(stage, {}).get(team, 0.0) or 0.0) > 0
        ]
        rows.sort(key=lambda r: r["pct"], reverse=True)
        simple[stage] = rows

    return {
        "simple": simple,
        "detailed": detailed,
        "detailed_same_as_simple": True,
        "stages": stages,
        "semantics": "reach",  # pct = likelihood of reaching the stage (Winner = champion)
    }


def _build_cup_winners_odds(winner_probs: dict[str, float], position_odds: dict) -> list[dict]:
    detailed = {
        str(row.get("team")): row
        for row in (position_odds.get("detailed") or [])
        if row.get("team")
    }
    rows = []
    for team, pct in sorted(winner_probs.items(), key=lambda x: -x[1]):
        name = str(team or "").strip()
        if not name or name.upper() in {"NONE", "DRAW", "TBD", "TIE"}:
            continue
        detail = detailed.get(name) or detailed.get(team) or {}
        odds = detail.get("odds") or {}
        rows.append({
            "team": name,
            "win_cup_pct": round(float(pct), 2),
            # league-data alias for easier frontend reuse
            "win_league_pct": round(float(pct), 2),
            "final_pct": round(float(odds.get(CUP_POSITION_STAGE_FINAL, 0.0) or 0.0), 2),
            "sf_pct": round(float(odds.get("SF", 0.0) or 0.0), 2),
            "qf_pct": round(float(odds.get("QF", 0.0) or 0.0), 2),
            "most_likely_position": detail.get("most_likely_position"),
            "most_likely_position_pct": detail.get("most_likely_position_pct"),
            "stage_odds": odds,
        })
    return rows


def _condensed_winners_odds(winners_odds: list[dict]) -> list[dict]:
    """League-data-style condensed winner list: team + pct only."""
    out = []
    for row in winners_odds or []:
        team = str(row.get("team") or "").strip()
        if not team or team.upper() in {"NONE", "DRAW", "TBD", "TIE"}:
            continue
        pct = row.get("win_cup_pct")
        if pct is None:
            pct = row.get("win_league_pct")
        try:
            pct_f = round(float(pct or 0), 2)
        except (TypeError, ValueError):
            pct_f = 0.0
        if pct_f <= 0:
            continue
        out.append({"team": team, "pct": pct_f, "win_cup_pct": pct_f, "win_league_pct": pct_f})
    return out


def _load_real_cup_table_rows(comp_name: str) -> list[dict]:
    """Load live/real phase table rows written by Track_Cup_Results.

    Memoized by CSV mtime (same pattern as projected-table CSV caching in
    ``predictions._load_projected_tables``).
    """
    path = getattr(config, "CUP_REAL_TABLE_FILE", "") or ""
    if not path or not os.path.exists(path):
        return []
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    base_comp, _ = resolve_competition_query(comp_name)
    cache_key = f"{os.path.normpath(path)}|{comp_name}|{base_comp}"
    with _REAL_CUP_TABLE_CACHE_LOCK:
        cached = _REAL_CUP_TABLE_CACHE.get(cache_key)
        if cached is not None and cached[0] == mtime:
            return list(cached[1])
    try:
        import pandas as pd
        df = pd.read_csv(path)
    except Exception:
        return []
    if df is None or df.empty or "competition" not in df.columns:
        return []
    mask = df["competition"].astype(str).str.strip().isin({comp_name, base_comp})
    rows = df.loc[mask].to_dict("records")
    if not isinstance(rows, list):
        rows = []
    with _REAL_CUP_TABLE_CACHE_LOCK:
        _REAL_CUP_TABLE_CACHE[cache_key] = (mtime, rows)
    return list(rows)


def _real_cup_standings_from_rows(comp_name: str, rows: list[dict]) -> dict | None:
    """Shape real cup table CSV rows into league-data standings groups."""
    if not rows:
        return None
    layout = standings_layout_for(comp_name)
    if layout == STANDINGS_LAYOUT_LEAGUES_CUP:
        from competition_rules import leagues_cup_table_side, LEAGUES_CUP_TABLE_MLS, LEAGUES_CUP_TABLE_LIGA_MX
        groups = {LEAGUES_CUP_TABLE_MLS: [], LEAGUES_CUP_TABLE_LIGA_MX: []}
        for row in rows:
            side = leagues_cup_table_side(str(row.get("team") or ""))
            if side in groups:
                groups[side].append(row)
        return {
            "groups": [
                {"name": name, "entries": sorted(entries, key=lambda r: int(r.get("position") or 999))}
                for name, entries in groups.items() if entries
            ]
        }
    return {
        "groups": [{
            "name": "League Phase" if layout == "league_phase" else "Overall",
            "entries": sorted(rows, key=lambda r: int(r.get("position") or 999)),
        }]
    }


def build_cup_data_payload(comp_name: str) -> dict:
    """Build the canonical cup-data API payload for one cup competition."""
    comp = str(comp_name or "").strip()
    if not is_cup_competition(comp):
        return {
            "ok": False,
            "error": f"Not a cup competition: {comp}",
            "competition": comp,
        }

    cached = _load_cup_data_from_cache(comp)
    if cached is not None:
        return cached

    lock = _build_lock_for(comp)
    with lock:
        cached = _load_cup_data_from_cache(comp)
        if cached is not None:
            return cached
        return _build_cup_data_payload_uncached(comp)


def _build_cup_data_payload_uncached(comp: str) -> dict:
    fmt = _cup_format_block(comp)
    style = fmt.get("format_style") or CUP_FORMAT_STYLE_KNOCKOUT
    include_table = bool(fmt.get("has_table"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        f_table = pool.submit(lambda: _load_usable_projected_table(comp) if include_table else [])
        f_standings = pool.submit(lambda: _load_real_standings(comp) if include_table else None)
        f_real_cup = pool.submit(lambda: _load_real_cup_table_rows(comp) if include_table else [])
        f_bracket = pool.submit(_build_bracket_section, comp)
        f_fixtures = pool.submit(_load_fixtures, comp)
        comp_table = f_table.result() or []
        real_standings = f_standings.result()
        real_cup_rows = f_real_cup.result() or []
        bracket = f_bracket.result() or {}
        fixtures = f_fixtures.result() or []

    # Prefer Track_Cup_Results live phase table when present.
    if include_table and real_cup_rows:
        shaped = _real_cup_standings_from_rows(comp, real_cup_rows)
        if shaped:
            real_standings = shaped

    predicted_table: list[dict] = list(comp_table) if include_table else []
    if include_table and predicted_table:
        fake = {
            "groups": [{"name": "Overall", "entries": [
                {**row, "team": row.get("team"), "P": row.get("P") or row.get("played") or 0}
                for row in predicted_table
            ]}]
        }
        deduped = _dedupe_standings_groups(fake, comp)
        entries = (deduped.get("groups") or [{}])[0].get("entries") or []
        by_team = {str(r.get("team", "")).strip(): r for r in predicted_table}
        predicted_table = []
        for entry in entries:
            team = str(entry.get("team", "")).strip()
            base = dict(by_team.get(team) or entry)
            base["team"] = team
            predicted_table.append(base)

    predicted_groups = None
    table_position_odds = {"simple": {}, "detailed": [], "detailed_same_as_simple": True}
    if include_table:
        predicted_groups = _load_predicted_groups(comp, predicted_table, real_standings=real_standings)
        table_position_odds = _build_position_odds(predicted_table)

    entry = {}
    # Prefer bracket.projected (already loaded in _build_bracket_section) so we
    # do not re-parse projected_cup_brackets.json.
    projected = bracket.get("projected") if isinstance(bracket.get("projected"), dict) else None
    if isinstance(projected, dict) and projected:
        entry = dict(projected)
    else:
        entry = _load_projected_cup_entry(comp)

    stage_position_odds = _build_cup_stage_position_odds(comp, entry)
    winner_probs = _pct_map_to_100(entry.get("winner_probabilities") or {})
    winner_probs = {
        t: p for t, p in winner_probs.items()
        if t and str(t).upper() not in {"NONE", "DRAW", "TBD", "TIE"}
    }
    if not winner_probs:
        # Fall back to Winner column of stage odds.
        for row in stage_position_odds.get("simple", {}).get(CUP_POSITION_STAGE_WINNER, []):
            team = str(row.get("team") or "").strip()
            if team and team.upper() not in {"NONE", "DRAW", "TBD", "TIE"}:
                winner_probs[team] = float(row.get("pct") or 0)

    champion = entry.get("champion")
    if champion and str(champion).upper() in {"NONE", "DRAW", "TBD", "TIE"}:
        champion = None
    if not champion and winner_probs:
        champion = max(winner_probs, key=lambda t: winner_probs[t])
    simulations_run = entry.get("simulations_run") or 0

    winners_odds = _build_cup_winners_odds(winner_probs, stage_position_odds)
    winners_odds_simple = _condensed_winners_odds(winners_odds)

    # For pure knockout: position_odds = stage reach odds.
    # For table/group cups: keep table position_odds under predicted.table_position_odds
    # and put stage-reach odds in predicted.position_odds (primary cup analog).
    predicted = {
        "table": predicted_table if include_table else [],
        "groups": predicted_groups if include_table else None,
        "winner": {
            "champion": champion,
            "probabilities": winner_probs,
            "simulations_run": simulations_run,
        },
        "winners_odds": winners_odds,
        "winners_odds_simple": winners_odds_simple,
        "position_odds": stage_position_odds,
    }
    if include_table:
        predicted["table_position_odds"] = {
            "simple": table_position_odds.get("simple", {}),
            "detailed": table_position_odds.get("detailed", []),
            "detailed_same_as_simple": table_position_odds.get("detailed_same_as_simple", True),
        }

    payload: dict = {
        "ok": True,
        "competition": comp,
        "format": fmt,
        "predicted": predicted,
        "real": {"standings": real_standings if include_table else None},
        "bracket": bracket,
        "fixtures": fixtures,
        # Flat aliases matching league-data for easier frontend reuse.
        "predicted_table": predicted_table if include_table else [],
        "position_odds": {
            "simple": stage_position_odds.get("simple", {}),
            "detailed": stage_position_odds.get("detailed", []),
            "stages": stage_position_odds.get("stages", []),
            "semantics": stage_position_odds.get("semantics", "reach"),
        },
        "winners_odds": winners_odds,
        "winners_odds_simple": winners_odds_simple,
        "real_table": real_standings if include_table else None,
        "champion": champion,
        "winner_probabilities": winner_probs,
        "simulations_run": simulations_run,
    }

    if entry.get("elimination_round_probabilities") or entry.get("elimination_round_odds"):
        payload["elimination_round_odds"] = (
            entry.get("elimination_round_probabilities")
            or entry.get("elimination_round_odds")
        )
        predicted["elimination_round_odds"] = payload["elimination_round_odds"]
    if entry.get("round_reach_probabilities"):
        payload["round_reach_probabilities"] = entry["round_reach_probabilities"]
        predicted["round_reach_probabilities"] = entry["round_reach_probabilities"]

    # Enrich only when bracket section did not already build knockout maps —
    # otherwise we re-scan competition history a second time (cold-path cost).
    if not bracket.get("knockout"):
        enriched = _enrich_league_data_cup_fields(comp, dict(payload))
        for key in ("knockout", "odds_knockout", "real_knockout"):
            if enriched.get(key):
                bracket[key] = enriched[key]
                payload[key] = enriched[key]
        if enriched.get("winner_probabilities") and not winner_probs:
            cleaned = {
                t: p for t, p in (enriched.get("winner_probabilities") or {}).items()
                if t and str(t).upper() not in {"NONE", "DRAW", "TBD", "TIE"}
            }
            payload["winner_probabilities"] = cleaned
            predicted["winner"]["probabilities"] = cleaned
        if enriched.get("champion") and not champion:
            champ = enriched["champion"]
            if champ and str(champ).upper() not in {"NONE", "DRAW", "TBD", "TIE"}:
                payload["champion"] = champ
                predicted["winner"]["champion"] = champ

    # Prefer Track-authored real_knockout / upcoming match odds when present.
    for key in ("real_knockout", "projected_knockout", "upcoming_fixtures", "rounds"):
        if entry.get(key) and not bracket.get(key):
            bracket[key] = entry[key]
    if entry.get("real_knockout"):
        # Keep Track real bracket even when live gather also ran.
        bracket["real_knockout"] = entry["real_knockout"]
        payload["real_knockout"] = entry["real_knockout"]
        predicted["real_knockout"] = entry["real_knockout"]
    if payload.get("odds_knockout"):
        predicted["odds_knockout"] = payload["odds_knockout"]
    if payload.get("knockout"):
        predicted["knockout"] = payload["knockout"]
    if payload.get("real_knockout"):
        predicted["real_knockout"] = payload["real_knockout"]

    # Ensure knockout framework topology is present.
    if not bracket.get("knockout_rounds"):
        ko_framework = _build_knockout_framework(comp)
        if ko_framework:
            bracket["knockout_rounds"] = ko_framework
    if not bracket.get("knockout"):
        matches = _gather_competition_cup_matches(comp)
        if matches:
            knockout, odds_knockout, real_knockout = _build_cup_knockout_payload(matches, comp)
            bracket["knockout"] = knockout
            bracket["odds_knockout"] = odds_knockout
            bracket["real_knockout"] = bracket.get("real_knockout") or real_knockout
            payload["knockout"] = knockout
            payload["odds_knockout"] = odds_knockout
            payload["real_knockout"] = bracket["real_knockout"]

    payload["bracket"] = bracket
    payload["predicted"] = predicted
    payload["format"] = fmt

    _write_cup_data_cache(comp, payload)
    return payload
