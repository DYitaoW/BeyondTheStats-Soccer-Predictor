# Unused / manual-only files (post-reorg audit)

These files are still in the tree but are **not** invoked by `main/Run_All_Pipeline.py`,
`main/Daily_Pipeline.py`, or the Website backend. Kept for manual/CLI use unless noted.

## Removed (2026-11-18)

Deleted after confirming nothing imports, runs or documents them as live:
`shared/prediction_engine.py`, `Website/services/`, `Website/markets/`, seven empty
`Website/*.py` stubs (`fixtures_loader`, `form_indexer`, `market_odds`, `past_games_archive`,
`pipeline_runner`, `predictions_loader`, `projected_tables`), `Website/features/calibration.py`
(calibration lives in `shared/probability_calibration.py`), `pipelines/mls/files/Predict_MLS_Cup_Games.py`,
both `Print_Upcoming_Predictions.py`, both `Test_train.py`, `pipelines/europe/files/Run_World_Cup_Pipeline.py`,
`Website/gunicorn_config.py` + `render.yaml` (old Render deploy), `scripts/website_diagnostic.py`,
`scripts/migrate_data_storage.py` (would move the live `pipelines/*/Data` trees), `SESSION_NOTES.md`.
Git history keeps them.

The four `pipelines/<region>/files/Predict_Match.py` copies were merged into
`pipelines/predict_match_core.py`; the regional files are now thin profiles.

## Manual utilities (used on demand)

| File | Notes |
|------|--------|
| `pipelines/europe/files/fetch_league_teams.py` | Rebuilds `Data/Seeds/league_teams.json`. |
| `pipelines/europe/files/generate_season_teams.py` | Seeds `Data/Seeds/current_season_teams.json`. |
| `scripts/audit_team_names.py` | Mapping coverage audit. |
| `scripts/populate_mapping.py` | Mapping bootstrap helper. |
| `scripts/clear_raw_data.py` | Wipes Raw_Data trees before a full re-download. |
| `scripts/build_static_frontend.py` | Static frontend build (if present). |
| `main/bts.py` | CLI predict/status helper. |

## Still used (via Download_Latest_Data, not Run_All directly)

`Process_Data.py` and `Sort_Data.py` in each region are imported by that region's
`Download_Latest_Data.py` (the pipeline step named "Download, process and sort").
