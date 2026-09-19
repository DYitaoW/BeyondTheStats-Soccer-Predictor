# Beyond-the-Stats folder structure

Canonical paths live in `shared/paths.py`. Prefer importing from there (or
`Website.config`, which re-exports the same roots) instead of hard-coding.

```
Beyond-the-Stats/
  main/                      # Entry points + persistent backend
    Daily_Pipeline.py
    Run_All_Pipeline.py
    run_backend.py
    bts.py
    Backend/                 # scheduler, memory monitor, server
  pipelines/
    europe/files/            # Global / UEFA / cups / national / friendlies
    mls/files/ + Data/       # MLS + Liga MX helpers
    extra/files/ + Data/     # Extra leagues
  shared/                    # Cross-cutting libraries (paths, calendar, cache, …)
  Website/                   # Flask API + templates/static
  Output/                    # ALL generated artifacts (gitignored)
    Predictions/{europe,mls,extra,cups,national,friendlies,shared}/
    LeagueData/ CupData/ Upcoming/ Status/ Logs/ Cache/
  Data/                      # Shared inputs / seeds / national / info
    Seeds/                   # league_teams, current_season_teams, espn_*_seen
    National_Team_Data/
    Info/
    Raw_Data/ Processed_Data/ Team_Data/   # Europe working data (generated)
  scripts/ docs/ deploy/ public/ tests/
```

## Entry points

| Task | Command |
|------|---------|
| Full pipeline | `python main/Run_All_Pipeline.py` |
| Daily loop / feed | `python main/Daily_Pipeline.py --once` |
| Persistent backend | `python main/run_backend.py` |
| Website only | `gunicorn --chdir Website -c gunicorn_config.py app:app` |

## Output layout

Prediction CSVs and JSON brackets are written under `Output/Predictions/<region>/`.
Runtime status (`last_refresh.json`, `pipeline_status.json`, …) lives in
`Output/Status/`. League/cup API caches use `Output/LeagueData/` and
`Output/CupData/`.
