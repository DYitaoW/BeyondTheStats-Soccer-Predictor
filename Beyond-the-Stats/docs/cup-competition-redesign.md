# Cup competition redesign — design draft

**Status:** draft for review (no implementation yet)  
**Branch context:** follows cup pipeline fixes in PR #200  
**Goal:** Make knockout cups first-class in `/api/league-data` and the website without fake league tables. Replace table-position odds with **finish-depth odds** (champion, runner-up, SF, QF, …).

---

## 1. Problem

Most cups are **knockout brackets**, not leagues:

| Keep a table / group / league-phase | Knockout-only (no table) |
|---|---|
| UEFA CL / EL / ECL (league phase → KO) | FA Cup, League Cup, Copa del Rey, DFB-Pokal, Coupe de France, Coppa Italia, US Open Cup |
| Leagues Cup (dual Phase One → QF+) | |
| National majors with groups (WC, Euros, Copa América, AFCON, Gold Cup, …) | |

Today the shared league-data shape still leans on **league concepts**:

- `predicted.table` / `position_odds` / `winners_odds[].win_league_pct`
- Domestic brackets often only expose upcoming/recent fixtures as `rounds`, so Monte Carlo rarely has a real KO tree → empty or weak winner odds
- Frontend cup page: `hasTable: false` → message + bracket only; **no finish-odds UI**
- League-data already *partially* carries `elimination_round_odds` / `round_reach_probabilities` from `Track_Cup_Results`, but they are not the primary contract and are not surfaced well

User intent (from earlier): keep as much of the league API as possible, but for knockout cups **omit tables** and expose odds for finishing champion, runner-up, semi, quarter, …

---

## 2. Design principles

1. **One endpoint, two modes** — `/api/league-data/<comp>` stays unified; `format.standings_layout` (and a new `odds_model`) tell clients which mode to use.
2. **Tables only when they mean something** — never invent a W/D/L table for pure knockouts.
3. **Finish depth is the cup analog of position odds** — champion ≈ “1st”; runner-up ≈ “2nd”; reaching SF/QF ≈ deep runs.
4. **Reuse existing simulation machinery** where possible (`_simulate_cup_tournament` already tracks champion, elimination round, round reach).
5. **Backward compatible for a transition** — keep legacy keys populated or aliased until mobile/web migrate.
6. **Don’t block on perfect brackets** — early rounds (large fields, TBD draws) still need honest odds (wider uncertainty / remaining-field sims).

---

## 3. Competition taxonomy (authoritative)

Encode once (config / `competition_rules`) and reuse everywhere:

```text
odds_model:
  league_table          # PL, La Liga, … — existing position odds
  league_phase_then_ko  # UCL/UEL/UECL — table + KO finish odds after phase
  dual_phase_then_ko    # Leagues Cup — dual tables + KO finish odds
  group_then_ko         # WC / continental majors — groups + KO finish odds
  knockout_finish       # domestic cups / Open Cup — finish odds only
```

Mapping:

| Competition | `standings_layout` (today) | Proposed `odds_model` |
|---|---|---|
| Club leagues | `single_table` / MLS / … | `league_table` |
| UCL / UEL / UECL | `league_phase` | `league_phase_then_ko` |
| Leagues Cup | `leagues_cup_dual` | `dual_phase_then_ko` |
| FA / League Cup / Copa / Pokal / Coupe / Coppa / Open Cup | `knockout_bracket` | `knockout_finish` |
| WC / Euros / … | `cup_groups` (when enabled) | `group_then_ko` |

---

## 4. Proposed API contract

### 4.1 Shared top-level (unchanged)

```json
{
  "ok": true,
  "competition": "England/FA Cup",
  "format": { "...": "..." },
  "predicted": { "...": "..." },
  "real": { "standings": null },
  "bracket": { "...": "..." },
  "fixtures": [ "..."]
}
```

### 4.2 `format` additions

```json
{
  "standings_layout": "knockout_bracket",
  "odds_model": "knockout_finish",
  "has_table": false,
  "extensions": {
    "knockout": true,
    "finish_stages": [
      "champion", "runner_up", "semi_final", "quarter_final",
      "round_of_16", "round_of_32", "earlier"
    ]
  }
}
```

For UEFA / Leagues Cup: `has_table: true` + same `finish_stages` for the KO half.

### 4.3 Knockout finish odds (new primary block)

Prefer nesting under `predicted` so league and cup stay parallel:

```json
"predicted": {
  "table": [],
  "groups": null,
  "position_odds": { "simple": {}, "detailed": [] },

  "winner": {
    "champion": "Arsenal",
    "probabilities": { "Arsenal": 18.5, "Man City": 14.2 },
    "simulations_run": 2500
  },

  "finish_odds": {
    "model": "knockout_finish",
    "simulations_run": 2500,
    "stages": ["champion", "runner_up", "semi_final", "quarter_final", "round_of_16"],
    "by_team": {
      "Arsenal": {
        "champion": 18.5,
        "runner_up": 12.0,
        "semi_final": 35.0,
        "quarter_final": 55.0,
        "round_of_16": 80.0,
        "most_likely_finish": "quarter_final",
        "most_likely_finish_pct": 28.0
      }
    },
    "by_stage": {
      "champion": [{"team": "Arsenal", "pct": 18.5}, ...],
      "runner_up": [...],
      "semi_final": [...]
    }
  },

  "winners_odds": [
    {
      "team": "Arsenal",
      "win_cup_pct": 18.5,
      "runner_up_pct": 12.0,
      "semi_final_pct": 35.0,
      "quarter_final_pct": 55.0,
      "most_likely_finish": "quarter_final",
      "most_likely_finish_pct": 28.0
    }
  ]
}
```

**Semantics (important):**

- `champion` — P(win the tournament)
- `runner_up` — P(lose the final) *(not “reach final”)*
- `semi_final` / `quarter_final` / `round_of_16` — **P(reach that round)** (inclusive of going further), matching sportsbook “to reach SF” style  
  *Alternative (document as a decision): P(eliminated in that round). Prefer “reach” for UI clarity; keep elimination map as a secondary field.*

Secondary (keep from today’s sim):

```json
"elimination_round_odds": { "Arsenal": { "Quarter-finals": 0.28, "Champion": 0.185 } },
"round_reach_probabilities": { "Semifinals": { "Arsenal": 0.35 } }
```

### 4.4 What knockout mode must **not** do

- Do not fill `predicted.table` with roster zeros or fake W/D/L
- Do not fill league-style `position_odds` (1st…20th)
- Do not set `real.standings` groups for pure knockouts
- `winners_odds[].win_league_pct` should be aliased → `win_cup_pct` (keep old key temporarily)

### 4.5 Hybrid cups (UEFA / Leagues Cup)

Keep **both**:

1. Phase table + table `position_odds` / qualification odds (top 8, playoff band, top 4 per dual table)
2. Knockout `finish_odds` once the KO tree is projectable from the table

```json
"predicted": {
  "table": [...],
  "groups": [...],
  "position_odds": {...},
  "finish_odds": { "model": "league_phase_then_ko", ... }
}
```

---

## 5. Pipeline / simulation work

### 5.1 Current state

`Track_Cup_Results._simulate_cup_tournament` already returns:

- `winner_probabilities` (champion)
- `elimination_round_probabilities`
- `round_reach_probabilities`

Gaps:

1. **No explicit runner-up** probability
2. Domestic `rounds` are often “Upcoming Cup Fixtures” / “Recent Cup Results” — names **not** in `CUP_KNOCKOUT_FEEDS` → sim returns empty
3. Large early-round fields (FA Cup 64+) are not a fixed bracket until drawn
4. League-data maps champion into `winners_odds` with league field names

### 5.2 Proposed simulation outputs

Extend sim result:

```python
{
  "champion": "...",
  "runner_up_probabilities": {...},   # NEW
  "winner_probabilities": {...},      # champion (existing)
  "round_reach_probabilities": {...}, # existing, normalize round keys
  "elimination_round_probabilities": {...},
  "finish_odds": { ... },             # NEW normalized block written into bracket JSON
  "simulations_run": 2500,
}
```

Normalize round keys to a stable enum:

`champion | runner_up | final | semi_final | quarter_final | round_of_16 | round_of_32 | round_of_64 | playoff | earlier`

### 5.3 How to build a simulatable tree per cup type

| Phase of season | Strategy |
|---|---|
| **Known bracket** (QF+ drawn) | Simulate from known matches + pred probs |
| **Partial bracket** (some rounds known) | Lock completed results; sim remaining + TBD slots |
| **Pre-draw / large field** | Remaining-team pool + random draw each sim (or strength-based pairing); report higher uncertainty |
| **UEFA league phase** | Existing table → seed KO tree → sim (already sketched) |
| **Leagues Cup Phase One** | Dual tables → top 4 × 2 → QF tree → sim |

Domestic cups need a dedicated path beyond “list upcoming fixtures as rounds”:

1. Infer current round size from remaining teams / upcoming count  
2. Build a **synthetic bracket template** for remaining rounds  
3. Feed that into `_simulate_cup_tournament`  
4. Store both `rounds` (display) and `sim_rounds` (simulation topology) if they differ

### 5.4 Artifact files

Keep writing:

- `projected_cup_brackets.json` — per competition: `rounds`, `finish_odds`, champion, sims  
- `projected_cup_tables.csv` — **only** UEFA + Leagues Cup (already mostly true)

Optional new: `projected_cup_finish_odds.json` if CSV consumers need a flat dump (mobile). Prefer nesting in bracket JSON first to avoid a third source of truth.

---

## 6. Website / mobile UI

### 6.1 Website cups page

For `hasTable: false`:

- Default view: **Finish odds** (new) + **Bracket**
- Hide Table tab (or replace Table tab with “Odds”)
- Finish odds table columns: Team | Champion | Runner-up | SF | QF | R16 | Most likely exit

For `hasTable: true` (UEFA / Leagues Cup):

- Keep Table + Bracket  
- Add Odds subview or a panel under the table for KO finish odds once available

### 6.2 League tiles / league-data consumers

- Cup tiles should call the same `/api/league-data` and branch on `format.odds_model` / `has_table`
- Leaders API (`/api/league-leaders` cups section) should use `finish_odds.by_stage.champion` (already close via winner probs)

### 6.3 Copy / naming

Avoid “win league” in cup UI. Prefer “Win cup” / “Lift the trophy” / “Reach SF”.

---

## 7. Compatibility plan

| Legacy field | Transition |
|---|---|
| `winners_odds[].win_league_pct` | Keep = champion pct; add `win_cup_pct` |
| `predicted.position_odds` | Empty object for knockout_finish |
| `predicted.table` | `[]` for knockout_finish |
| `elimination_round_odds` | Keep; derive UI “most likely exit” |
| `winner_probabilities` | Keep; also mirror into `finish_odds.by_stage.champion` |

Deprecate after one client release cycle: relying on table/position_odds for cups.

---

## 8. Work breakdown (suggested phases)

### Phase A — Contract + wiring (small, safe)

1. Add `odds_model` + `has_table` + `finish_stages` to `competition_format_spec`
2. Define `finish_odds` schema helper; populate from existing elim/reach/champion when present
3. League-data: for `knockout_bracket`, attach `predicted.finish_odds` + rename/alias winners_odds fields
4. Docs + golden JSON fixtures for FA Cup / UCL

### Phase B — Simulation completeness

1. Runner-up tracking in `_simulate_cup_tournament`
2. Stable stage key normalization
3. Domestic **sim_rounds** builder (remaining field → synthetic KO tree)
4. Ensure Open Cup / Copa / etc. produce non-empty `finish_odds` when upcoming preds exist

### Phase C — Hybrid cups

1. UEFA: finish_odds from league-phase-seeded bracket (in addition to table)
2. Leagues Cup: finish_odds from dual-table QF seeding
3. Qualification odds stay on the table side (`top4_pct` etc.)

### Phase D — Frontend

1. Cups page Odds view for knockout comps
2. Bracket view consumes same payload
3. Hide empty table tab for `has_table: false`

### Phase E — Cleanup

1. Remove dead “fake table” fallbacks for cups
2. Trim legacy field aliases once clients updated

---

## 9. Accuracy / product decisions (need your call)

1. **Reach vs eliminated-in-round** for SF/QF columns?  
   - Recommendation: **reach** for main UI; keep elimination map secondary.
2. **Runner-up** = lost final only (yes/no)?  
   - Recommendation: **yes**.
3. Early FA Cup (100+ clubs): sim all entrants or only “known remaining + favorited” set?  
   - Recommendation: all remaining with strength priors; cap list in UI to top N by champion odds.
4. Replays / two-legged ties: treat as single KO edge with aggregated probs (current approach) or explicit legs?  
   - Recommendation: keep single-edge for v1; two-leg expansion already exists for display.
5. Should national-team majors (WC) use the same `finish_odds` block?  
   - Recommendation: **yes** (`group_then_ko`), after club cups.

---

## 10. Out of scope (this redesign)

- Changing league (PL etc.) position-odds math
- Live in-play cup odds
- Betting exchange / external odds ingestion
- Full historical cup reconstruction beyond current season artifacts

---

## 11. Success criteria

- FA Cup `/api/league-data/England/FA Cup` returns `format.has_table: false`, empty table, non-empty `finish_odds` when predictions exist
- UCL still returns league-phase table **and** KO finish odds when projectable
- Website knockout cups show an Odds view (not a dead table message)
- No cup writes rows into `projected_cup_tables.csv` except UEFA + Leagues Cup
- Pipeline cup failure still fail-fast (already fixed in #15)

---

## 12. Next step

Review this draft (especially §9 decisions). After approval, implement **Phase A** on a new branch (`cursor/cup-finish-odds-design-d2e6` or follow-on), then B→D.
