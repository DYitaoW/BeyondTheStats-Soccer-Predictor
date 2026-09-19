# Unused / manual-only files (post-reorg audit)

These files are still in the tree but are **not** invoked by `main/Run_All_Pipeline.py`,
`main/Daily_Pipeline.py`, or the Website backend. Kept for manual/CLI use unless noted.

## Likely unused (safe candidates to delete later)

| File | Notes |
|------|--------|
| `pipelines/mls/files/Predict_MLS_Cup_Games.py` | Orphaned; nothing imports or runs it. MLS Cup odds come from `Project_League_Table.py`. |
| `pipelines/*/files/Print_Upcoming_Predictions.py` | Manual pretty-printer for upcoming CSVs. |
| `pipelines/*/files/Test_train.py` | Ad-hoc training experiments; not part of daily pipeline. |
| `pipelines/europe/files/Run_World_Cup_Pipeline.py` | Standalone WC helper; daily path uses `Process_National_Team_Data` / `Project_World_Cup` directly when WC is active. |
| `scripts/website_diagnostic.py` | One-off diagnostic printer. |

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
