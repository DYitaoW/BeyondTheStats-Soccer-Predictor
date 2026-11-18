"""NWSL playoff bracket projection (seeded from the regular-season table).

Format (8 teams, single-elimination, higher seed hosts every round):

    Quarterfinals  QF1 1v8   QF2 4v5   QF3 2v7   QF4 3v6
    Semifinals     SF1 = W(QF1) v W(QF2)   SF2 = W(QF3) v W(QF4)
    Championship   W(SF1) v W(SF2)

Seeds come from the regular-season table ranked by points, wins, goal
difference, goals for (reproduces the real 2026 seeding). Real playoff results
already played (from the ESPN season CSV, ``Stage == "playoffs"``) are kept;
only the remaining ties are simulated. A knockout draw is resolved in
proportion to the two sides' win probabilities (extra time / penalties).

Pure logic: the caller supplies ``pair_probs(home, away) -> (p_home, p_draw,
p_away)`` (the trained model in Project_League_Table).
"""
from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

PLAYOFF_TEAMS = 8
# (label, round, home source, away source); sources are seeds or prior match labels.
BRACKET = [
    ("QF1", "quarterfinals", 1, 8),
    ("QF2", "quarterfinals", 4, 5),
    ("QF3", "quarterfinals", 2, 7),
    ("QF4", "quarterfinals", 3, 6),
    ("SF1", "semifinals", "QF1", "QF2"),
    ("SF2", "semifinals", "QF3", "QF4"),
    ("F", "final", "SF1", "SF2"),
]
ROUND_ORDER = ["made_playoffs", "semifinals", "final", "champion"]

PairProbs = Callable[[str, str], tuple]


def rank_table(standings: dict) -> list[str]:
    """Regular-season order: points, wins, goal difference, goals for."""
    return sorted(
        standings,
        key=lambda t: (standings[t].get("Pts", 0), standings[t].get("W", 0),
                       standings[t].get("GD", 0), standings[t].get("GF", 0)),
        reverse=True,
    )


def real_playoff_results(raw_rows: list[dict]) -> dict:
    """frozenset({home, away}) -> {winner, score, date, match_id} for played playoff games."""
    out: dict = {}
    for row in raw_rows:
        if str(row.get("Stage", "")).strip() != "playoffs":
            continue
        home, away = str(row.get("Home", "")).strip(), str(row.get("Away", "")).strip()
        res = str(row.get("Res", "")).strip()
        if not home or not away or res not in ("H", "D", "A"):
            continue
        side = str(row.get("Winner", "")).strip() or (res if res in ("H", "A") else "")
        if side not in ("H", "A"):
            continue  # draw with no recorded winner: cannot use
        score = f"{row.get('HG')}-{row.get('AG')}"
        if str(row.get("HPens", "")).strip() and str(row.get("APens", "")).strip():
            score += f" ({row.get('HPens')}-{row.get('APens')} pens)"
        out[frozenset((home, away))] = {
            "winner": home if side == "H" else away,
            "home_team": home, "away_team": away, "score": score,
            "date": row.get("Date", ""), "match_id": str(row.get("MatchId", "")),
        }
    return out


def scheduled_playoff_games(raw_rows: list[dict]) -> dict:
    """frozenset({home, away}) -> {date, time, match_id} for announced, unplayed playoff games."""
    out = {}
    for row in raw_rows:
        if str(row.get("Stage", "")).strip() != "playoffs" or str(row.get("Res", "")).strip() in ("H", "D", "A"):
            continue
        home, away = str(row.get("Home", "")).strip(), str(row.get("Away", "")).strip()
        if home and away:
            out[frozenset((home, away))] = {
                "date": row.get("Date", ""), "time": row.get("Time", ""),
                "kickoff_utc": row.get("KickoffUTC", ""), "match_id": str(row.get("MatchId", "")),
            }
    return out


def advance_probability(pair_probs: PairProbs, home: str, away: str) -> float:
    """P(home side advances) in a single knockout game."""
    try:
        ph, pd_, pa = (float(x) for x in pair_probs(home, away))
    except Exception:
        return 0.5
    tot = ph + pd_ + pa
    if tot <= 0:
        return 0.5
    ph, pd_, pa = ph / tot, pd_ / tot, pa / tot
    decisive = ph + pa
    return ph + pd_ * (ph / decisive if decisive > 0 else 0.5)


def _play_bracket(seeds: list[str], real: dict, choose: Callable[[str, str], str]) -> list[dict]:
    """Resolve every bracket game: real result if that pairing was played, else ``choose``."""
    winners: dict[str, str] = {}
    seed_of = {team: i + 1 for i, team in enumerate(seeds)}
    games = []
    for label, round_name, src_home, src_away in BRACKET:
        a = seeds[src_home - 1] if isinstance(src_home, int) else winners[src_home]
        b = seeds[src_away - 1] if isinstance(src_away, int) else winners[src_away]
        home, away = (a, b) if seed_of[a] <= seed_of[b] else (b, a)  # higher seed hosts
        actual = real.get(frozenset((home, away)))
        winner = actual["winner"] if actual else choose(home, away)
        winners[label] = winner
        games.append({
            "label": label, "round": round_name,
            "home_team": home, "away_team": away,
            "home_seed": seed_of[home], "away_seed": seed_of[away],
            "winner": winner, "played": bool(actual),
            "score": actual["score"] if actual else None,
            "match_id": actual["match_id"] if actual else None,
        })
    return games


def project_playoffs(
    final_tables: list[list[str]],
    pair_probs: PairProbs,
    raw_rows: list[dict],
    seed: int = 7,
    sim_runs: int = 5000,
) -> dict:
    """Monte Carlo playoff projection.

    ``final_tables``: simulated regular-season final rankings (a single
    ranking once the regular season is complete). ``sim_runs`` brackets are
    played, cycling through the rankings; each seeds the top 8 and keeps the
    real results.
    """
    rng = random.Random(seed)
    real = real_playoff_results(raw_rows)
    scheduled = scheduled_playoff_games(raw_rows)
    adv_cache: dict = {}

    def p_adv(home, away):
        key = (home, away)
        if key not in adv_cache:
            adv_cache[key] = advance_probability(pair_probs, home, away)
        return adv_cache[key]

    if not final_tables:
        raise ValueError("project_playoffs needs at least one regular-season ranking")
    runs = max(1, int(sim_runs), len(final_tables))
    reach = {r: defaultdict(int) for r in ROUND_ORDER}
    for i in range(runs):
        ranking = final_tables[i % len(final_tables)]
        seeds = list(ranking[:PLAYOFF_TEAMS])
        if len(seeds) < PLAYOFF_TEAMS:
            continue
        for team in seeds:
            reach["made_playoffs"][team] += 1
        games = _play_bracket(seeds, real, lambda h, a: h if rng.random() < p_adv(h, a) else a)
        for g in games:
            if g["round"] == "quarterfinals":
                reach["semifinals"][g["winner"]] += 1
            elif g["round"] == "semifinals":
                reach["final"][g["winner"]] += 1
            else:
                reach["champion"][g["winner"]] += 1

    pct = {r: {t: round(100.0 * n / runs, 1) for t, n in sorted(c.items(), key=lambda kv: -kv[1])}
           for r, c in reach.items()}

    # Most likely bracket: modal regular-season ranking, favourite advances.
    modal = final_tables[0] if len(final_tables) == 1 else _modal_ranking(final_tables)
    seeds = list(modal[:PLAYOFF_TEAMS])
    games = _play_bracket(seeds, real, lambda h, a: h if p_adv(h, a) >= 0.5 else a) if len(seeds) == PLAYOFF_TEAMS else []
    for g in games:
        g["prob_home_advances"] = None if g["played"] else round(100.0 * p_adv(g["home_team"], g["away_team"]), 1)
        sched = scheduled.get(frozenset((g["home_team"], g["away_team"])))
        if sched and not g["played"]:
            g.update({"scheduled": True, **{k: sched[k] for k in ("date", "time", "kickoff_utc", "match_id")}})

    played = sum(1 for g in games if g["played"])
    status = "complete" if games and played == len(games) else ("in_progress" if played else "projected")
    champion = next((g["winner"] for g in games if g["round"] == "final" and g["played"]), None)
    elimination: dict = defaultdict(dict)
    teams = {t for r in reach.values() for t in r}
    for t in teams:
        made = pct["made_playoffs"].get(t, 0.0)
        sf, fi, ch = (pct[r].get(t, 0.0) for r in ("semifinals", "final", "champion"))
        elimination[t] = {k: v for k, v in {
            "missed_playoffs": round(100.0 - made, 1), "quarterfinals": round(made - sf, 1),
            "semifinals": round(sf - fi, 1), "final": round(fi - ch, 1), "champion": ch,
        }.items() if v > 0}
    return {
        "format": "nwsl_playoffs",
        "status": status,
        "champion": champion,
        "seeds": [{"seed": i + 1, "team": t} for i, t in enumerate(seeds)],
        "quarterfinals": [g for g in games if g["round"] == "quarterfinals"],
        "semifinals": [g for g in games if g["round"] == "semifinals"],
        "final": next((g for g in games if g["round"] == "final"), None),
        "championship_winner_probabilities": pct["champion"],
        "make_playoffs_probabilities": pct["made_playoffs"],
        "round_reach_probabilities": pct,
        "elimination_round_probabilities": dict(elimination),
        "simulations_run": runs,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _modal_ranking(final_tables: list[list[str]]) -> list[str]:
    """Order teams by average finishing position across runs."""
    pos = defaultdict(list)
    for ranking in final_tables:
        for i, t in enumerate(ranking):
            pos[t].append(i)
    return sorted(pos, key=lambda t: sum(pos[t]) / len(pos[t]))
