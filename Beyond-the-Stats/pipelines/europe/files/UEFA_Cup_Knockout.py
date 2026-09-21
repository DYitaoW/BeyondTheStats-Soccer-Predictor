"""UEFA Champions / Europa / Conference League knockout format helpers.

League phase → Knockout Playoff (9–24) → Round of 16 → QF → SF → Final.

Playoff draw (two-legged, higher seed home in 2nd leg):
  - Seeds 9–16 are drawn against 17–24 in fixed bands:
      9 & 10  ↔  23 & 24   (9/10 may face either 23 or 24)
      11 & 12 ↔  21 & 22
      13 & 14 ↔  19 & 20
      15 & 16 ↔  17 & 18

Round of 16 constraints:
  - Seeds 1 and 2 sit on opposite bracket halves (cannot meet before the final)
  - Seeds 3 and 4 cannot meet 1 or 2 before the semi-finals
  - Seeds 1–2 only face winners of the 9/10 vs 23/24 playoff ties
  - Seeds 3–4 face winners of 11/12 vs 21/22, etc.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import numpy as np

UEFA_COMPETITIONS = (
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
)

UEFA_PHASE_MATCHES = {
    "Europe/Champions League": 8,
    "Europe/Europa League": 8,
    "Europe/Conference League": 6,
}

# Bands: (high seeds 9-16 pair, low seeds 17-24 pair)
PLAYOFF_DRAW_BANDS = (
    ((9, 10), (23, 24)),
    ((11, 12), (21, 22)),
    ((13, 14), (19, 20)),
    ((15, 16), (17, 18)),
)

# Which band's playoff winners may face which automatic qualifiers in R16.
R16_BAND_FOR_SEEDS = {
    1: 0,
    2: 0,  # band 0 = 9/10 vs 23/24
    3: 1,
    4: 1,  # band 1 = 11/12 vs 21/22
    5: 2,
    6: 2,
    7: 3,
    8: 3,
}

# Bracket halves: 1 and 2 opposite; 3/4 cannot meet 1/2 until SF.
# Half A path: 1, 4, 5, 8 — Half B path: 2, 3, 6, 7
R16_HALF_A_SEEDS = (1, 8, 4, 5)
R16_HALF_B_SEEDS = (2, 7, 3, 6)

PredictFn = Callable[[str, str, Any], tuple[int, int, dict]]
# predict_fn(home, away, rng) -> (home_goals, away_goals, meta)


def ranked_team_list(table_rows: list[dict]) -> list[str]:
    """Return team names ordered by league-phase position (1..N)."""
    def _pos(row):
        try:
            return int(float(row.get("position") or 999))
        except (TypeError, ValueError):
            return 999

    ordered = sorted(table_rows or [], key=lambda r: (_pos(r), str(r.get("team") or "")))
    teams = [str(r.get("team") or "").strip() for r in ordered if str(r.get("team") or "").strip()]
    # Pad to 24 so knockout draws always have placeholders when table is short.
    while len(teams) < 24:
        teams.append(f"Seed {len(teams) + 1}")
    return teams


def seed_team(ranked: list[str], position: int) -> str:
    idx = max(1, int(position)) - 1
    if 0 <= idx < len(ranked):
        return ranked[idx]
    return f"Seed {position}"


def draw_knockout_playoff_ties(ranked: list[str], rng: np.random.Generator) -> list[dict]:
    """Randomise playoff pairings within UEFA bands; return tie descriptors."""
    ties: list[dict] = []
    slot = 1
    for band_idx, (highs, lows) in enumerate(PLAYOFF_DRAW_BANDS):
        low_order = list(lows)
        rng.shuffle(low_order)
        for i, high_pos in enumerate(highs):
            low_pos = low_order[i]
            high = seed_team(ranked, high_pos)
            low = seed_team(ranked, low_pos)
            ties.append(
                {
                    "slot": slot,
                    "band": band_idx,
                    "high_pos": high_pos,
                    "low_pos": low_pos,
                    "high_team": high,
                    "low_team": low,
                    # Leg 1: lower seed hosts; leg 2: higher seed hosts.
                    "leg1_home": low,
                    "leg1_away": high,
                    "leg2_home": high,
                    "leg2_away": low,
                }
            )
            slot += 1
    return ties


def _sample_score(home: str, away: str, rng: np.random.Generator, predict_fn: Optional[PredictFn]):
    if predict_fn is not None:
        try:
            hg, ag, meta = predict_fn(home, away, rng)
            return int(max(0, hg)), int(max(0, ag)), meta or {}
        except Exception:
            pass
    # Mild home edge prior when model odds are unavailable.
    ph, pd_, pa = 0.46, 0.26, 0.28
    pick = float(rng.random())
    base_h, base_a = 1, 1
    if pick < ph:
        hg, ag = max(base_h, base_a + 1), base_a
        result = "H"
    elif pick < ph + pd_:
        hg = ag = base_h
        result = "D"
    else:
        hg, ag = base_h, max(base_a, base_h + 1)
        result = "A"
    return hg, ag, {"predicted_result": result, "prob_home": ph, "prob_draw": pd_, "prob_away": pa}


def resolve_two_legged_tie(
    high_team: str,
    low_team: str,
    rng: np.random.Generator,
    predict_fn: Optional[PredictFn] = None,
) -> dict:
    """Play two legs; higher seed home in 2nd leg. Aggregate then penalties if level."""
    leg1_hg, leg1_ag, meta1 = _sample_score(low_team, high_team, rng, predict_fn)
    leg2_hg, leg2_ag, meta2 = _sample_score(high_team, low_team, rng, predict_fn)
    high_agg = leg1_ag + leg2_hg
    low_agg = leg1_hg + leg2_ag
    pens = False
    if high_agg > low_agg:
        winner = high_team
    elif low_agg > high_agg:
        winner = low_team
    else:
        pens = True
        # Tied on aggregate → random (penalties). Slight edge to higher seed
        # (home in 2nd leg / ranking advantage).
        winner = high_team if float(rng.random()) < 0.55 else low_team
    return {
        "high_team": high_team,
        "low_team": low_team,
        "leg1": {"home": low_team, "away": high_team, "home_goals": leg1_hg, "away_goals": leg1_ag, **meta1},
        "leg2": {"home": high_team, "away": low_team, "home_goals": leg2_hg, "away_goals": leg2_ag, **meta2},
        "high_agg": high_agg,
        "low_agg": low_agg,
        "winner": winner,
        "decided_by_penalties": pens,
    }


def _seed_rank_map(ranked: list[str]) -> dict[str, int]:
    """Map team → league-phase position (1-based). First occurrence wins."""
    out: dict[str, int] = {}
    for idx, team in enumerate(ranked):
        name = str(team or "").strip()
        if name and name not in out:
            out[name] = idx + 1
    return out


def _order_by_seed(team_a: str, team_b: str, seed_ranks: dict[str, int]) -> tuple[str, str]:
    """Return (higher_seed, lower_seed) for 2nd-leg home advantage."""
    ra = seed_ranks.get(team_a, 999)
    rb = seed_ranks.get(team_b, 999)
    if ra <= rb:
        return team_a, team_b
    return team_b, team_a


def simulate_knockout_from_table(
    ranked_teams: list[str],
    rng: np.random.Generator,
    predict_fn: Optional[PredictFn] = None,
) -> dict:
    """One full knockout path from a finished league-phase ordering.

    Returns winners / round participants for odds tallying, plus a compact
    bracket snapshot (not the primary UI — finish odds are).
    """
    ranked = list(ranked_teams)
    while len(ranked) < 24:
        ranked.append(f"Seed {len(ranked) + 1}")
    seed_ranks = _seed_rank_map(ranked)

    playoff_ties = draw_knockout_playoff_ties(ranked, rng)
    playoff_results = []
    winners_by_band: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
    for tie in playoff_ties:
        result = resolve_two_legged_tie(tie["high_team"], tie["low_team"], rng, predict_fn)
        result.update({k: tie[k] for k in ("slot", "band", "high_pos", "low_pos")})
        playoff_results.append(result)
        winners_by_band[int(tie["band"])].append(result["winner"])

    # Assign band winners to face automatic seeds 1–8.
    r16_pairings = []
    for band_idx in range(4):
        band_winners = list(winners_by_band.get(band_idx) or [])
        rng.shuffle(band_winners)
        seeds = [s for s, b in R16_BAND_FOR_SEEDS.items() if b == band_idx]
        seeds = sorted(seeds)
        while len(band_winners) < len(seeds):
            band_winners.append(f"Playoff Winner {band_idx}-{len(band_winners)+1}")
        for i, seed_pos in enumerate(seeds):
            r16_pairings.append(
                {
                    "seed_pos": seed_pos,
                    "seed_team": seed_team(ranked, seed_pos),
                    "playoff_winner": band_winners[i],
                    "band": band_idx,
                }
            )

    def _half_matches(seed_order: tuple[int, ...]):
        matches = []
        for seed_pos in seed_order:
            pairing = next(p for p in r16_pairings if p["seed_pos"] == seed_pos)
            high = pairing["seed_team"]
            low = pairing["playoff_winner"]
            # Higher league-phase seed is "high" for home-2nd-leg purposes.
            result = resolve_two_legged_tie(high, low, rng, predict_fn)
            matches.append(
                {
                    "seed_pos": seed_pos,
                    "home_second_leg": high,
                    "away_second_leg": low,
                    **result,
                }
            )
        return matches

    half_a_r16 = _half_matches(R16_HALF_A_SEEDS)
    half_b_r16 = _half_matches(R16_HALF_B_SEEDS)

    def _advance_half(r16_matches: list[dict], label: str):
        # QF: (1st R16 vs 2nd), (3rd vs 4th) in the half seed order list.
        # Half A (1,8,4,5): seed1-path vs seed8-path, seed4-path vs seed5-path
        # → 1 and 2 cannot meet before the final; 3/4 cannot meet 1/2 before SF.
        qf = []
        for i in range(0, 4, 2):
            a = r16_matches[i]["winner"]
            b = r16_matches[i + 1]["winner"]
            high, low = _order_by_seed(a, b, seed_ranks)
            result = resolve_two_legged_tie(high, low, rng, predict_fn)
            qf.append({"slot": i // 2 + 1, "half": label, **result})
        sf_a_team = qf[0]["winner"]
        sf_b_team = qf[1]["winner"]
        sf_high, sf_low = _order_by_seed(sf_a_team, sf_b_team, seed_ranks)
        sf = resolve_two_legged_tie(sf_high, sf_low, rng, predict_fn)
        sf["half"] = label
        return qf, sf

    qf_a, sf_a = _advance_half(half_a_r16, "A")
    qf_b, sf_b = _advance_half(half_b_r16, "B")

    # Final — single leg at neutral venue; sample then pens if level.
    final_home, final_away = sf_a["winner"], sf_b["winner"]
    fh, fa, fmeta = _sample_score(final_home, final_away, rng, predict_fn)
    if fh > fa:
        champion = final_home
        pens = False
    elif fa > fh:
        champion = final_away
        pens = False
    else:
        pens = True
        champion = final_home if float(rng.random()) < 0.5 else final_away
    runner_up = final_away if champion == final_home else final_home

    participants = {
        "playoff": sorted({t["high_team"] for t in playoff_ties} | {t["low_team"] for t in playoff_ties}),
        "round_of_16": sorted(
            {m["high_team"] for m in half_a_r16 + half_b_r16}
            | {m["low_team"] for m in half_a_r16 + half_b_r16}
        ),
        "quarterfinal": sorted({m["high_team"] for m in qf_a + qf_b} | {m["low_team"] for m in qf_a + qf_b}),
        "semifinal": sorted({sf_a["high_team"], sf_a["low_team"], sf_b["high_team"], sf_b["low_team"]}),
        "final": sorted({final_home, final_away}),
        "runner_up": [runner_up],
        "winner": [champion],
    }

    return {
        "champion": champion,
        "runner_up": runner_up,
        "participants": participants,
        "playoff": playoff_results,
        "round_of_16": {"half_a": half_a_r16, "half_b": half_b_r16},
        "quarterfinals": {"half_a": qf_a, "half_b": qf_b},
        "semifinals": {"half_a": sf_a, "half_b": sf_b},
        "final": {
            "home_team": final_home,
            "away_team": final_away,
            "home_goals": fh,
            "away_goals": fa,
            "winner": champion,
            "decided_by_penalties": pens,
            **fmeta,
        },
    }


def empty_finish_counts(teams: list[str]) -> dict[str, dict[str, int]]:
    stages = (
        "winner",
        "runner_up",
        "final",
        "semifinal",
        "quarterfinal",
        "round_of_16",
        "playoff",
        "missed_knockout",
    )
    return {team: {s: 0 for s in stages} for team in teams}


def accumulate_finish_counts(counts: dict[str, dict[str, int]], sim: dict, all_teams: list[str]) -> None:
    """Update per-team knockout finish tallies from one simulation."""
    parts = sim.get("participants") or {}
    champ = str(sim.get("champion") or "").strip()
    runner = str(sim.get("runner_up") or "").strip()
    playoff = set(parts.get("playoff") or [])
    r16 = set(parts.get("round_of_16") or [])
    qf = set(parts.get("quarterfinal") or [])
    sf = set(parts.get("semifinal") or [])
    finalists = set(parts.get("final") or [])

    for team in all_teams:
        if not team or team.startswith("Seed "):
            continue
        bucket = counts.setdefault(
            team,
            {s: 0 for s in (
                "winner", "runner_up", "final", "semifinal",
                "quarterfinal", "round_of_16", "playoff", "missed_knockout",
            )},
        )
        if team == champ:
            bucket["winner"] += 1
        if team == runner:
            bucket["runner_up"] += 1
        if team in finalists:
            bucket["final"] += 1
        if team in sf:
            bucket["semifinal"] += 1
        if team in qf:
            bucket["quarterfinal"] += 1
        if team in r16:
            bucket["round_of_16"] += 1
        if team in playoff:
            bucket["playoff"] += 1
        if team not in playoff and team not in r16:
            # Outside top 24 on this sim's table.
            bucket["missed_knockout"] += 1


def finish_counts_to_probabilities(counts: dict[str, dict[str, int]], runs: int) -> dict[str, dict[str, float]]:
    runs = max(1, int(runs))
    out = {}
    for team, stages in counts.items():
        out[team] = {
            stage: round((int(n) / runs) * 100.0, 2)
            for stage, n in stages.items()
        }
    return out


# Round labels matching Website/competition_rules._normalize_cup_stage_key.
REACH_ROUND_LABELS = (
    ("playoff", "Playoff"),
    ("round_of_16", "Round of 16"),
    ("quarterfinal", "Quarterfinals"),
    ("semifinal", "Semifinals"),
    ("final", "Final"),
    ("winner", "Winner"),
)


def finish_probs_to_round_reach(finish_probs: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Nested reach maps keyed by consumer-facing round names."""
    reach: dict[str, dict[str, float]] = {label: {} for _, label in REACH_ROUND_LABELS}
    for team, stages in (finish_probs or {}).items():
        if not team or str(team).startswith("Seed "):
            continue
        for key, label in REACH_ROUND_LABELS:
            reach[label][team] = float(stages.get(key, 0.0) or 0.0)
    return reach


def finish_probs_to_elimination(finish_probs: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Terminal finish mass per team (Playoff / RO16 / … / Winner)."""
    out: dict[str, dict[str, float]] = {}
    for team, stages in (finish_probs or {}).items():
        if not team or str(team).startswith("Seed "):
            continue
        playoff = float(stages.get("playoff", 0.0) or 0.0)
        r16 = float(stages.get("round_of_16", 0.0) or 0.0)
        qf = float(stages.get("quarterfinal", 0.0) or 0.0)
        sf = float(stages.get("semifinal", 0.0) or 0.0)
        final = float(stages.get("final", 0.0) or 0.0)
        winner = float(stages.get("winner", 0.0) or 0.0)
        runner = float(stages.get("runner_up", 0.0) or 0.0)
        missed = float(stages.get("missed_knockout", 0.0) or 0.0)
        # Nested reach → terminal slices. Top-8 never enter playoff (playoff=0).
        out[team] = {
            "League Phase": round(max(0.0, missed), 2),
            "Playoff": round(max(0.0, playoff - r16), 2),
            "Round of 16": round(max(0.0, r16 - qf), 2),
            "Quarterfinals": round(max(0.0, qf - sf), 2),
            "Semifinals": round(max(0.0, sf - final), 2),
            "Final": round(max(0.0, runner if runner > 0 else final - winner), 2),
            "Champion": round(max(0.0, winner), 2),
        }
    return out
