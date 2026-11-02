# Beyond the Stats — AI Coding Agents Guide & System Specification

> **MANDATORY FOR ALL CODING AGENTS**:
> Read this document completely before starting any task on this repository.
> This repository contains an integrated ecosystem of machine learning pipelines, a persistent FastAPI backend, real-time live match polling, APNs push notification services for iOS, and SQLite persistence.
>
> **LIVING DOCUMENT REQUIREMENT**:
> Whenever you modify architectural components, add API endpoints, alter database tables/indexes, change prediction schemas, or add new test suites, you **MUST** update this file as part of your task's Definition of Done. Record your changes in the [Agent Activity & System Evolution Log](#agent-activity--system-evolution-log).

---

## Table of Contents
1. [Core Working Principles ("Take Time on Work")](#1-core-working-principles-take-time-on-work)
2. [Project Architecture & Directory Layout](#2-project-architecture--directory-layout)
3. [Data Flow & Subsystems](#3-data-flow--subsystems)
4. [Database Architecture (SQLite Store)](#4-database-architecture-sqlite-store)
5. [Data Schemas & API Contracts](#5-data-schemas--api-contracts)
6. [Testing & Quality Assurance Protocol](#6-testing--quality-assurance-protocol)
7. [Operations & Process Management (`startbts`)](#7-operations--process-management-startbts)
8. [Agent Maintenance Checklist](#8-agent-maintenance-checklist)
9. [Agent Activity & System Evolution Log](#9-agent-activity--system-evolution-log)

---

## 1. Core Working Principles ("Take Time on Work")

Beyond the Stats is a sophisticated data science and real-time backend platform. Quick, unverified patches or hurried band-aids often introduce subtle edge-case failures, data leakage, broken mobile clients, or live polling crashes. All coding agents must adhere to the following principles:

### A. Deliberate Investigation Before Code Modification
- **Trace the full execution path**: Never modify a function without knowing its callers and callees across both the `pipelines/` and `Website/` layers.
- **Inspect upstream data inputs and downstream consumers**: E.g., changing a dictionary key in `predictions.py` impacts `/api/upcoming`, `/api/match/<id>`, SQLite storage, and the iOS frontend parser.
- **Find the true root cause**: Do not mask a data formatting error with an empty catch block or fallback that hides data loss.

### B. Respect Dual-Directory Layout
- The repository has a root workspace directory (`BeyondTheStats-Soccer-Predictor/`) and a primary application root (`Beyond-the-Stats/`).
- **Canonical Paths**: Never hardcode relative directory traversal strings like `../../Output`. Always import and use canonical paths from `shared.paths` (or `Website.config`).
- **Python Environment**: Always run commands using the project virtualenv at `Beyond-the-Stats/.venv2/bin/python`.

### C. Handle Edge Cases Methodically
- **Identifiers**: Match fixtures do **not** have numeric ESPN `match_id` values until they appear on ESPN scoreboards. Upstream model fixtures are identified by their canonical `sync_key` (e.g. `2026-10-24|rosariocentral|huracan` or `2026-10-24|argentina/primera division|rosario central|huracan`). Any endpoint or lookup querying matches must support **both** `match_id` and `sync_key` (including URL-encoded variants).
- **Timezones**: UTC vs America/New_York (Eastern Time). Model schedules and match display labels often use ET; API timestamps and raw kickoff data use ISO 8601 UTC. Avoid converting midnight UTC dates into ET without verifying whether the string represents a pure calendar date (`YYYY-MM-DD`) or a timestamp with hours/minutes.
- **Data Types**: Probability fields may exist as decimals (0.0 to 1.0) or percentages (0.0 to 100.0). Always check scale before formatting to avoid multiplying already scaled numbers (e.g., `13.5` becoming `1350%`).
- **Division by Zero & Null Safety**: In Poisson distributions, goal likelihoods, and team ratings, always guard against zero totals, empty team histories, or unmapped clubs.

### D. Preserve Codebase & Documentation Integrity
- Preserve existing docstrings, type annotations, and comments.
- Follow existing patterns in the codebase (e.g., Starlette compatibility shims, atomic cache writing, thread-safe locks).

---

## 2. Project Architecture & Directory Layout

```
BeyondTheStats-Soccer-Predictor/        # Workspace root
├── AGENTS.md                          # This file (Living Agent Guide)
├── README.md                          # Public overview
├── startbts, startbts.py              # Central backend management script
├── Data/                              # Shared datasets & master team mapping
│   ├── redeem_codes.json              # In-app redemption codes
│   └── team_name_mapping_master_new.json # Master name normalization seed
└── Beyond-the-Stats/                  # Core application package
    ├── AGENTS.md                      # Synchronized copy / symlink
    ├── startbts, startbts.py          # Application-level management script
    ├── .venv2/                        # Primary Python 3.12 virtual environment
    ├── main/                          # Top-level entry points & backend daemon
    │   ├── Run_All_Pipeline.py        # Complete pipeline runner
    │   ├── Daily_Pipeline.py          # Daily scheduled refresh
    │   ├── run_backend.py             # Persistent server runner
    │   └── Backend/                   # Server process, scheduler, memory monitor
    ├── pipelines/                     # Feature engineering & ML models
    │   ├── europe/files/              # European leagues, UEFA cups, domestic cups, friendlies
    │   ├── mls/files/ + Data/         # MLS & Liga MX models and data
    │   └── extra/files/ + Data/       # Additional global leagues
    ├── shared/                        # Shared cross-cutting modules
    │   ├── paths.py                   # Canonical filesystem paths
    │   ├── sqlite_store.py            # SQLite database interface & migrations
    │   ├── prediction_engine.py       # Core prediction algorithms & Poisson math
    │   ├── calendar.py                # Season dates & matchweek tracking
    │   └── cache.py                   # Atomic file & disk caching utilities
    ├── Website/                       # FastAPI web application & background workers
    │   ├── app.py                     # Main FastAPI application instance & lifespan
    │   ├── config.py                  # Web application configuration
    │   ├── live_poller.py             # Real-time ESPN live score polling & momentum
    │   ├── notifications.py           # APNs worker, Live Activities, push dispatch
    │   ├── standings.py               # Standings calculation & points deductions
    │   ├── math_utils.py              # Poisson distributions & secondary markets
    │   ├── competition_rules.py       # Canonical competition & team mappings
    │   └── routes/                    # Modular API route blueprints
    │       ├── live_scores.py         # Match details, live scores, SSE stream
    │       ├── predictions.py         # Upcoming global, team next-game, past-games
    │       ├── admin.py               # Points adjustments, recalculation, legal
    │       ├── leagues.py             # League leaders, standings, league info
    │       ├── helpers.py             # Auth check, logging, response sanitization
    │       └── compat.py              # Flask-like Blueprint & RequestContext shims
    ├── Output/                        # Generated runtime artifacts (gitignored)
    │   ├── Predictions/               # CSV outputs by region (europe, mls, cups, etc.)
    │   ├── Data/bts_storage.db        # Primary SQLite operational database
    │   ├── Status/                    # Runtime status JSONs & poll caches
    │   └── Logs/                      # Service stdout/stderr logs
    ├── Data/                          # Inputs, seeds, and training historical data
    │   ├── Seeds/                     # Tracked rosters, ESPN names seen
    │   └── National_Team_Data/        # International squads & rankings
    └── tests/                         # Automated unit & integration tests
```

---

## 3. Data Flow & Subsystems

```
1. Scraping & Inputs (ESPN API, football-data.org, Transfermarkt seeds)
               │
               ▼
2. Pipelines (pipelines/{europe,mls,extra}/files/)
   • Feature Engineering (Form, Rolling Stats, Relative Strength, Head-to-Head)
   • ML Predictions (Random Forest, Logistic Regression, Poisson Expected Goals)
   • CSV / JSON Generation (Output/Predictions/)
               │
               ▼
3. SQLite Persistence (shared/sqlite_store.py)
   • upsert upcoming_games, past_games, live_score_history, squad_values
               │
               ▼
4. Web Serving Layer (Website/app.py -> routes/)
   • Modular routers: predictions, live_scores, admin, leagues
   • Background Workers:
       - live_poller.py: In-game scores, clock, events, momentum
       - notifications.py: Apple Push Notifications (APNs) & iOS Live Activities
               │
               ▼
5. Consumer Clients (iOS Mobile App, Web Frontend, Admin Dashboard)
```

---

## 4. Database Architecture (SQLite Store)

The primary database is stored at `Beyond-the-Stats/Output/Data/bts_storage.db` and managed via [`shared/sqlite_store.py`](file:///Users/yitao/BtS/BeyondTheStats-Soccer-Predictor/Beyond-the-Stats/shared/sqlite_store.py).

### Core Tables & Schemas
1. **`upcoming_games`**:
   - Stores scheduled fixtures and model predictions.
   - Primary Key: `storage_key` (`fixture:<date>|<comp>|<home>|<away>` or `prediction:<key>`).
   - Indexed fields: `match_date_iso`, `competition`, `status`, `home_team`, `away_team`, and `json_extract(payload_json, '$.sync_key')`.
2. **`past_games`**:
   - Stores completed fixtures with actual scores (`actual_home_goals`, `actual_away_goals`, `actual_result`, `is_correct`).
   - Used for historical model accuracy tracking and team recent form computation (`load_team_recent_matches`).
3. **`live_score_history`**:
   - Stores live or recently finished ESPN matches with active clocks, goalscorers, cards, and box scores.
   - Indexed on `match_id`, `storage_key`, `game_date`, and `json_extract(payload_json, '$.sync_key')`.
4. **`points_deductions`**:
   - Stores official league point deductions, baseline starting points, and manual point adjustments.
   - Indexed on `competition`, `team_name`, `season`.
5. **`notification_subscriptions`**:
   - Stores iOS APNs device tokens, Live Activity push tokens, match subscriptions, and team subscriptions.

### Fast Match Lookup Contract (`get_game_by_match_id`)
When searching for a match by any ID or identifier string `mid`:
- Check for direct match on numeric `match_id`.
- Check `storage_key` variations (`id:<mid>`, `prediction:<mid>`, `fixture:<mid>`).
- Check `json_extract(payload_json, '$.sync_key')` for exact match with URL-decoded key.
- Check decomposed pipe-delimited pattern (`YYYY-MM-DD|home|away` or `YYYY-MM-DD|comp|home|away`).

---

## 5. Data Schemas & API Contracts

### A. Comprehensive Match Prediction Schema (79+ Fields)
Every single match payload returned by `/api/upcoming/global`, `/api/match/<match_id>`, and `/api/team/<team>/next-game` must guarantee full statistical parity:

| Category | Key Names | Description |
|---|---|---|
| **Probabilities & Goals** | `prob_home`, `prob_draw`, `prob_away`<br>`prob_home_text`, `prob_draw_text`, `prob_away_text`<br>`pred_home_goals`, `pred_away_goals` | Win/Draw/Loss probabilities (floats & formatted strings) and expected goals. |
| **Poisson Distributions** | `correct_score_dist`<br>`asian_handicap`<br>`total_goals_dist`<br>`first_to_score`<br>`clean_sheet`<br>`double_chance` | Detailed market distributions computed via Poisson modeling in [`math_utils.py`](file:///Users/yitao/BtS/BeyondTheStats-Soccer-Predictor/Beyond-the-Stats/Website/math_utils.py). |
| **Expected Points (xPTS)** | `xpts_home`, `xpts_away`, `expected_points` | Expected points calculated from 3-point win / 1-point draw system. |
| **HT vs FT & Half Markets** | `pred_home_goals_ht`, `pred_away_goals_ht`<br>`prob_home_ht`, `prob_draw_ht`, `prob_away_ht`<br>`ht_correct_score_dist`<br>`htft_prediction`, `predicted_htft`<br>`prob_highest_scoring_half` | Half-time expected goals, 1X2 HT market, HT scoreline distribution, joint 9-way HT/FT probabilities, and highest scoring half breakdown. |
| **Combo Markets** | `prob_home_win_and_over_2_5`, `prob_away_win_and_over_2_5`<br>`prob_home_win_clean_sheet`, `prob_away_win_clean_sheet`<br>`prob_btts_and_over_2_5` | High-value combination outcomes derived from joint probability models. |
| **Predicted Match Stats** | `pred_home_shots`, `pred_away_shots`<br>`pred_home_sot`, `pred_away_sot`<br>`pred_home_corners`, `pred_away_corners`<br>`pred_home_possession`, `pred_away_possession`<br>`pred_home_fouls`, `pred_away_fouls`<br>`pred_home_cards`, `pred_away_cards` | Model-estimated in-game statistics (shots, shots on target, corners, possession %, fouls, yellow/red cards). |
| **Cross-Competition Form** | `last_5_home`, `last_5_away`<br>`last_5_home_at_home`, `last_5_away_at_away` | Chronological completed match history spanning domestic league, domestic cup, and continental tournaments (overall and home/away splits). |
| **Head-to-Head (H2H)** | `head_to_head`<br>`h2h_summary` (`home_wins`, `away_wins`, `draws`, `avg_total_goals`, `btts_rate`, `matches_played`) | Recent direct competitive encounters between the clubs (excluding friendlies) with aggregated metrics. |
| **Momentum & Streaks** | `home_momentum`, `away_momentum`<br>(`unbeaten_streak`, `win_streak`, `scoring_streak`, `clean_sheet_streak`, `form_guide`, `points_last_5`, `goal_diff_last_5`, `clean_sheets_last_5`, `shot_conversion_rate`) | Real-time form metrics, streak trackers, and efficiency measures. |
| **Goal Likelihoods** | `prob_both_score`, `prob_over_1_5`, `prob_over_2_5`, `prob_over_3_5`<br>`prob_home_goals_0/1plus/2plus`<br>`prob_away_goals_0/1plus/2plus` | Probability of secondary goal markets. |
| **Ratings & Form** | `home_attack_rating`, `home_defence_rating`<br>`away_attack_rating`, `away_defence_rating` | Relative team strength metrics. |
| **Prediction Meta** | `predicted_result`, `winner_label`, `prediction_quality`<br>`has_prediction`, `reasoning`, `odds_home`, `odds_draw`, `odds_away` | Pick labels ("Pred: Arsenal", "Schedule only"), confidence/quality, reasoning text, and betting odds. |
| **Team Identification** | `home_team`, `away_team`, `canonical_home_team`, `canonical_away_team`<br>`display_home_team`, `display_away_team`, `home_team_logo`, `away_team_logo` | Normalized and display team names with logos. |

### B. Dynamic Fallback Math
If a match is loaded from an external live feed or an un-enriched source without precomputed distributions, [`_enrich_with_upcoming_prediction`](file:///Users/yitao/BtS/BeyondTheStats-Soccer-Predictor/Beyond-the-Stats/Website/routes/live_scores.py#L180) in `Website/routes/live_scores.py` dynamically invokes `math_utils.py` functions:
- `_compute_correct_score_dist(hg, ag)`
- `_compute_double_chance(ph, pd, pa)`
- `_compute_asian_handicap(hg, ag, ph, pd, pa)`
- `_compute_total_goals_dist(hg, ag)`
- `_compute_first_to_score(hg, ag)`
- `_compute_clean_sheet(hg, ag)`
- `_compute_goal_probabilities(hg, ag)`

### C. Security & Admin Mutations
- All endpoints that mutate database state or trigger heavy recalculations (`/api/admin/...`, `/api/refresh`) require authorization.
- Pass token via header: `X-Admin-Token: <MUTATION_API_TOKEN>` or `Authorization: Bearer <MUTATION_API_TOKEN>`.

### D. Team Canonicalization & Catalog Deduplication Contract
1. **Sibling-League Fallback by Country**:
   - `canonical_team_name(name, competition)` resolves aliases using country prefixes (e.g. `England/FA Cup` and `England/League Cup` fall back to `England/Premier League` and `England/Championship`).
   - Strictly isolated by country: No global cross-country fallback to prevent ambiguous collisions (e.g. MLS `Inter Miami` vs Italian `Inter`).
2. **App Teams Catalog (`/api/teams/catalog`)**:
   - Built by `build_app_teams_catalog_payload`.
   - Teams must be deduplicated by resolved `team_id` (via `resolve_team_info`).
   - If multiple entries share a `team_id`, prefer target canonical names from `team_name_mapping_master.json` and merge all competitions.
3. **SQLite Sanitization**:
   - `cleanup_canonical_team_names` in `shared/sqlite_store.py` ensures `upcoming_games`, `past_games`, and `live_score_history` have normalized `home_team`, `away_team`, `sync_key`, and deduplicated storage keys.

---

## 6. Testing & Quality Assurance Protocol

### A. Virtual Environment
Always execute commands using the project virtualenv:
```bash
# From workspace root:
./Beyond-the-Stats/.venv2/bin/python ...

# Or from Beyond-the-Stats directory:
./.venv2/bin/python ...
```

### B. Test Execution
```bash
# 1. Run full test suite:
cd Beyond-the-Stats
./.venv2/bin/python -m unittest discover -s tests -p "test_*.py"

# 2. Run targeted test file:
cd Beyond-the-Stats
./.venv2/bin/python -m unittest tests/test_match_detail_prediction_stats.py
./.venv2/bin/python -m unittest tests/test_caching_and_stream.py
./.venv2/bin/python -m unittest tests/test_team_next_game.py
./.venv2/bin/python -m unittest tests/test_sync_matching.py
./.venv2/bin/python -m unittest tests/test_admin_points_adjustments.py
./.venv2/bin/python -m unittest tests/test_sqlite_end_to_end.py
```

### C. Critical Rule: Date-Independent Tests
When writing tests that mock fixtures or test upcoming date windows:
- **NEVER** hardcode static calendar dates (e.g., `"2026-10-25"`), because fixture filters drop dates in the past relative to wall-clock time (`today_iso`).
- **ALWAYS** use dynamic relative dates:
  ```python
  from datetime import datetime, timedelta, timezone

  today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
  future_iso = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
  past_iso = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
  ```

### D. Regression Testing Requirement
Before concluding any task or committing changes:
1. Identify all components and endpoints touched.
2. Run their corresponding automated test suites.
3. Write a new regression test reproducing any bug fixed or asserting parity for any new feature.
4. Verify that existing tests continue to pass with zero regressions.

---

## 7. Operations & Process Management (`startbts`)

The project includes a unified CLI script [`startbts.py`](file:///Users/yitao/BtS/BeyondTheStats-Soccer-Predictor/startbts.py) available at both the root and `Beyond-the-Stats/`:

| Command | Action |
|---|---|
| `python startbts` | Reloads / restarts the backend service (via macOS launchd `com.beyondthestats.backend`) and checks health on port 8000. |
| `python startbts status` | Displays launchd service state, API health check (`/api/health`), and recent log activity. |
| `python startbts logs` | Tails live service logs from `Output/Logs/launchd_stderr.log`. |
| `python startbts refresh` | Triggers upcoming predictions refresh via `/api/refresh` or direct script execution. |
| `python startbts stop` | Gracefully stops the background service. |
| `python startbts --foreground` | Runs backend in the foreground for live debugging and console inspection. |

---

## 8. Agent Maintenance Checklist

Before concluding your task, complete this checklist:
- [ ] Root cause identified and resolved without quick-fix shortcuts.
- [ ] Code modifications adhere to type hints, existing patterns, and docstring integrity.
- [ ] Dual-lookup support verified for both ESPN `match_id` and canonical `sync_key`.
- [ ] Full statistical parity verified against the 79+ key prediction schema.
- [ ] Regression tests run and verified with `Beyond-the-Stats/.venv2/bin/python`.
- [ ] New automated unit/regression tests added in `Beyond-the-Stats/tests/`.
- [ ] Backend status checked via `python startbts status` or `curl http://127.0.0.1:8000/api/health`.
- [ ] **Updated this file (`AGENTS.md`)**: Added relevant architectural details, updated schemas, and appended a log entry in [Section 9](#9-agent-activity--system-evolution-log).

---

## 9. Agent Activity & System Evolution Log

| Date | Agent / Task | Changes Made | Verification & Tests |
|---|---|---|---|
| **2026-10-24** | Admin Points Adjustments API | Created endpoints for points deductions, baseline points, preview, recalculate in `Website/routes/admin.py` with `MUTATION_API_TOKEN` auth. | Added `tests/test_admin_points_adjustments.py`. |
| **2026-10-25** | Single Match Prediction Stats Parity | Enriched `GET /api/match/<match_id>` to support `sync_key` lookups, dynamic Poisson distribution fallbacks in `math_utils.py`, and 100% field parity with `/api/upcoming/global`. | Added `tests/test_match_detail_prediction_stats.py`. |
| **2026-11-01** | Date-Robustness & Agent Guide Creation | 1. Created comprehensive `AGENTS.md` living guide.<br>2. Fixed date drift in `tests/test_team_next_game.py` with relative future dates.<br>3. Fixed UTC date conversion shift in `live_poller.py` for pure calendar dates.<br>4. Fixed `clear_league_leaders_cache` in `Website/routes/leagues.py`. | `test_team_next_game.py` (8/8 PASSED), `test_sync_matching.py` (8/8 PASSED), `test_match_detail_prediction_stats.py` (5/5 PASSED). |
| **2026-11-01** | Sibling-League Country Fallback, Catalog Deduplication & SQLite Cleanup | 1. Implemented sibling-league fallback strictly by country in `Website/competition_rules.py::canonical_team_name` (no global bleed).<br>2. Deduplicated `/api/teams/catalog` by resolved `team_id`, merging competitions in `Website/team_mappings.py`.<br>3. Updated domestic cup mappings in `Data/team_name_mapping_master.json`.<br>4. Implemented `cleanup_canonical_team_names` in `shared/sqlite_store.py` and cleaned SQLite store (639 upcoming, 2 past, 3 live rows re-canonicalized). | Added `tests/test_team_catalog_and_sibling_fallback.py` (4/4 PASSED), full regression suites passed (34/34 total tests PASSED). |
| **2026-11-01** | Cross-Competition Form, xPTS, HT/FT & Extended Match Stats | 1. Created `Website/match_analytics.py` for cross-competition match aggregation (covering domestic leagues, cups, and continental tournaments for 729+ clubs).<br>2. Fixed empty form bug by integrating cup predictions, SQLite past games, and processed data.<br>3. Added Expected Points (xPTS), HT/FT 9-way joint distribution, highest scoring half, combos, and predicted in-game stats (shots, SOT, corners, possession %, fouls, cards) in `Website/math_utils.py`.<br>4. Added head-to-head (H2H) competitive match filtering (excluding friendlies) and team momentum streak tracker (`unbeaten_streak`, `shot_conversion_rate`, etc.).<br>5. Integrated into `/api/match/<id>` and `/api/team/<team>/next-game`. | Added `tests/test_match_analytics_and_stats.py` (6/6 PASSED). |
| **2026-11-01** | Team Overview API, Real Season Averages, Multi-Comp Lineups & Pre-caching | 1. Verified & strengthened 3-tier names (`home_team_full`, `home_team_short`, `home_team_abbr`, `canonical_home_team`) across `GET /api/match` and its `upcoming_prediction` & `prematch_prediction` snapshots.<br>2. Built comprehensive `Website/team_overview.py` module computing real season averages (shots, SOT, corners, possession %, fouls, passes, pass accuracy) from live match boxscores and current season datasets.<br>3. Extracted latest match lineups (`formation`, `startXI`, `substitutes`) and active multi-competition projected season finishes with position odds.<br>4. Integrated relative ratings (`attack_rating`, `defence_rating`, `overall_rating`) and form streaks across all competitions.<br>5. Added `/api/team/<team>/overview`, `/api/team?team=...&overview=true`, and `/api/team-overview` endpoints with sub-millisecond cached lookups.<br>6. Implemented automatic background pre-caching (`precache_popular_teams` / `TeamOverviewPrecacheWorker`) pre-caching 126+ popular teams across top 5 European leagues & MLS at startup and daily. | Added `tests/test_team_overview_and_match_names.py` (4/4 PASSED), all regression suites passed (39/39 total tests PASSED). |

