# Website Data-Flow Audit (for mobile-app feed)

Source: `Website/app.py`, `Website/static/shared.js`, `Website/static/world_cup.js`.

## API routes, sources, rendered fields

| Route / API | Source file(s) | Row fields used (server-side: `_load_upcoming_rows` / `_load_projected_tables` allowlist + add) | Rendered fields (client: `shared.js` / `world_cup.js`) |
|---|---|---|---|
| `/` (home) | `GLOBAL_UPCOMING_FILE` | Up to 6 high-confidence rows (filtered through `pickRandomRows`/`isValidProbabilityRow`) | `home_team, away_team, competition, predicted_result, winner_label, prob_home, prob_draw, prob_away, pred_home_goals, pred_away_goals, actual_result, time_label` |
| `/upcoming-matches` | `GLOBAL_UPCOMING_FILE` / `MLS_UPCOMING_FILE` / `EXTRA_UPCOMING_FILE` / `CUP_UPCOMING_FILE` | full row set, grouped by day | same as above; `renderUpcoming` uses `r.competition, r.home_team, r.away_team, r.winner_label, r.pred_home_goals, r.pred_away_goals, r.prob_home, r.prob_draw, r.prob_away, r.time_label, r.actual_result, r.is_correct, r.weekday, r.date_label` (cards click → H2H) |
| `/cups` | `CUP_UPCOMING_FILE` + `CUP_PROJECTED_TABLE_FILE` + `CUP_PROJECTED_BRACKET_FILE` | per-cup tabs; `activeCupSelection().competitions` filter; `cup_brackets` JSON | upcoming rows as above; tables as League Tables view; cup-bracket JSON passed to `renderCupBracket` (consumes `data.cup_brackets`) |
| `/league-tables` | `GLOBAL_PROJECTED_TABLE_FILE` / `MLS_PROJECTED_TABLE_FILE` / `EXTRA_PROJECTED_TABLE_FILE` / `CUP_PROJECTED_TABLE_FILE` (+ `MLS_PROJECTED_BRACKET_FILE` for `mls`, + `CUP_PROJECTED_BRACKET_FILE` for `cups`) | all `_load_projected_tables` columns (see below) | `position, team, P, W, D, L, GF, GA, GD, Pts` (standings view); `win_league_pct, top4_pct, bottom3_pct, most_likely_position, most_likely_position_pct` (probability view); bracket view renders `data.bracket` (MLS) / `data.cup_brackets` (cups) |
| `/world-cup` | `Data/Predictions/world_cup_projection.json` | entire file | `data.knockout, data.champion, data.simulations.winner_probabilities, data.rules_summary, data.group_tables, data.third_place_table, data.group_fixtures, data.generated_at_utc, data.simulations.simulations_run, data.competition`; per-fixture: `display_home_team, home_team, display_away_team, away_team, winner_team/winner, match_date, match_datetime_utc, venue, pred_home_goals, pred_away_goals, label, stage, prob_home, prob_draw, prob_away, predicted_result` |
| `/players` | `Data/Team_Data/current_season_top_scorers.json` | `last_updated_utc, competitions, available_leagues` | per-competition player rows (schema defined in pipeline, e.g. `player, team, goals, apps, ...`); consumer not yet wired in `shared.js` (`/api/scorers` JSON exists but no renderer). |
| `/head-to-head` | `Data/...` H2H lookup, `Data/Current_Form/current_form_*.json`, etc. | `data.team1_form, data.team2_form, data.h2h_data, data.h2h_data_reverse, data.h2h_total_games` | `points_last_10, wins_last_10, draws_last_10, losses_last_10, avg_goals_for_last_10, avg_goals_against_last_10, avg_shots_for_last_10, avg_shots_against_last_10, home_wins, home_draws, home_losses` (in both directions) |
| `/custom-predictor` | live `ctx.clf` + team history | none (computed on demand) | `home_team, away_team, competition, predicted_result, winner_label, prob_home, prob_draw, prob_away, pred_home_goals, pred_away_goals, pred_home_shots, pred_away_shots, pred_home_sot, pred_away_sot` |
| `/market-odds` | `Data/Market_Values/...` + same predictors | live | (no fields in `shared.js` for this — uses `data` from `/api/predict`) |
| `/position-odds` | same projected-table CSVs | position-odds subset | `position, team, most_likely_position, position_odds` (dict: pos→pct) |
| `/about`, `/tactics` | static templates | none | none |

## _load_upcoming_rows output schema
Required (allowlist `LOW_MEMORY_STATIC`, lines 866-887): `match_date, competition, home_team, away_team, predicted_result, prob_home, prob_draw, prob_away, pred_home_goals, pred_away_goals, pred_home_shots, pred_away_shots, pred_home_sot, pred_away_sot, probability_reasoning, actual_result, match_datetime_utc, match_datetime_et`.
Server adds: `display_home_team, display_away_team, weekday, date_label, time_label, winner_label, is_correct, confidence`.

## _load_projected_tables output schema
Allowed (lines 1046-1067): `competition, position, team, P, W, D, L, GF, GA, GD, Pts, PlayedReal, PlayedPred, win_league_pct, top4_pct, bottom3_pct, most_likely_position, most_likely_position_pct, position_odds_json, sim_runs, remaining_games`.
Server parses `position_odds_json` into dict `position_odds` (1-based position → pct) — that's the only nested field.

## Core vs display-only fields (for mobile-app filtering)
**Core (must keep in feed)**
- Fixtures: `match_date, competition, home_team, away_team, predicted_result, prob_home, prob_draw, prob_away, pred_home_goals, pred_away_goals, actual_result, winner_label`.
- Tables: `competition, position, team, P, W, D, L, GF, GA, GD, Pts`.
- Tables (prob view): `win_league_pct, top4_pct, bottom3_pct, most_likely_position, position_odds`.
- World cup: champion, group tables, third-place table, knockout bracket, winner probabilities, simulations count, generated_at_utc, fixtures (subset of core fixture fields above).
- Top scorers: `last_updated_utc, competitions`.

**Display-only (drop or downsample for mobile)**
- `pred_home_shots, pred_away_shots, pred_home_sot, pred_away_sot` (only used by predictor page; upcoming card never renders them).
- `probability_reasoning` (none of the rendered cards consume it; can be omitted).
- `display_home_team, display_away_team` (UI aliasing for the home/upcoming pages only; mobile can apply display-name logic on the client or have the server embed it).
- `match_datetime_et` (timezone-converted variant; not rendered — only `match_datetime_utc`/formatted `time_label` is used).
- `PlayedReal, PlayedPred, remaining_games` (helper columns; not rendered anywhere).
- `sim_runs` (only displayed in the upcoming-page header; mobile can hardcode or omit).
- `position_odds_json` (raw string — server parses it; mobile should consume parsed `position_odds` only).

**Excluded (user-specific, not part of a feed)**
- H2H (`/api/h2h`): user-driven; not feed material.
- Predictor (`/api/predict`): user-driven; not feed material.
- Team list (`/api/teams`): user-driven; not feed material.

## Notes for the mobile feed
- The 11 fields picked in `Daily_Pipeline._FIXTURE_FIELDS` cover the core fixture set; consider trimming `match_datetime_utc` (kept for sorting) but dropping the rest of the display-only fields.
- `_TABLE_FIELDS` is 11 keys that match the rendered standings columns exactly — already lean.
- The world-cup block is large because `world_cup.js` consumes knockout + groups + third-place + fixtures + sims. Keep all of them; the WC projection is the only feed source that requires full JSON.
- Cup brackets (`CUP_PROJECTED_BRACKET_FILE` and `MLS_PROJECTED_BRACKET_FILE`) are passed through as-is — schema_version=1 doesn't define their inner shape.
