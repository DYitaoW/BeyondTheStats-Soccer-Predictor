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
    │   ├── Status/bts_store.db        # Primary SQLite operational database (override: BTS_SQLITE_STORE_PATH)
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

The primary database is stored at `Beyond-the-Stats/Output/Status/bts_store.db` (`shared.paths.SQLITE_STORE_FILE`, overridable with `BTS_SQLITE_STORE_PATH`) and managed via [`shared/sqlite_store.py`](file:///Users/yitao/BtS/BeyondTheStats-Soccer-Predictor/Beyond-the-Stats/shared/sqlite_store.py).

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
   - Primary Key: `(token, sub_type, sub_target)`. `sub_type` ∈ `device`, `match`, `team`, `live_activity`, `la_push_to_start` (target = owning device token), `la_started` (target = match_id; push-to-start dedupe marker), `la_channel` (token = device, target = match_id; device runs a channel-based iOS 18 activity).
   - Metadata columns (auto-migrated): `device_token` (owner of a Live Activity / push-to-start token), `sync_key`, `team_id` (resolved team identity for `team` rows), `environment` (`sandbox`/`production`, also written when APNs auto-detects the environment).
   - Match-end cleanup (`remove_match_notification_subs`) deletes `match`, `live_activity`, `la_started`, and `la_channel` rows by `match_id` **and** `sync_key`.
6. **`notification_history`**:
   - Stores chronological audit and live feed history for in-app notifications (`GET /api/notifications`).
   - Indexed on `created_at DESC`, `match_id`, and `sync_key`.
7. **`live_activity_channels`**:
   - iOS 18 ActivityKit broadcast channels. Primary Key: `(match_key, environment)` (`match_key` = sync_key or match_id; `environment` = `sandbox`/`production` — channels never cross environments).
   - Columns: `channel_id` (base64, from APNs `apns-channel-id`), `match_id`, `sync_key`, `competition`, `created_at`, `ended_at` (set by the end push; channel deleted `APNS_BROADCAST_DELETE_AFTER_END_SECONDS` later).
8. **`live_activity_device_settings`**:
   - Per-device ActivityKit capabilities: `frequent_updates`, `activities_enabled`, `supports_broadcast` (iOS 18+), `ios_version`. Primary Key: `device_token`.

### Result Finalization Contract (final whistle → tables, past_games, team form)
When the live poller sees a match go `post` (from any path: poll cycle, on-demand `refresh_live_scores_now`, overnight rollover), `live_poller._merge_completed_to_history`:
1. Upserts the game into `live_score_history` (unchanged behaviour).
2. **Immediately writes it to `past_games`** via `_sync_finished_games_to_past_games` (reuses `predictions._live_game_to_past_row` + `_merge_prediction_onto_past_row`: ET match date, canonical names, sync key, pre-match prediction). Previously finals only reached `past_games` in the nightly `archive_todays_games_to_past_games_file`; upserts are idempotent with it. Synthetic games (`test-*`, `sim-*`, `Test/*`) are skipped. A post-FT score change (VAR/admin correction) rewrites the row; unchanged finals are not reprocessed (`_past_synced_scores` memo). Startup ESPN backfill also syncs missed finals.
3. Queues the competition and both teams in `_pending_result_comps` / `_pending_result_teams`.

`_drain_finalized_results` (poller cycle; background thread after on-demand refresh; concurrent calls coalesce) then runs `standings.refresh_real_standings_after_live_final` per competition and `_invalidate_result_dependent_caches`: `match_analytics.invalidate_match_history_cache`, `predictions.clear_team_form_caches`, team next-game index, league leaders, `team_overview.refresh_team_overviews_after_results` (drops alias-matched overviews, rebuilds the global index in the background, re-warms), and `cache._cache_clear_endpoints(_RESULT_DEPENDENT_ENDPOINTS)` (league/cup tables, stats, team, past-games, h2h, upcoming, home, widget responses).

**Team form readers** (`sqlite_store.load_team_recent_matches`, `match_analytics.get_team_comprehensive_form` / `get_head_to_head`, `predictions._build_last5_form_index`) match teams by resolved team id (`sqlite_store._team_identity_id`) as well as raw/canonical names — canonical names are competition-dependent ("Manchester United" → "Man United" only for the Premier League). Duplicate rows of one fixture collapse by `match_id` / canonical `date|home|away` (or one match per team per date). Rows dated after today (`_latest_completed_date_iso`) are ignored. The upcoming-feed last-5 overlays settled `past_games` rows (last 120 days) on top of the Processed_Data CSVs.

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

### E. Cup Competition Safeguards & Upcoming Deduplication Contract
1. **Club Identity Qualifiers ("City", "United", "Rovers", "Town", "Athletic")**:
   - Generic acronyms (`fc`, `cf`, `afc`, `sc`, `club`) can be stripped during normalization, but club qualifiers must **never** be stripped indiscriminately. Stripping qualifiers collides distinct clubs (e.g. Oxford City vs Oxford United, Bristol City vs Bristol Rovers, Man City vs Man United).
   - In `fuzzy_resolve_team_name`, qualifier isolation ensures that clubs with competing qualifiers never match. Substring matches across bare cities (e.g. "Oxford" matching "Oxford City") are prohibited when a specific qualifier is present.
2. **Cup Safeguards (Non-League & Lower-Division Clubs)**:
   - Domestic cup ties (`FA Cup`, `Copa del Rey`, etc.) frequently feature unmapped or lower-division clubs not in top-flight datasets.
   - Unmapped cup teams must **never** be fuzzy-hijacked by senior league clubs. If unmapped, cup fixtures remain `schedule_only = "1"`, `prediction_quality = "no_prediction"`, `winner_label = "Schedule only"`.
   - Domestic cup mapping updates must never leak or propagate lower-tier unmapped clubs into senior league mappings.
3. **Deterministic Match Merging & ESPN `match_id` Propagation**:
   - `/api/upcoming/global` preloads live score history into O(1) in-memory indices and attaches ESPN `match_id` to upcoming rows whenever available.
   - `_annotate_upcoming_rows_with_live` must **never** wipe `row["match_id"]` when a match is scheduled or in pre-game state.
   - `_deduplicate_and_merge_upcoming_rows` reconciles duplicates across CSV predictions and live/schedule feeds by matching on `match_id`, `sync_key`, or canonical fixture tuple `(date_iso, base_comp, c_home, c_away)`.
   - When merging duplicate records, the richest prediction distributions, expected stats, and probabilities are preserved alongside numeric ESPN `match_id` and live game state.

### F. APNs Push & Live Activity (ActivityKit) Contract
0a. **Targeting — followers only**: a game's alerts, Live Activity starts and updates reach ONLY devices that follow that match (ESPN `match_id` or `sync_key`, via `for_match_tokens`) or one of its two teams (`for_team_tokens`), deduplicated in `for_game_or_team_subscribers`. Registering a device follows nothing.
   - **Teams are matched by resolved team id** (`notifications.resolve_team_id` → `standardized_teams.resolve_team_info`, the same id as `/api/teams/catalog`; clients may send `team_id` directly). Registry key `id:<team_id>`; unmapped names fall back to their exact-name slug. **No fuzzy/prefix name matching** (it leaked Inter ↔ Inter Miami, Real Sociedad → Real Sociedad B, Newcastle → Newcastle Jets).
   - **Matches**: every registry key for the identifier is checked (all competition spellings), never only when exact keys miss. Client identifiers go through `normalize_match_ident` (URL-decode; sync keys lowercased). Subscribing with both `match_id` and `sync_key` follows both. Match follows are cleared at full time; team follows persist.
   - **Mass sends are explicit**: admin `POST /api/notifications` requires a target (`match_id`/`sync_key`/`team(s)`) or `"audience": "all"`; `/api/test/notifications/push` requires `device_token` or `"audience": "all"`.
0. **Notifications are all-or-nothing**: `normalize_events` always returns every event type (`goal`, `start`, `ht`, `second_half`, `extra_time`, `penalties`, `red_card`, `end`); per-event `events` preferences from clients are accepted but ignored. Opting out = unsubscribe / unregister.
1. **`aps.event` is only ever `start`, `update`, or `end`** (Apple rejects anything else). In-match labels (`kickoff`, `halftime`, `goal`, `own goal`, `penalty`, `red card`, `second half`, `extra time`, `penalties`, `end of regular time`, `score`, `fulltime`, `prematch`, `live`) travel in `content-state.event`. `send_live_activity_update(..., event=<label>)` maps the label automatically; `notifications.build_live_activity_aps` is the single payload builder (also used by `/api/test/live-activities/push`).
2. **Lifecycle**:
   - `start`: push-to-start (iOS 17.2+), sent by `live_poller._live_activity_start_loop` (20 s tick) `APNS_LA_START_LEAD_SECONDS` (300) before scheduled kickoff, with up to `APNS_LA_START_GRACE_SECONDS` (900) late grace, to devices that registered a push-to-start token **and** follow the match or either team. Payload adds `attributes-type` (`APNS_LA_ATTRIBUTES_TYPE`, default `MatchActivityAttributes`), `attributes`, `alert`, `input-push-token: 1` (iOS 18+). Deduped per (device, match) in memory + SQLite `la_started`; skipped if the device already has an activity for the match.
   - `update`: every in-match event from `_dispatch_match_event_notifications` (one push per change; the old duplicate score-change block in the poller loop was removed).
   - `end`: full final content-state (same schema as updates) + `dismissal-date` (now + 4 h).
3. **Content-state schema** (`live_poller._la_content_state`): `match_id`, `sync_key`, `competition`, `home_team`, `away_team`, `home_score`/`away_score` (int or null), `status`, `clock`, `period`, `home_team_logo`, `away_team_logo`, `match_minute`, `prob_home/draw/away`, `half_start_time`, `kickoff_date` (ISO-8601 strings), plus `event` / `event_text`, plus the **clock fields** `timer_start`, `timer_paused_at`, `kickoff_time` (Swift `Date` = seconds since 2001-01-01, so the widget can use them as `Date` with ActivityKit's default decoder) and `timer_running` (Bool). `now - timer_start` = match time; when `timer_paused_at` is set the clock is frozen at `timer_paused_at - timer_start` (45:00 HT, 90:00 end of regular time, 105:00 ET HT, final at FT/penalties). Anchored from ESPN `clock_seconds` (`espn_parser`) at `last_polled_utc`, kept stable within 75 s per period. Never send a partial state.
   - **Period events**: halftime start (`halftime`), halftime end (`second half`), `extra time`, `penalties` → alert + regular push (`ht` / `second_half` / `extra_time` / `penalties`); `extra time second half` / `end of regular time` → silent update.
4. **Priority**: 10 for start/end/alerts, period changes and score corrections; 5 otherwise. Per device: if `frequent_updates` is False (`POST /api/live-activities/settings`), non-alert updates drop to 5. `activities_enabled` False → no push-to-start. `stale-date` = kickoff + `APNS_LA_STALE_AFTER_KICKOFF_SECONDS` (3 h). Alert `sound` goes **inside** the `alert` dict.
5. **Regular pushes** carry `match_id`, `sync_key`, `competition`, `event`, `aps.thread-id = match-<id>`, and `apns-collapse-id` = dispatcher dedupe key. Devices with an active Live Activity for the match are skipped (`exclude_live_activity_devices=True`) to avoid duplicate banners.
6. **STATUS_END_REGULAR_TIME is not full time** (cup extra time / penalties follow).
7. **Auth**: `POST /api/notifications` (broadcast), `POST /api/live-activities/update|end`, `GET /api/live-activities`, and every `/api/test/*` route require `MUTATION_API_TOKEN`. Device-facing routes (`/api/notifications/register|unregister|subscribe|unsubscribe*|preferences`, `/api/live-activities/register|unregister|settings|channel`, `/api/live-activities/push-to-start/register|unregister`) stay public.
8. **Broadcast channels (iOS 18+, `APNS_BROADCAST_ENABLED=1`, requires the Broadcast capability on the App ID)**: `POST /api/live-activities/channel` get-or-creates one channel per match/environment (only for known, unfinished matches; capped by `APNS_BROADCAST_MAX_ACTIVE_CHANNELS`) via `api-manage-broadcast[.sandbox].push.apple.com:2196/2195 /1/apps/<bundle>/channels` with storage policy `APNS_BROADCAST_STORAGE_POLICY` (default 0 = No Message Stored, so `apns-expiration: 0`). Every update/end is also published once per channel to `api[.sandbox].push.apple.com/4/broadcasts/apps/<bundle>` (`apns-channel-id`, no `apns-topic`). Push-to-start to `supports_broadcast` devices uses `input-push-channel` and records a `la_channel` marker. Channels are marked ended on the end push and deleted by `cleanup_expired_channels` (10-min tick in the LA start thread).
9. **Caching**: `/api/notifications*`, `/api/live-activities*`, `/api/test/*`, and all non-GET responses are `Cache-Control: no-store`.

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
./.venv2/bin/python -m unittest tests/test_live_activity_lifecycle.py
./.venv2/bin/python -m unittest tests/test_live_activity_clock_and_channels.py
./.venv2/bin/python -m unittest tests/test_notification_targeting.py
./.venv2/bin/python -m unittest tests/test_result_finalization.py
./.venv2/bin/python -m unittest tests/test_store_isolation.py
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

### C2. Critical Rule: Tests Never Touch the Production Store
- Under `unittest`/`pytest`, `sqlite_store._default_db_path()` resolves to a throwaway per-process database (`_running_under_test_runner` / `_test_isolated_db_path`). `BTS_SQLITE_STORE_PATH` or patching `_default_db_path` still selects a specific DB; `BTS_ALLOW_REAL_STORE_IN_TESTS=1` opts out (read-only diagnostics only).
- Unisolated tests previously wrote a fake *Liverpool 1-1 Everton* into production `past_games` (both clubs showed an extra Premier League game), 17 fake device/team/Live Activity subscriptions and 64 fake `match-100` feed events.
- Tests must **seed their own fixtures** (relative dates) instead of reading whatever production data exists, and must not hit ESPN (patch `regional_espn_schedule_fallback` / `merged_upcoming_file_is_fresh` as in `test_cup_mapping_and_deduplication.py` and `test_match_detail_prediction_stats.py`).
- After a full run, verify nothing was written: no `past_games.updated_at`, `notification_subscriptions.created_at` or non-ESPN `notification_history` rows newer than the run start.

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
| **2026-11-06** | Cup Team Mapping Safeguards, Match ID Propagation & Upcoming Deduplication | 1. Resolved false Oxford City -> Oxford (Oxford United) and Bristol Rovers -> Bristol City mappings in `Data/team_name_mapping_master.json` and master seeds.<br>2. Preserved club identity qualifiers ("City", "United", "Rovers", "Town", "Athletic") in `shared/team_mapping_groups.py::normalize_team_key` and `Website/competition_rules.py::normalize_team_key`.<br>3. Enforced qualifier isolation in `fuzzy_resolve_team_name` so lower-tier/cup teams cannot be hijacked by senior clubs.<br>4. Stopped `_annotate_upcoming_rows_with_live` from wiping `row["match_id"]` for scheduled games, and preloaded recent live history into O(1) in-memory indices.<br>5. Implemented `_deduplicate_and_merge_upcoming_rows` in `Website/routes/predictions.py` merging duplicate records by match_id, sync_key, and canonical fixture identity while preserving predictions and ESPN match_ids.<br>6. Cleaned contaminated SQLite `upcoming_games` and `live_score_history` rows. | Added `tests/test_cup_mapping_and_deduplication.py` (6/6 PASSED), full regression suites passed (44/44 total tests PASSED). |
| **2026-11-07** | In-App Notification Feed, Persistent History & Sync/Team Matching | 1. Wired `record_notification_event` into `live_poller.py` for all in-match events (kickoff, halftime, goals, red cards, fulltime) and created `notification_history` SQLite table so `GET /api/notifications` returns real-time game alerts across restarts.<br>2. Added rich query filtering to `GET /api/notifications` by `match_id`, `sync_key`, `team`, and `type`.<br>3. Enhanced `POST /api/notifications/register` to accept `token`/`device_token`/`push_token`, default to iOS, persist registrations in SQLite (`notification_subscriptions`), and auto-subscribe initial `favorite_teams` / `matches`.<br>4. Upgraded `for_game_or_team_subscribers`, `for_match_tokens`, and `for_team_tokens` in `Website/notifications.py` to support `sync_key` lookups alongside numeric `match_id` and canonical team aliases (resolving Chelsea, Swansea City vs Swansea, Man United, etc.).<br>5. Safe-guarded APNs auto-pruning to only delete verified dead tokens (410 or fatal bad 64-char hex) instead of purging dev tokens on transient network/sandbox mismatches. | Added `tests/test_notifications_feed_and_sync.py` (4/4 PASSED), all notification & regression suites passed (53/53 total tests PASSED). |
| **2026-11-07** | Live Activities (ActivityKit) SQLite Persistence & APNs Diagnostic Testing | 1. Saved Live Activity subscriptions to SQLite (`notification_subscriptions` with `sub_type='live_activity'`) so lock screen and Dynamic Island widgets survive backend restarts and daily reload.<br>2. Added dual `match_id` and `sync_key` lookup in `Website/notifications.py::for_match` and `Website/live_poller.py` so matches registered via `sync_key` before ESPN match numbers appear continue receiving live clock and score updates.<br>3. Added `GET /api/live-activities` endpoint with `match_id` and `sync_key` filters.<br>4. Enriched ActivityKit content-state payload with `sync_key`, clock, period, logos, and event details.<br>5. Added real ES256 JWT generation, ActivityKit topic suffix enforcement (`.push-type.liveactivity`), and diagnostic push simulation in `test_live_activities_and_apns.py`. | Added `tests/test_live_activities_and_apns.py` (6/6 PASSED). |
| **2026-11-07** | MLS Upcoming Fixture Alias Deduplication & Multi-Source Merge Hardening | 1. Identified root cause of duplicated MLS games: upcoming rows with raw team aliases (e.g. `Seattle Sounders FC` vs `LAFC`) and canonical names (`Seattle Sounders` vs `Los Angeles FC`) shared identical `sync_key`s but produced separate primary keys in `upcoming_games` (`past_game_storage_key`).<br>2. Updated `shared/sqlite_store.py::past_game_storage_key` to canonicalize home and away team names so alias variations generate identical storage keys.<br>3. Hardened `_upsert_upcoming_conn` to detect existing records matching `sync_key`, merge payloads, and remove obsolete alias keys.<br>4. Supported `source` and `mode` query arguments in `sqlite_store.load_upcoming_games`.<br>5. Enhanced `_fixture_dedup_key` in `Website/routes/predictions.py` to key by `sync_key` or canonical team slugs rather than raw strings.<br>6. Upgraded `_deduplicate_and_merge_upcoming_rows` to index both primary and secondary records across `match_id`, `sync_key`, canonical team tuple, and sorted canonical team pair.<br>7. Upgraded `api_home_upcoming()` (`/api/home/upcoming`) to run feed through `_annotate_upcoming_rows_with_live` and `_deduplicate_and_merge_upcoming_rows` instead of raw string hashing, eliminating duplicate cards on the home page.<br>8. Updated `api_upcoming(mode)` for `mode == "mls"` to load and merge SQLite upcoming predictions even when the CSV only contains Liga MX, preventing missing or duplicate MLS fixtures.<br>9. Hardened `Website/routes/live_scores.py` second pass to track `matched_upcoming_keys` and canonical team pairs. | Added `tests/test_mls_alias_deduplication.py` (6/6 PASSED), full regression suite passed (55/55 total tests PASSED). |
| **2026-11-07** | Backend Startup Reload-Only Configuration (`--no-run-on-start`) | 1. Configured `--no-run-on-start` in `deploy/macos/com.beyondthestats.backend.plist`, `deploy/beyond-the-stats.service`, and `startbts.py` foreground mode so backend restarts load existing models and SQLite storage in seconds without triggering pipeline execution.<br>2. Added automatic launchd plist verification in `startbts.py` to inject `--no-run-on-start` into installed LaunchAgents.<br>3. Fixed `FutureGamesWatcher` in `main/Backend/scheduler.py` to refresh signatures after light refresh callbacks, preventing recursive poll loops. | `tests/test_backend_startup.py` (6/6 PASSED). |
| **2026-11-08** | APNs & Live Activity Audit Fixes: start/update/end Lifecycle, Push-to-Start, Auth & Cleanup | 1. Fixed invalid `aps.event` values (kickoff/goal/halftime/red card/score) — APNs now only receives `start`/`update`/`end`; labels moved to `content-state.event` (`Website/notifications.py::build_live_activity_aps`).<br>2. Added ActivityKit push-to-start: `register_push_to_start` / `send_live_activity_start` and `POST /api/live-activities/push-to-start/register|unregister` (also accepted as `push_to_start_token` on `/api/notifications/register`); `live_poller._live_activity_start_loop` sends `start` 5 min before kickoff.<br>3. Full final content-state on `end`; scores coerced to int/null; `stale-date`; alert `sound` inside `alert`; priority 5 vs 10.<br>4. Period-change updates (2nd half / ET / penalties), VAR score corrections, `STATUS_END_REGULAR_TIME` no longer treated as FT, orphaned activities ended silently when a match is first seen finished; removed duplicate score push.<br>5. `mutation_authorized` on broadcast, LA update/end, LA listing; router-wide admin dependency on `/api/test/*`; `no-store` caching on notification routes.<br>6. Devices with an active Live Activity no longer get duplicate regular pushes; regular payloads add `sync_key`/`competition`/`event`, `thread-id`, `apns-collapse-id`.<br>7. `/api/notifications/unregister` now fully removes a device (`unregister_device`); unsubscribe works without competition; team alias keys rebuilt on reload; LA/push-to-start tokens no longer loaded into the device registry; LA SQLite rows deleted at match end (by match_id and sync_key); token environment persisted (`environment` column); APNs worker errors logged; fixed missing `logger` in `routes/live_scores.py`.<br>8. SQLite: added `device_token`, `sync_key`, `environment` columns to `notification_subscriptions` (auto-migrated). | Added `tests/test_live_activity_lifecycle.py` (22/22 PASSED); updated notification suites (41/41 PASSED). Full suite: 314 run, 19 pre-existing failures unrelated to notifications (identical on the pre-change baseline), 0 regressions. |
| **2026-11-08** | Live Activity Clock, Period Events, Broadcast Channels, Red-Card Dedupe, Frequent-Updates Settings & /api/startup Fix | 1. All-or-none notifications: `normalize_events` always returns every event type; added `second_half`, `extra_time`, `penalties` event types.<br>2. Apple-timer clock fields in content-state (`timer_start`, `timer_paused_at`, `timer_running`, `kickoff_time` as Swift reference-date seconds), anchored from new ESPN `clock_seconds` (`Website/espn_parser.py`); halftime pause and second-half resume; halftime end / extra time / penalties alert + regular push at priority 10.<br>3. iOS 18 broadcast channels: `get_or_create_match_channel`, `apns_broadcast_send`, `cleanup_expired_channels`, `register_channel_activity`; `POST /api/live-activities/channel`; channel-based `register`/`unregister`; push-to-start `input-push-channel`; new `live_activity_channels` table; `APNS_BROADCAST_*` config (off by default).<br>4. Red-card dedupe across scoreboard `red_cards` and summary `key_events` (surname/side/minute fingerprint, accent/suffix folding) and pre-marking existing cards on first observation (no re-alerts after restart).<br>5. Frequent-updates / activities-enabled / iOS version settings: `POST|GET /api/live-activities/settings` (also accepted on `/api/notifications/register` and push-to-start register); new `live_activity_device_settings` table; per-device priority policy.<br>6. Fixed `/api/startup` always returning an empty `upcoming_today` (undefined `_ALL_UPCOMING_SOURCES` + wrong-module import, both swallowed) and the same missing import in the `/api/match` CSV fallback; failures are now logged. | Added `tests/test_live_activity_clock_and_channels.py` (23/23 PASSED); updated `tests/test_notifications.py` and `tests/test_live_activity_lifecycle.py` for all-or-none + new period alerts. Full suite: 337 run, same 19 pre-existing unrelated failures as baseline, 0 regressions. |
| **2026-11-08** | Follower-Only Notification Targeting (Teams by ID, Match Key Coverage) | 1. Audited every APNs send path: event alerts, Live Activity updates/ends, push-to-start and channel broadcasts all target followers only; found team-name leaks and misses.<br>2. Replaced name/prefix team matching with resolved team-id matching (`resolve_team_id`, `_team_follow_records`, `followed_teams`); `subscribe_team` / `unsubscribe_team` / `for_team_tokens` accept `team_id`; unsubscribe by any alias deletes all matching SQLite rows (incl. legacy); `team_id` column added to `notification_subscriptions`.<br>3. `for_match_tokens` now always includes every registry key for the match (previously only when exact keys found nobody); `normalize_match_ident` for client-supplied ids/sync keys.<br>4. Routes: `team_id` / `sync_key` on subscribe & unsubscribe, `favorite_teams` objects on register, `team_ids` / `followed_teams` / `followed_matches` in preferences; admin broadcast and test push require an explicit target or `audience: "all"`. | Added `tests/test_notification_targeting.py` (14/14 PASSED). Full suite: 351 run, same 19 pre-existing unrelated failures, 0 regressions. |
| **2026-11-08** | Result Finalization: Live Finals → Standings, past_games & Team Form | 1. Finals now written to SQLite `past_games` at the final whistle (`_sync_finished_games_to_past_games`), not only by the nightly pipeline archive (27 of today's 29 finals were missing from `past_games`, so team last-5 / momentum / H2H lagged a day). Startup backfill syncs missed finals; post-FT score corrections rewrite the row and re-trigger table refresh.<br>2. Fixed lost standings triggers: the poller's inner `_flush_completed_live_scores_to_history` consumed new finals so the outer call returned an empty set; finals seen by the on-demand refresh or rollover never patched league-data / `standings_cache.json`. Results are now queued and drained by `_drain_finalized_results`.<br>3. Cache invalidation after results: analytics history, last-5/recent-match caches (per-entry timestamps — a shared timestamp let old entries stay "fresh" forever), next-game index, league leaders, team overviews (alias-aware + background index rebuild) and endpoint-targeted response caches (`cache._cache_clear_endpoints`).<br>4. Team identity by resolved team id in `load_team_recent_matches` (now also reads finished `live_score_history` rows, as documented) and `match_analytics` (team overview for "Manchester United", "PSG", "Paris Saint-Germain" returned an empty last-5); split spellings merged; prediction:/fixture: duplicate rows collapsed.<br>5. MLS / Liga MX `Process_Data.convert_mls_raw_to_standard` parsed football-data DD/MM dates month-first (swapping every day ≤ 12, producing 40 "completed" results dated in the future); now `dayfirst=True` like europe/extra. Readers also drop future-dated completed rows.<br>6. `match_analytics` uses the store's real DB path (honours `BTS_SQLITE_STORE_PATH`); AGENTS.md DB path corrected to `Output/Status/bts_store.db`. | Added `tests/test_result_finalization.py` (19/19 PASSED, 10× repeat stable). Full suite: 370 run, same 19 pre-existing unrelated failures, 0 regressions. |
| **2026-11-08** | Premier League Extra Game (Everton/Liverpool) — Test Data Leaking Into Production Store | 1. Root cause: `tests/test_past_games_retention.py` redirected only the JSON archive, so `save_completed_rows_to_past_games` upserted its fixture (`prediction:new-key`, Liverpool 1-1 Everton, 2026-09-18) into production `past_games` on every suite run; `collect_competition_games` counted it, giving Everton and Liverpool 11 played. Other unisolated suites (`test_notifications.py`, `test_live_notifications.py`, `test_live_activity_goal_updates.py`) leaked fake subscriptions and `match-100` feed events.<br>2. `shared/sqlite_store._default_db_path` now isolates test runs to a temp store; retention test sets `BTS_SQLITE_STORE_PATH`.<br>3. Tests that silently depended on production data now seed their own fixtures: `test_match_detail_prediction_stats.py` (3 previously skipping tests run), `test_cup_mapping_and_deduplication.py::test_api_upcoming_deduplication_and_match_id` (hardcoded 2026-11-06, read live FA Cup data).<br>4. Seeded parity test exposed `/api/match/<sync_key>` missing `live_status`, `live_updates`, `live_updates_eligible`, `prediction_note` for SQLite-only fixtures; detail endpoint now applies `_annotate_upcoming_rows_with_live` for pre-match sources.<br>5. `past_game_storage_key` treated NaN `prediction_key` as the literal `"nan"`, collapsing all key-less rows into `prediction:nan` (overwrites); added `_clean_prediction_key`.<br>6. Production cleanup (backup `Output/Status/bts_store.db.bak-before-test-cleanup-20261108130249`): removed the fake past game, `sim-999` live row, 64 fake feed rows, 17 fake subscriptions; re-keyed the `prediction:nan` row. PL table: Everton 10 P / 16 pts, Liverpool 10 P / 18 pts. | Added `tests/test_store_isolation.py` (5/5 PASSED). Full suite: 375 run, 0 skipped, same 19 pre-existing unrelated failures, 0 regressions; production store verified untouched after the run. |
| **2026-11-09** | National Team Competitions: UEFA Nations League, Euro & CONCACAF Nations League Integration | 1. Added UEFA Nations League (`uefa.nations`), Euro (`uefa.euro`), and CONCACAF Nations League (`concacaf.nations.league`) with aliases to `Website/config.py::LIVE_SCORE_COMPETITIONS` for live score polling and event alerts.<br>2. Added `North America/Concacaf Nations League` (`concacaf.nations.league`) to `UPCOMING_ESPN_COMPETITIONS` and `RECENT_ESPN_COMPETITIONS` in `Process_National_Team_Data.py`.<br>3. Added `North America/Concacaf Nations League` to `fetch_league_teams.py`, `Update_Live_Prediction_Results.py`, `accuracy_tracker.py` (`national` pipeline mapping), and `Website/config.py::TOURNAMENT_KEY_MAP`.<br>4. Updated `Predict_Upcoming_National_Team_Games.py::competition_configs` to return all in-season non-World-Cup competitions (including Nations Leagues and Euros) when `friendlies_only` is set, dynamically pruning only the fetched competitions outside the lookahead window.<br>5. Updated `is_national_team_competition` in `Website/config.py` to identify all international tournaments (including `European Championship`, `Nations League`, `Gold Cup`, `Copa America`, `World Cup`, etc.).<br>6. Verified upcoming fixture fetching and prediction generation (52 UEFA Nations League and 32 CONCACAF Nations League matches predicted and synced to SQLite `upcoming_games`). | Added `tests/test_national_competitions.py` (6/6 PASSED). |
