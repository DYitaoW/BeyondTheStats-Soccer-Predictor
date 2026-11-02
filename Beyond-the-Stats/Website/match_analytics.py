"""Comprehensive match analytics, unified cross-competition form, head-to-head, and momentum."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from typing import Any, Optional

import config
from competition_rules import canonical_team_name
import math_utils

_ANALYTICS_CACHE_LOCK = threading.Lock()
_ANALYTICS_HISTORY_CACHE: dict[str, list[dict]] = {}
_ANALYTICS_CACHE_EXPIRY = 0.0
_ANALYTICS_CACHE_TTL = 300.0  # 5 minutes


def _load_unified_match_history() -> dict[str, list[dict]]:
    """Build a unified index of all completed matches per team across all competitions.

    Includes:
    1. SQLite past_games (domestic leagues, cups, continental, international)
    2. completed_cup_predictions.csv (domestic & continental cups)
    3. Data/Processed_Data/ (deep historical league matches with stats)
    """
    global _ANALYTICS_HISTORY_CACHE, _ANALYTICS_CACHE_EXPIRY
    now = time.time()
    with _ANALYTICS_CACHE_LOCK:
        if _ANALYTICS_HISTORY_CACHE and now < _ANALYTICS_CACHE_EXPIRY:
            return _ANALYTICS_HISTORY_CACHE

        history: dict[str, list[dict]] = defaultdict(list)
        seen: set[str] = set()

        # 1. Load from past_games in SQLite
        db_path = getattr(config, "SQLITE_STORE_PATH", None) or os.path.join(
            config.PROJECT_DIR, "Output", "Status", "bts_store.db"
        )
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("SELECT payload_json FROM past_games")
                for (payload_raw,) in cur.fetchall():
                    if not payload_raw:
                        continue
                    try:
                        p = json.loads(payload_raw)
                    except Exception:
                        continue
                    dt = p.get("match_date_iso") or p.get("match_date")
                    comp = str(p.get("competition", "")).strip()
                    ht = str(p.get("home_team", "")).strip()
                    at = str(p.get("away_team", "")).strip()
                    if not dt or not ht or not at:
                        continue
                    c_h = canonical_team_name(ht, comp) or ht
                    c_a = canonical_team_name(at, comp) or at
                    key = f"{dt}|{c_h.lower()}|{c_a.lower()}"
                    if key in seen:
                        continue
                    seen.add(key)
                    hg = p.get("actual_home_goals") if p.get("actual_home_goals") is not None else p.get("home_score")
                    ag = p.get("actual_away_goals") if p.get("actual_away_goals") is not None else p.get("away_score")
                    res = str(p.get("actual_result", "")).strip().upper()
                    try:
                        hg_i = int(hg) if hg is not None else None
                        ag_i = int(ag) if ag is not None else None
                    except Exception:
                        hg_i, ag_i = None, None
                    if not res and hg_i is not None and ag_i is not None:
                        res = "H" if hg_i > ag_i else ("A" if ag_i > hg_i else "D")

                    m_home = {
                        "date": dt, "competition": comp, "opponent": c_a, "venue": "home",
                        "home_team": c_h, "away_team": c_a, "home_score": hg_i, "away_score": ag_i,
                        "team_score": hg_i, "opponent_score": ag_i,
                        "result": "W" if res == "H" else ("L" if res == "A" else "D"),
                        "is_home": True,
                    }
                    m_away = {
                        "date": dt, "competition": comp, "opponent": c_h, "venue": "away",
                        "home_team": c_h, "away_team": c_a, "home_score": hg_i, "away_score": ag_i,
                        "team_score": ag_i, "opponent_score": hg_i,
                        "result": "W" if res == "A" else ("L" if res == "H" else "D"),
                        "is_home": False,
                    }
                    history[c_h].append(m_home)
                    history[c_a].append(m_away)
                conn.close()
            except Exception:
                pass

        # 2. Load from completed_cup_predictions.csv
        cup_csv = getattr(config, "COMPLETED_CUP_FILE", None) or os.path.join(
            config.PROJECT_DIR, "Output", "Predictions", "cups", "completed_cup_predictions.csv"
        )
        if os.path.exists(cup_csv):
            try:
                with open(cup_csv, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        dt = str(r.get("match_date", "")).strip()
                        comp = str(r.get("competition", "")).strip()
                        ht = str(r.get("home_team", "")).strip()
                        at = str(r.get("away_team", "")).strip()
                        if not dt or not ht or not at:
                            continue
                        c_h = canonical_team_name(ht, comp) or ht
                        c_a = canonical_team_name(at, comp) or at
                        key = f"{dt}|{c_h.lower()}|{c_a.lower()}"
                        if key in seen:
                            continue
                        seen.add(key)
                        try:
                            hg_i = int(float(r.get("actual_home_goals", 0) or 0))
                            ag_i = int(float(r.get("actual_away_goals", 0) or 0))
                        except Exception:
                            continue
                        res = str(r.get("actual_result", "")).strip().upper()
                        if not res and hg_i is not None and ag_i is not None:
                            res = "H" if hg_i > ag_i else ("A" if ag_i > hg_i else "D")

                        m_home = {
                            "date": dt, "competition": comp, "opponent": c_a, "venue": "home",
                            "home_team": c_h, "away_team": c_a, "home_score": hg_i, "away_score": ag_i,
                            "team_score": hg_i, "opponent_score": ag_i,
                            "result": "W" if res == "H" else ("L" if res == "A" else "D"),
                            "is_home": True,
                        }
                        m_away = {
                            "date": dt, "competition": comp, "opponent": c_h, "venue": "away",
                            "home_team": c_h, "away_team": c_a, "home_score": hg_i, "away_score": ag_i,
                            "team_score": ag_i, "opponent_score": hg_i,
                            "result": "W" if res == "A" else ("L" if res == "H" else "D"),
                            "is_home": False,
                        }
                        history[c_h].append(m_home)
                        history[c_a].append(m_away)
            except Exception:
                pass

        # 3. Load from Processed_Data directories
        processed_dirs = [
            getattr(config, "EUROPE_PROCESSED_DIR", None) or os.path.join(config.PROJECT_DIR, "Data", "Processed_Data"),
            getattr(config, "MLS_PROCESSED_DIR", None),
            getattr(config, "EXTRA_PROCESSED_DIR", None),
        ]
        for p_dir in processed_dirs:
            if not p_dir or not os.path.exists(p_dir):
                continue
            for root, _, files in os.walk(p_dir):
                csv_files = [f for f in sorted(files, reverse=True) if f.endswith(".csv")][:3]
                for name in csv_files:
                    path = os.path.join(root, name)
                    comp = os.path.basename(root) or "Domestic League"
                    try:
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            reader = csv.DictReader(f)
                            for r in reader:
                                dt = str(r.get("Date", "")).strip()
                                ht = str(r.get("HomeTeam", "")).strip()
                                at = str(r.get("AwayTeam", "")).strip()
                                if not dt or not ht or not at:
                                    continue
                                c_h = canonical_team_name(ht, comp) or ht
                                c_a = canonical_team_name(at, comp) or at
                                key = f"{dt}|{c_h.lower()}|{c_a.lower()}"
                                if key in seen:
                                    continue
                                seen.add(key)
                                try:
                                    hg_i = int(float(r.get("FTHG", 0) or 0))
                                    ag_i = int(float(r.get("FTAG", 0) or 0))
                                except Exception:
                                    continue
                                res = str(r.get("FTR", "")).strip().upper()
                                hs_val = r.get("HS") or r.get("HomeShots")
                                as_val = r.get("AS") or r.get("AwayShots")
                                hst_val = r.get("HST") or r.get("HomeShotsOnTarget")
                                ast_val = r.get("AST") or r.get("AwayShotsOnTarget")

                                hs_i = int(float(hs_val)) if hs_val is not None and str(hs_val).replace(".", "").isdigit() else None
                                as_i = int(float(as_val)) if as_val is not None and str(as_val).replace(".", "").isdigit() else None
                                hst_i = int(float(hst_val)) if hst_val is not None and str(hst_val).replace(".", "").isdigit() else None
                                ast_i = int(float(ast_val)) if ast_val is not None and str(ast_val).replace(".", "").isdigit() else None

                                m_home = {
                                    "date": dt, "competition": comp, "opponent": c_a, "venue": "home",
                                    "home_team": c_h, "away_team": c_a, "home_score": hg_i, "away_score": ag_i,
                                    "team_score": hg_i, "opponent_score": ag_i,
                                    "result": "W" if res == "H" else ("L" if res == "A" else "D"),
                                    "is_home": True, "shots": hs_i, "shots_on_target": hst_i,
                                }
                                m_away = {
                                    "date": dt, "competition": comp, "opponent": c_h, "venue": "away",
                                    "home_team": c_h, "away_team": c_a, "home_score": hg_i, "away_score": ag_i,
                                    "team_score": ag_i, "opponent_score": hg_i,
                                    "result": "W" if res == "A" else ("L" if res == "H" else "D"),
                                    "is_home": False, "shots": as_i, "shots_on_target": ast_i,
                                }
                                history[c_h].append(m_home)
                                history[c_a].append(m_away)
                    except Exception:
                        pass

        # Sort all teams' histories descending by date
        for t in list(history.keys()):
            history[t].sort(key=lambda m: str(m.get("date", "")), reverse=True)

        _ANALYTICS_HISTORY_CACHE = history
        _ANALYTICS_CACHE_EXPIRY = now + _ANALYTICS_CACHE_TTL
        return _ANALYTICS_HISTORY_CACHE


def get_team_comprehensive_form(
    team_name: str,
    competition: str = "",
    limit: int = 5,
    venue: Optional[str] = None,
    exclude_friendlies: bool = True,
) -> list[dict]:
    """Return recent completed matches for a team across all competitions."""
    c_team = canonical_team_name(team_name, competition) or str(team_name or "").strip()
    if not c_team:
        return []
    history = _load_unified_match_history()
    matches = history.get(c_team, [])
    if not matches:
        # Fallback lowercase lookup
        c_lower = c_team.lower()
        for k, v in history.items():
            if k.lower() == c_lower:
                matches = v
                break

    out: list[dict] = []
    for m in matches:
        comp = str(m.get("competition", "")).lower()
        if exclude_friendlies and ("friendly" in comp or "friendlies" in comp):
            continue
        if venue == "home" and not m.get("is_home"):
            continue
        if venue == "away" and m.get("is_home"):
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def get_head_to_head(
    home_team: str,
    away_team: str,
    competition: str = "",
    limit: int = 5,
    exclude_friendlies: bool = True,
) -> tuple[list[dict], dict]:
    """Return historical Head-to-Head (H2H) meetings and summary statistics."""
    c_home = canonical_team_name(home_team, competition) or str(home_team or "").strip()
    c_away = canonical_team_name(away_team, competition) or str(away_team or "").strip()
    if not c_home or not c_away:
        return [], {}

    history = _load_unified_match_history()
    matches_raw = history.get(c_home, [])
    h2h_matches: list[dict] = []
    seen = set()

    for m in matches_raw:
        opp = m.get("opponent", "")
        if opp == c_away or opp.lower() == c_away.lower():
            comp = str(m.get("competition", "")).lower()
            if exclude_friendlies and ("friendly" in comp or "friendlies" in comp):
                continue
            dt = str(m.get("date", ""))
            k = f"{dt}|{m.get('home_team')}|{m.get('away_team')}"
            if k in seen:
                continue
            seen.add(k)
            # Annotate winner from match perspective
            h_score = m.get("home_score")
            a_score = m.get("away_score")
            if h_score is not None and a_score is not None:
                winner = m.get("home_team") if h_score > a_score else (m.get("away_team") if a_score > h_score else "Draw")
            else:
                winner = "Draw" if m.get("result") == "D" else (c_home if m.get("result") == "W" else c_away)

            m_copy = dict(m)
            m_copy["winner"] = winner
            h2h_matches.append(m_copy)
            if len(h2h_matches) >= limit:
                break

    # Compute H2H Summary statistics
    total = len(h2h_matches)
    if total == 0:
        return [], {
            "total_meetings": 0,
            "home_team_wins": 0,
            "away_team_wins": 0,
            "draws": 0,
            "avg_goals_per_game": 0.0,
            "btts_pct": 0.0,
        }

    h_wins = 0
    a_wins = 0
    draws = 0
    tot_goals = 0
    btts_cnt = 0

    for m in h2h_matches:
        hs = m.get("home_score", 0) or 0
        as_ = m.get("away_score", 0) or 0
        tot_goals += (hs + as_)
        if hs > 0 and as_ > 0:
            btts_cnt += 1
        if m.get("winner") == c_home:
            h_wins += 1
        elif m.get("winner") == c_away:
            a_wins += 1
        else:
            draws += 1

    summary = {
        "total_meetings": total,
        "home_team": c_home,
        "away_team": c_away,
        "home_team_wins": h_wins,
        "away_team_wins": a_wins,
        "draws": draws,
        "avg_goals_per_game": round(tot_goals / total, 2),
        "btts_pct": round((btts_cnt / total) * 100, 1),
        "last_meeting_date": h2h_matches[0].get("date") if h2h_matches else None,
        "last_meeting_result": f"{h2h_matches[0].get('home_team')} {h2h_matches[0].get('home_score')}-{h2h_matches[0].get('away_score')} {h2h_matches[0].get('away_team')}" if h2h_matches else None,
    }
    return h2h_matches, summary


def compute_team_momentum(matches: list[dict]) -> dict:
    """Calculate streaks, conversion rates, and form momentum from recent matches."""
    if not matches:
        return {
            "unbeaten_streak": 0,
            "win_streak": 0,
            "scoring_streak": 0,
            "clean_sheet_streak": 0,
            "form_guide": "",
            "points_last_5": 0,
            "goals_scored_last_5": 0,
            "goals_conceded_last_5": 0,
            "goal_diff_last_5": 0,
            "clean_sheets_last_5": 0,
            "failed_to_score_last_5": 0,
            "shot_conversion_rate": None,
        }

    # Streaks (chronological forward from most recent backwards)
    unbeaten = 0
    for m in matches:
        if m.get("result") in ("W", "D"):
            unbeaten += 1
        else:
            break

    wins = 0
    for m in matches:
        if m.get("result") == "W":
            wins += 1
        else:
            break

    scoring = 0
    for m in matches:
        ts = m.get("team_score")
        if ts is not None and ts > 0:
            scoring += 1
        else:
            break

    clean_sheets = 0
    for m in matches:
        os_ = m.get("opponent_score")
        if os_ is not None and os_ == 0:
            clean_sheets += 1
        else:
            break

    form_tokens = [m.get("result", "-") for m in matches[:5]]
    form_guide = "-".join(form_tokens)

    pts = sum(3 if r == "W" else (1 if r == "D" else 0) for r in form_tokens)
    goals_for = sum(m.get("team_score") or 0 for m in matches[:5])
    goals_against = sum(m.get("opponent_score") or 0 for m in matches[:5])
    clean_cnt = sum(1 for m in matches[:5] if (m.get("opponent_score") or 0) == 0 and m.get("opponent_score") is not None)
    failed_cnt = sum(1 for m in matches[:5] if (m.get("team_score") or 0) == 0 and m.get("team_score") is not None)

    # Shot conversion rate
    tot_shots = sum(m.get("shots") or 0 for m in matches[:5] if m.get("shots") is not None)
    tot_sot = sum(m.get("shots_on_target") or 0 for m in matches[:5] if m.get("shots_on_target") is not None)

    conv_rate = None
    if tot_sot > 0:
        conv_rate = round((goals_for / tot_sot) * 100, 1)
    elif tot_shots > 0:
        conv_rate = round((goals_for / tot_shots) * 100, 1)

    return {
        "unbeaten_streak": unbeaten,
        "win_streak": wins,
        "scoring_streak": scoring,
        "clean_sheet_streak": clean_sheets,
        "form_guide": form_guide,
        "points_last_5": pts,
        "goals_scored_last_5": goals_for,
        "goals_conceded_last_5": goals_against,
        "goal_diff_last_5": goals_for - goals_against,
        "clean_sheets_last_5": clean_cnt,
        "failed_to_score_last_5": failed_cnt,
        "shot_conversion_rate": conv_rate,
    }


def enrich_match_analytics(entry: dict) -> dict:
    """Enrich a match dictionary with all comprehensive statistical predictions and analytics."""
    if not isinstance(entry, dict):
        return entry

    comp = str(entry.get("competition", "")).strip()
    h_team = str(entry.get("home_team", "")).strip()
    a_team = str(entry.get("away_team", "")).strip()
    if not h_team or not a_team:
        return entry

    # 1. Comprehensive team form (across league, cups, continental)
    last_5_h = get_team_comprehensive_form(h_team, competition=comp, limit=5)
    last_5_a = get_team_comprehensive_form(a_team, competition=comp, limit=5)

    entry["last_5_home"] = last_5_h
    entry["last_5_away"] = last_5_a

    # 2. Home vs Away venue-specific split form
    entry["last_5_home_at_home"] = get_team_comprehensive_form(h_team, competition=comp, limit=5, venue="home")
    entry["last_5_away_at_away"] = get_team_comprehensive_form(a_team, competition=comp, limit=5, venue="away")

    # 3. Head-to-Head (last 5 meetings between the two sides, excluding friendlies)
    h2h_matches, h2h_summary = get_head_to_head(h_team, a_team, competition=comp, limit=5)
    entry["head_to_head"] = h2h_matches
    entry["h2h_summary"] = h2h_summary

    # 4. Momentum & Streaks
    entry["home_momentum"] = compute_team_momentum(last_5_h)
    entry["away_momentum"] = compute_team_momentum(last_5_a)

    # 5. Expected Points (xPTS)
    ph = entry.get("prob_home")
    pdv = entry.get("prob_draw")
    pa = entry.get("prob_away")
    if ph is not None or pdv is not None or pa is not None:
        xpts = math_utils._compute_expected_points(ph, pdv, pa)
        entry["expected_points"] = xpts
        entry["xpts_home"] = xpts["xpts_home"]
        entry["xpts_away"] = xpts["xpts_away"]

    # 6. Half-Time vs Full-Time (HT/FT) goal distributions
    hg = entry.get("pred_home_goals")
    ag = entry.get("pred_away_goals")
    if hg is not None and ag is not None:
        ht_dist = math_utils._compute_halftime_distributions(hg, ag)
        entry["halftime_prediction"] = ht_dist
        entry["pred_home_goals_ht"] = ht_dist["pred_home_goals_ht"]
        entry["pred_away_goals_ht"] = ht_dist["pred_away_goals_ht"]
        entry["prob_home_ht"] = ht_dist["prob_home_ht"]
        entry["prob_draw_ht"] = ht_dist["prob_draw_ht"]
        entry["prob_away_ht"] = ht_dist["prob_away_ht"]
        entry["prob_ht_over_0_5"] = ht_dist["prob_ht_over_0_5"]
        entry["prob_ht_over_1_5"] = ht_dist["prob_ht_over_1_5"]
        entry["ht_correct_score_dist"] = ht_dist["ht_correct_score_dist"]

        htft_dist = math_utils._compute_htft_probabilities(hg, ag, ph, pdv, pa)
        entry["htft_prediction"] = htft_dist["prob_htft"]
        entry["predicted_htft"] = htft_dist["predicted_htft"]
        entry["prob_highest_scoring_half"] = htft_dist["prob_highest_scoring_half"]

        # 7. Combo Betting Markets
        combos = math_utils._compute_combo_markets(hg, ag, ph, pdv, pa)
        entry["combo_markets"] = combos
        for ck, cv in combos.items():
            entry[ck] = cv

        # 8. Predicted Team Match Stats (Shots, SOT, Corners, Possession, Fouls, Cards)
        team_stats = math_utils._compute_predicted_team_stats(
            hg, ag,
            home_attack=entry.get("home_attack_rating"),
            away_attack=entry.get("away_attack_rating"),
        )
        entry["predicted_stats"] = team_stats
        for sk, sv in team_stats.items():
            if entry.get(sk) is None:
                entry[sk] = sv

    return entry

