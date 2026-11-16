"""Projected League Tables for NWSL and WSL using Monte Carlo simulation."""

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
_WEBSITE_DIR = str(_bts_paths.SP_DIR / "Website")
if _WEBSITE_DIR not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, _WEBSITE_DIR)

BASE_DIR = str(_bts_paths.WOMENS_DIR)
PREDICTIONS_DIR = str(_bts_paths.OUTPUT_PRED_WOMENS)
PROJECT_DIR = str(_bts_paths.SP_DIR)

import argparse
import json
import os
import random
from collections import defaultdict
import pandas as pd

from competition_rules import canonical_team_name

OUT_CSV = str(_bts_paths.WOMENS_PROJECTED_TABLE_FILE)
SIM_RUNS = 1000

def _league_configs() -> list[dict]:
    """Current-season raw CSV of each league (written by Womens_Data_Manager from ESPN)."""
    import Womens_Data_Manager as wdm

    configs = []
    for comp, cfg in wdm.LEAGUES.items():
        season = wdm.current_season(comp)
        configs.append({
            "competition": comp,
            "raw_csv": os.path.join(str(cfg["raw_dir"]), f"{cfg['prefix']}stat{season}.csv"),
        })
    return configs


def _model_probabilities() -> dict:
    """(home, away, date) -> (p_home, p_draw, p_away) from this run's upcoming predictions."""
    path = str(_bts_paths.WOMENS_UPCOMING_FILE)
    out: dict = {}
    if not os.path.exists(path):
        return out
    try:
        df = pd.read_csv(path)
    except Exception:
        return out
    for _, row in df.iterrows():
        try:
            probs = (float(row["prob_home"]), float(row["prob_draw"]), float(row["prob_away"]))
        except (KeyError, TypeError, ValueError):
            continue
        if any(p != p for p in probs):  # NaN
            continue
        tot = sum(probs)
        if tot <= 0:
            continue
        if tot > 1.5:  # percentages
            probs = tuple(p / 100.0 for p in probs)
            tot = sum(probs)
        key = (str(row.get("home_team", "")).strip(), str(row.get("away_team", "")).strip(),
               str(row.get("match_date_iso", row.get("match_date", ""))).strip()[:10])
        out[key] = tuple(p / tot for p in probs)
    return out


def load_league_state(raw_csv_path: str, competition: str, model_probs: dict | None = None):
    """Current table + remaining league fixtures. Playoff games never count."""
    df = pd.read_csv(raw_csv_path, dtype=str).fillna("")
    if "Stage" in df.columns:
        df = df[df["Stage"].str.strip().isin(("", "regular"))]
    teams = sorted((set(df["Home"].str.strip()) | set(df["Away"].str.strip())) - {""})
    model_probs = model_probs or {}

    standings = {
        t: {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0}
        for t in teams
    }
    remaining_fixtures = []

    for _, row in df.iterrows():
        ht = str(row["Home"]).strip()
        at = str(row["Away"]).strip()
        res = str(row.get("Res", "")).strip()

        if res in ("H", "D", "A"):
            hg = int(float(row["HG"]))
            ag = int(float(row["AG"]))
            standings[ht]["P"] += 1
            standings[at]["P"] += 1
            standings[ht]["GF"] += hg
            standings[ht]["GA"] += ag
            standings[ht]["GD"] += (hg - ag)
            standings[at]["GF"] += ag
            standings[at]["GA"] += hg
            standings[at]["GD"] += (ag - hg)

            if res == "H":
                standings[ht]["W"] += 1
                standings[ht]["Pts"] += 3
                standings[at]["L"] += 1
            elif res == "A":
                standings[at]["W"] += 1
                standings[at]["Pts"] += 3
                standings[ht]["L"] += 1
            else:
                standings[ht]["D"] += 1
                standings[at]["D"] += 1
                standings[ht]["Pts"] += 1
                standings[at]["Pts"] += 1
        else:
            # Model probabilities for the fixture (Predict_Upcoming_Matchweek);
            # neutral home-advantage prior if the fixture was not predicted.
            try:
                d_iso = pd.to_datetime(row["Date"], format="%d/%m/%Y").strftime("%Y-%m-%d")
            except Exception:
                d_iso = ""
            ph, pd_prob, pa = model_probs.get((ht, at, d_iso), (0.44, 0.26, 0.30))
            remaining_fixtures.append((ht, at, ph, pd_prob, pa))

    return teams, standings, remaining_fixtures


def simulate_league(competition: str, teams: list[str], current_standings: dict, remaining: list, sim_runs: int = SIM_RUNS):
    rng = random.Random(42)
    position_counts = {t: defaultdict(int) for t in teams}
    total_pts = {t: 0 for t in teams}
    total_w = {t: 0 for t in teams}
    total_d = {t: 0 for t in teams}
    total_l = {t: 0 for t in teams}
    total_gf = {t: 0 for t in teams}
    total_ga = {t: 0 for t in teams}

    num_teams = len(teams)

    for run in range(sim_runs):
        sim_table = {
            t: {
                "Pts": current_standings[t]["Pts"],
                "GF": current_standings[t]["GF"],
                "GA": current_standings[t]["GA"],
                "GD": current_standings[t]["GD"],
                "W": current_standings[t]["W"],
                "D": current_standings[t]["D"],
                "L": current_standings[t]["L"],
            }
            for t in teams
        }

        for ht, at, ph, pd_prob, pa in remaining:
            roll = rng.random()
            if roll < ph:
                sim_table[ht]["Pts"] += 3
                sim_table[ht]["W"] += 1
                sim_table[at]["L"] += 1
                sim_table[ht]["GF"] += 2
                sim_table[ht]["GA"] += 1
                sim_table[ht]["GD"] += 1
                sim_table[at]["GF"] += 1
                sim_table[at]["GA"] += 2
                sim_table[at]["GD"] -= 1
            elif roll < ph + pd_prob:
                sim_table[ht]["Pts"] += 1
                sim_table[at]["Pts"] += 1
                sim_table[ht]["D"] += 1
                sim_table[at]["D"] += 1
                sim_table[ht]["GF"] += 1
                sim_table[ht]["GA"] += 1
                sim_table[at]["GF"] += 1
                sim_table[at]["GA"] += 1
            else:
                sim_table[at]["Pts"] += 3
                sim_table[at]["W"] += 1
                sim_table[ht]["L"] += 1
                sim_table[at]["GF"] += 2
                sim_table[at]["GA"] += 1
                sim_table[at]["GD"] += 1
                sim_table[ht]["GF"] += 1
                sim_table[ht]["GA"] += 2
                sim_table[ht]["GD"] -= 1

        # Rank teams: Pts, GD, GF
        ranked = sorted(
            teams,
            key=lambda t: (sim_table[t]["Pts"], sim_table[t]["GD"], sim_table[t]["GF"], rng.random()),
            reverse=True,
        )

        for pos_idx, team in enumerate(ranked):
            pos = pos_idx + 1
            position_counts[team][pos] += 1
            total_pts[team] += sim_table[team]["Pts"]
            total_w[team] += sim_table[team]["W"]
            total_d[team] += sim_table[team]["D"]
            total_l[team] += sim_table[team]["L"]
            total_gf[team] += sim_table[team]["GF"]
            total_ga[team] += sim_table[team]["GA"]

    # Build final projected table rows
    projected_rows = []
    avg_pts = {t: total_pts[t] / sim_runs for t in teams}
    ranked_final = sorted(teams, key=lambda t: (avg_pts[t], current_standings[t]["GD"]), reverse=True)

    for pos_idx, team in enumerate(ranked_final):
        pos = pos_idx + 1
        pos_dist = {str(p): round((position_counts[team][p] / sim_runs) * 100.0, 1) for p in range(1, num_teams + 1)}
        win_pct = pos_dist.get("1", 0.0)
        top4_pct = round(sum(position_counts[team][p] for p in range(1, 5)) / sim_runs * 100.0, 1)
        bottom3_pct = round(sum(position_counts[team][p] for p in range(num_teams - 2, num_teams + 1)) / sim_runs * 100.0, 1)

        most_likely_pos = max(range(1, num_teams + 1), key=lambda p: position_counts[team][p])
        most_likely_pct = pos_dist.get(str(most_likely_pos), 0.0)

        real_p = current_standings[team]["P"]
        rem_p = sum(1 for ht, at, _, _, _ in remaining if ht == team or at == team)

        projected_rows.append({
            "competition": competition,
            "position": pos,
            "team": team,
            "P": real_p + rem_p,
            "W": round(total_w[team] / sim_runs),
            "D": round(total_d[team] / sim_runs),
            "L": round(total_l[team] / sim_runs),
            "GF": round(total_gf[team] / sim_runs),
            "GA": round(total_ga[team] / sim_runs),
            "GD": round((total_gf[team] - total_ga[team]) / sim_runs),
            "Pts": round(avg_pts[team]),
            "PlayedReal": real_p,
            "PlayedPred": rem_p,
            "win_league_pct": win_pct,
            "top4_pct": top4_pct,
            "bottom3_pct": bottom3_pct,
            "most_likely_position": most_likely_pos,
            "most_likely_position_pct": most_likely_pct,
            "position_odds_json": json.dumps(pos_dist),
            "sim_runs": sim_runs,
        })

    return projected_rows


def project_all():
    print("=== Projecting Womens League Tables ===")
    all_rows = []
    leagues = _league_configs()
    model_probs = _model_probabilities()
    for cfg in leagues:
        comp = cfg["competition"]
        csv_path = cfg["raw_csv"]
        if not os.path.exists(csv_path):
            print(f"[WARN] {comp}: missing {csv_path} (run Womens_Data_Manager.py)")
            continue
        print(f"Simulating {comp} from {csv_path}...")
        teams, standings, remaining = load_league_state(csv_path, comp, model_probs)
        print(f"  {len(teams)} teams, {sum(s['P'] for s in standings.values()) // 2} played, {len(remaining)} remaining fixtures.")
        rows = simulate_league(comp, teams, standings, remaining, sim_runs=SIM_RUNS)
        all_rows.extend(rows)

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    df_out = pd.DataFrame(all_rows)
    df_out.to_csv(OUT_CSV, index=False)
    print(f"Saved {len(all_rows)} rows to {OUT_CSV}")

    # Rebuild league data caches from the refreshed history. build_league_data_payload
    # returns the existing cache file when there is one, so it never rebuilt
    # anything here: after the switch to real ESPN data the app kept serving a
    # stale table (1 game per team). rebuild_league_data_caches re-indexes games,
    # drops cached real tables and rebuilds these competitions uncached.
    try:
        from league_data import rebuild_league_data_caches

        comps = [cfg["competition"] for cfg in leagues]
        results = rebuild_league_data_caches(comps, clear_first=False)
        for comp in comps:
            print(f"  Rebuilt LeagueData for {comp} (ok: {results.get(comp)})")
    except Exception as exc:
        print(f"[WARN] Error rebuilding league data caches: {exc}")


def main():
    project_all()


if __name__ == "__main__":
    main()

