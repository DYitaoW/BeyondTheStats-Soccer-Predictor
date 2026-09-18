# Cup competition API — `/api/cup-data`

**Status:** implemented (Phase A contract + wiring)  
**Branch context:** PR #200 / cup pipeline fixes  
**Goal:** Serve cup competitions via a dedicated `/api/cup-data/` twin of `/api/league-data`, with `format_style` driving which blocks are present, and **stage-reach position odds** (Winner / Final / SF / QF / …) instead of fake league tables for pure knockouts.

---

## 1. Why a separate endpoint

Leagues and cups share a lot of shape (`ok`, `competition`, `format`, `predicted`, `real`, `bracket`, `fixtures`), but cups need different primary odds:

| | League (`/api/league-data`) | Cup (`/api/cup-data`) |
|---|---|---|
| Primary odds | Table place (1st…Nth) | Stage reach (Winner, Final, SF, …) |
| Table | Always (for leagues) | Only when `format_style` has a phase table |
| Format discriminator | `standings_layout` | `format_style` |

Keeping parsers similar means frontend can reuse the same nesting; only `format.format_style` / `position_odds.stages` / `semantics` change.

Prefer **`/api/cup-data/<competition>`** for all cup competitions. `/api/league-data` may still respond for hybrid cups during transition, but cup-data is the contract to migrate to.

---

## 2. Format styles (three product shapes)

Authoritative helper: `competition_rules.cup_format_style_for`.

| `format_style` | Meaning | Examples |
|---|---|---|
| `knockout` | Pure bracket; **no table** | FA Cup, League Cup, Copa, Pokal, Coupe, Coppa, US Open Cup |
| `table_knockout` | League / dual phase table → knockout | UCL / UEL / UECL, Leagues Cup |
| `group_knockout` | Group stage → knockout | WC / continental majors (when enabled) |

Exposed on every payload as:

```json
"format": {
  "competition_type": "cup",
  "format_style": "knockout",
  "has_table": false,
  "has_groups": false,
  "position_stages": ["Winner", "Final", "SF", "QF", "RO16"],
  "stages": ["..."],
  "knockout_rounds": ["..."],
  "draw_rules": { "two_leg_rounds": [], "final_neutral": true, "...": "..." },
  "standings_layout": "knockout_bracket",
  "notes": ["..."]
}
```

`draw_rules` carries cup matchup rules (two-leg rounds, neutral final, no-draws, seedings, advance_per_table) so clients and sims stay aligned with each competition’s pairing rules.

---

## 3. API contract

### 3.1 Index

`GET /api/cup-data` → `{ ok, cups: [{ competition, format_style, position_stages, path }], count }`

### 3.2 Detail

`GET /api/cup-data/<competition>`

```json
{
  "ok": true,
  "competition": "England/FA Cup",
  "format": { "format_style": "knockout", "has_table": false, "position_stages": ["Winner", "Final", "SF", "QF", "RO16"], "...": "..." },
  "predicted": {
    "table": [],
    "groups": null,
    "winner": {
      "champion": "Arsenal",
      "probabilities": { "Arsenal": 18.5, "Man City": 14.2 },
      "simulations_run": 2500
    },
    "winners_odds": [
      {
        "team": "Arsenal",
        "win_cup_pct": 18.5,
        "win_league_pct": 18.5,
        "final_pct": 30.0,
        "sf_pct": 45.0,
        "qf_pct": 60.0,
        "most_likely_position": "QF",
        "most_likely_position_pct": 28.0,
        "stage_odds": { "Winner": 18.5, "Final": 30.0, "SF": 45.0, "QF": 60.0 }
      }
    ],
    "position_odds": {
      "semantics": "reach",
      "stages": ["Winner", "Final", "SF", "QF", "RO16"],
      "simple": {
        "Winner": [{"team": "Arsenal", "pct": 18.5}],
        "SF": [{"team": "Arsenal", "pct": 45.0}]
      },
      "detailed": [
        {
          "team": "Arsenal",
          "odds": { "Winner": 18.5, "Final": 30.0, "SF": 45.0, "QF": 60.0, "RO16": 80.0 },
          "most_likely_position": "QF",
          "most_likely_position_pct": 28.0
        }
      ]
    }
  },
  "real": { "standings": null },
  "bracket": { "projected": {}, "knockout": {}, "odds_knockout": {}, "real_knockout": {} },
  "fixtures": [],
  "predicted_table": [],
  "position_odds": { "...": "same as predicted.position_odds" },
  "winners_odds": [],
  "champion": "Arsenal",
  "winner_probabilities": {},
  "simulations_run": 2500
}
```

### 3.3 Position-odds semantics

- Keys are **cup stages**, not table places: `Winner`, `Final`, `SF`, `QF`, `RO16`, `RO32`, `RO64`, `Playoff` (subset per cup via `format.position_stages`).
- `semantics: "reach"` — pct = likelihood of **reaching** that stage (`Winner` = champion).
- Nested: `Winner ⊆ Final ⊆ SF ⊆ …`
- `most_likely_position` is the most likely **terminal** finish (from elimination mass when present; otherwise inferred from reach diffs), not max reach.
- Secondary raw sim maps kept when available: `elimination_round_odds`, `round_reach_probabilities`.

### 3.4 Hybrid cups (`table_knockout` / `group_knockout`)

When `has_table` is true:

- `predicted.table` / `predicted.groups` / `real.standings` populated like league-data
- `predicted.table_position_odds` — phase table place odds (1st…Nth)
- `predicted.position_odds` — **still** knockout stage-reach odds (primary cup analog)

---

## 4. Data sources

| Field | Source |
|---|---|
| Winner / stage reach | `projected_cup_brackets.json` sims (`winner_probabilities`, `round_reach_probabilities`, `elimination_round_*`) |
| Bracket topology | Same + knockout helpers (`knockout.py`) |
| Phase tables | Projected cup/league-phase tables (UEFA, Leagues Cup only) |
| Matchup rules | `config._CUP_FORMATS` / `cup_format()` → `format.draw_rules` |

Caches: `Output/CupData/<slug>.json`, rebuilt after pipeline publish via `rebuild_cup_data_caches` (Daily_Pipeline + Run_All_Pipeline), mirroring LeagueData.

---

## 5. Simulation / matchup rules (ongoing)

Full brackets are often unknowable early (randomized draws between rounds). Approach:

1. Use **simulated cup results** for winner + stage-reach odds (not a fake full bracket).
2. When building / simulating matchups, honor each cup’s rules (`two_leg_rounds`, seedings, advance_per_table, no-draws, etc.).
3. Known bracket rounds stay locked; TBD slots use remaining-field draws consistent with that cup.

**Phase B (follow-on):** richer domestic `sim_rounds` trees so early FA Cup / Copa fields produce non-empty odds more reliably.

---

## 6. Frontend migration notes

1. Cup tiles / detail → `GET /api/cup-data/<comp>` (list via `/api/cup-data`).
2. Branch on `format.format_style` / `format.has_table`.
3. Reuse league `position_odds` UI with stage columns from `format.position_stages` (label “Win cup” / “Reach SF”, not “1st”).
4. Hide table tab when `has_table: false`.
5. `winners_odds[].win_cup_pct` is canonical; `win_league_pct` kept as alias.

---

## 7. Work phases

| Phase | Status |
|---|---|
| **A** — `/api/cup-data`, `format_style`, stage-reach `position_odds`, cache rebuild | **Done** |
| **B** — Domestic sim_rounds / stronger early-round odds | Next |
| **C** — UEFA / Leagues Cup KO finish odds from seeded trees | Next |
| **D** — Frontend migrate to cup-data | Next |
| **E** — Trim league-data cup dual-serve / legacy aliases | Later |

---

## 8. Success criteria

- FA Cup `/api/cup-data/England/FA Cup` → `format_style: knockout`, `has_table: false`, empty table, stage `position_odds` when sims exist
- UCL `/api/cup-data/Europe/Champions League` → `format_style: table_knockout`, phase table **and** stage-reach odds
- Leagues Cup → `table_knockout` + dual tables
- Pipeline rebuild writes `Output/CupData/*.json` after publish
- No pure-knockout cup invents a W/D/L league table
