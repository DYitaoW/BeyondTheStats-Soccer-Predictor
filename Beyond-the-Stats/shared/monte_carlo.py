"""Vectorized (numpy) Monte Carlo season simulation shared by the projection pipelines.

The pipelines used to simulate one season at a time in pure Python (a dict
table per run, ``random.choices`` per fixture, a full sort per run). That
capped smaller leagues at 150-250 runs (title odds +-4 points of noise). This
engine samples every run x fixture at once and ranks every run with one
``np.lexsort``, so 10,000+ runs take well under a second for a 20-team league.

Semantics match the per-run Python loops it replaces:

* outcome ~ (H, D, A) probabilities; optional early-season jitter on the
  probabilities per run and fixture (``_jitter_probs``: uniform +-scale on H/A,
  +-0.6*scale on D, floor 0.01, renormalised);
* scoreline = ``coerce_scoreline(outcome, pred_home_goals, pred_away_goals)``
  (the rounded expected score, bumped to agree with the sampled outcome);
* ranking: points, goal difference, goals for, name — or, for head-to-head
  leagues, points then the head-to-head mini-table among all clubs level on
  points (points, GD, goals for), then overall GD, GF, name. Real matches
  count towards head-to-head.

Returns the same ``(stat_sums, position_counts)`` shape as the old loops so
callers' averaging code is unchanged.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Iterable, Optional, Sequence

import numpy as np

STAT_KEYS = ("P", "W", "D", "L", "GF", "GA", "GD", "Pts", "PlayedReal", "PlayedPred")
DEFAULT_CHUNK = 4000


def stable_seed(*parts: object) -> int:
    """Deterministic 63-bit seed from arbitrary parts (competition, date, ...)."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(digest[:15], 16)


def coerce_scoreline(result: str, base_hg: float, base_ag: float) -> tuple[int, int]:
    """Rounded expected score adjusted so it agrees with ``result`` (H/D/A)."""
    try:
        hg = max(0, int(round(float(base_hg))))
    except (TypeError, ValueError):
        hg = 1
    try:
        ag = max(0, int(round(float(base_ag))))
    except (TypeError, ValueError):
        ag = 1
    if result == "H" and hg <= ag:
        hg = ag + 1
    elif result == "A" and ag <= hg:
        ag = hg + 1
    elif result == "D":
        ag = hg
    return hg, ag


def _fixture_arrays(fixtures: Sequence[dict], index: dict):
    n = len(fixtures)
    home = np.empty(n, dtype=np.int64)
    away = np.empty(n, dtype=np.int64)
    probs = np.empty((n, 3), dtype=np.float64)
    goals = np.empty((n, 3, 2), dtype=np.int64)  # outcome (H, D, A) -> (hg, ag)
    for i, fx in enumerate(fixtures):
        home[i] = index[fx["home_team"]]
        away[i] = index[fx["away_team"]]
        p = fx.get("probs") or {}
        row = [max(0.0, float(p.get(k, 0.0) or 0.0)) for k in ("H", "D", "A")]
        total = sum(row)
        probs[i] = [x / total for x in row] if total > 0 else [1 / 3, 1 / 3, 1 / 3]
        for j, res in enumerate(("H", "D", "A")):
            goals[i, j] = coerce_scoreline(res, fx.get("pred_home_goals", 1), fx.get("pred_away_goals", 1))
    return home, away, probs, goals


def _sample_outcomes(rng, probs: np.ndarray, runs: int, jitter_scale: float) -> np.ndarray:
    """(runs, fixtures) int8 outcomes: 0 = home win, 1 = draw, 2 = away win."""
    n_fx = probs.shape[0]
    if jitter_scale > 0.0:
        p = np.broadcast_to(probs, (runs, n_fx, 3)).copy()
        p[..., 0] += rng.uniform(-jitter_scale, jitter_scale, (runs, n_fx))
        p[..., 1] += rng.uniform(-0.6 * jitter_scale, 0.6 * jitter_scale, (runs, n_fx))
        p[..., 2] += rng.uniform(-jitter_scale, jitter_scale, (runs, n_fx))
        np.maximum(p, 0.01, out=p)
        p /= p.sum(axis=2, keepdims=True)
        cut_h = p[..., 0]
        cut_d = cut_h + p[..., 1]
    else:
        cut_h = np.broadcast_to(probs[:, 0], (runs, n_fx))
        cut_d = np.broadcast_to(probs[:, 0] + probs[:, 1], (runs, n_fx))
    u = rng.random((runs, n_fx))
    out = np.full((runs, n_fx), 2, dtype=np.int8)
    out[u < cut_d] = 1
    out[u < cut_h] = 0
    return out


def simulate_season(
    teams: Iterable[str],
    base_table: dict,
    fixtures: Sequence[dict],
    runs: int,
    *,
    h2h: bool = False,
    real_matches: Optional[Sequence[tuple]] = None,
    jitter_scale: float = 0.0,
    seed: Optional[int] = None,
    chunk: int = DEFAULT_CHUNK,
    collect_tables: bool = False,
):
    """Simulate ``runs`` season finishes.

    ``base_table``: ``{team: {P, W, D, L, GF, GA, GD, Pts, PlayedReal, PlayedPred}}``
    (missing keys count as 0). ``fixtures``: dicts with ``home_team``,
    ``away_team``, ``probs`` ``{H, D, A}``, ``pred_home_goals``,
    ``pred_away_goals``. ``real_matches``: ``(home, away, hg, ag)`` already
    played (head-to-head only).

    Returns ``(stat_sums, position_counts)`` — ``stat_sums[team][key]`` summed
    over runs, ``position_counts[team][position]`` — plus, when
    ``collect_tables`` is set, a list of per-run final tables
    ``{team: {Pts, W, D, L, GF, GA, GD}}``.
    """
    runs = max(1, int(runs))
    names = list(dict.fromkeys(list(teams) + list(base_table.keys())))
    for fx in fixtures:
        for side in ("home_team", "away_team"):
            if fx[side] not in names:
                names.append(fx[side])
    index = {t: i for i, t in enumerate(names)}
    n_teams = len(names)
    rng = np.random.default_rng(seed)

    base = np.zeros((len(STAT_KEYS), n_teams), dtype=np.int64)
    for t, stats in base_table.items():
        for k, key in enumerate(STAT_KEYS):
            try:
                base[k, index[t]] = int(round(float((stats or {}).get(key, 0) or 0)))
            except (TypeError, ValueError):
                pass
    # Alphabetical rank: the final tiebreak of the Python rank_table.
    name_rank = np.empty(n_teams, dtype=np.int64)
    name_rank[np.argsort(np.array(names, dtype=object))] = np.arange(n_teams)

    stat_sums = {t: defaultdict(float) for t in names}
    position_counts = {t: defaultdict(int) for t in names}
    tables: list[dict] = []

    # Constant head-to-head contribution from already played matches.
    real_h2h = np.zeros((3, n_teams, n_teams), dtype=np.int64)  # pts, gd, gf of i vs j
    if h2h and real_matches:
        for m in real_matches:
            try:
                hi, ai, hg, ag = index[m[0]], index[m[1]], int(m[2]), int(m[3])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            hp, ap = (3, 0) if hg > ag else ((0, 3) if ag > hg else (1, 1))
            real_h2h[0, hi, ai] += hp
            real_h2h[0, ai, hi] += ap
            real_h2h[1, hi, ai] += hg - ag
            real_h2h[1, ai, hi] += ag - hg
            real_h2h[2, hi, ai] += hg
            real_h2h[2, ai, hi] += ag

    if fixtures:
        home, away, probs, goals = _fixture_arrays(fixtures, index)
        n_fx = len(fixtures)
        # Incidence matrices: fixture -> team.
        h_inc = np.zeros((n_fx, n_teams), dtype=np.int64)
        a_inc = np.zeros((n_fx, n_teams), dtype=np.int64)
        h_inc[np.arange(n_fx), home] = 1
        a_inc[np.arange(n_fx), away] = 1
    else:
        n_fx = 0

    done = 0
    while done < runs:
        r = min(int(chunk), runs - done)
        done += r
        tot = np.broadcast_to(base[:, None, :], (len(STAT_KEYS), r, n_teams)).copy()
        if n_fx:
            out = _sample_outcomes(rng, probs, r, float(jitter_scale or 0.0))
            fx_idx = np.arange(n_fx)[None, :]
            hg = goals[fx_idx, out, 0]
            ag = goals[fx_idx, out, 1]
            hw = (out == 0).astype(np.int64)
            dr = (out == 1).astype(np.int64)
            aw = (out == 2).astype(np.int64)
            k = {key: i for i, key in enumerate(STAT_KEYS)}
            ones = np.ones((r, n_fx), dtype=np.int64)
            tot[k["P"]] += ones @ h_inc + ones @ a_inc
            tot[k["PlayedPred"]] += ones @ h_inc + ones @ a_inc
            tot[k["W"]] += hw @ h_inc + aw @ a_inc
            tot[k["D"]] += dr @ h_inc + dr @ a_inc
            tot[k["L"]] += aw @ h_inc + hw @ a_inc
            tot[k["GF"]] += hg @ h_inc + ag @ a_inc
            tot[k["GA"]] += ag @ h_inc + hg @ a_inc
            tot[k["Pts"]] += (3 * hw + dr) @ h_inc + (3 * aw + dr) @ a_inc
        k = {key: i for i, key in enumerate(STAT_KEYS)}
        tot[k["GD"]] = tot[k["GF"]] - tot[k["GA"]]
        pts, gd, gf = tot[k["Pts"]], tot[k["GD"]], tot[k["GF"]]

        keys = [np.broadcast_to(name_rank, (r, n_teams)), -gf, -gd]
        if h2h:
            # h2h[r, i, j] = points / GD / GF of team i against team j.
            hh = np.broadcast_to(real_h2h[:, None], (3, r, n_teams, n_teams)).copy()
            if n_fx:
                hp = 3 * hw + dr
                ap = 3 * aw + dr
                rows = np.repeat(np.arange(r), n_fx)
                hi = np.tile(home, r)
                ai = np.tile(away, r)
                np.add.at(hh[0], (rows, hi, ai), hp.ravel())
                np.add.at(hh[0], (rows, ai, hi), ap.ravel())
                np.add.at(hh[1], (rows, hi, ai), (hg - ag).ravel())
                np.add.at(hh[1], (rows, ai, hi), (ag - hg).ravel())
                np.add.at(hh[2], (rows, hi, ai), hg.ravel())
                np.add.at(hh[2], (rows, ai, hi), ag.ravel())
            level = (pts[:, :, None] == pts[:, None, :])  # (r, i, j) tied on points
            mini = (hh * level[None]).sum(axis=3)        # (3, r, i)
            keys += [-mini[2], -mini[1], -mini[0]]
        keys.append(-pts)
        order = np.lexsort(keys, axis=-1)               # (r, n_teams): team index by position
        positions = np.empty_like(order)
        positions[np.arange(r)[:, None], order] = np.arange(1, n_teams + 1)[None, :]

        sums = tot.sum(axis=1)
        for t, i in index.items():
            for kk, key in enumerate(STAT_KEYS):
                stat_sums[t][key] += float(sums[kk, i])
            counts = np.bincount(positions[:, i], minlength=n_teams + 1)
            for pos in np.nonzero(counts)[0]:
                position_counts[t][int(pos)] += int(counts[pos])
        if collect_tables:
            for run in range(r):
                tables.append({
                    t: {key: int(tot[k[key], run, i]) for key in ("Pts", "W", "D", "L", "GF", "GA", "GD")}
                    for t, i in index.items()
                })

    if collect_tables:
        return stat_sums, position_counts, tables
    return stat_sums, position_counts
