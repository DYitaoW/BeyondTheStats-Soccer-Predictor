# Session Status (updated)

## Changes applied (all uncommitted)

### Server / pipeline reliability
1. **`Backend/server.py`** — restart-hang fixed: `_shutdown()` always runs (try/finally); pipeline spawned detached (`start_new_session=True`, `PYTHONUNBUFFERED=1`); kill now terminates whole process tree (`taskkill /PID /T /F` Windows / `os.killpg` POSIX).
2. **`Run_All_Pipeline.py`** — `run_step` streams child output live; added missing 3600s timeouts (`global_upcoming_matchweek`, `global_upcoming_cups`); standalone `main()` now calls `publish_to_output()`.
3. **`files/Update_Live_Prediction_Results.py`** — cups removed (`cup_df = None`); `Track_Cup_Results.py` is sole cup owner (fixes double-settle, lost `completed_cup_predictions.csv` archiving, duplicate ESPN scraping).

### API speed
4. **`football_data_api.py`** — rate-limit pause moved inside `fetch_json` (`wait_between_requests` + `_last_request_ts`): sleeps only before LIVE HTTP requests; cache hits return instantly. Old behavior slept 120s before every competition even on cache hits (~45 min/run). Verified by test: hit 0.01s, spaced misses enforced. Old `wait_between_competition_requests` removed; 4 call sites updated (Predict_Upcoming_Matchweek :844/:1030/:1097 regions, Predict_Upcoming_National_Team_Games :212).
5. **`espn_api_cache.py` (NEW)** — per-league ESPN disk cache: `Data/ApiCache/espn/<espn_id>/<yyyymmdd>.json`, atomic writes, TTL 2h today/past + 24h future, `max_age_seconds` override param, `clear_cache()`. Wired into:
   - `files/Predict_Upcoming_Cups` 366-day crawl (was 366 uncached calls per cup)
   - `files/Update_Club_Friendlies.py` 365-day crawl + removed dead `fetch_json`/ESPN constant/unused imports
   - friendlies result-sync poller path uses `max_age_seconds=60`

### Live poller call-rate fix
6. **`Website/live_poller.py`** — pre-game summary throttling. Before: EVERY pre+in game re-fetched its full ESPN summary EVERY 60s cycle (~30-50 calls/min busy matchday). Now: `in` games still every cycle (real-time key events), `pre` games once then max once/10min (`_PRE_SUMMARY_RETRY_S=600`, tracked in `_summary_fetch_meta`, cleared on day boundary). Simulated: 35/min -> 17/min (-51%) with 20 pre + 15 in games; static h2h/last-five no longer hammered.

## Diagnosis: why frontend shows "no live events"
- Poller only polls competitions found with fixtures dated TODAY-ET by `_get_todays_competitions()` (sources: 6 upcoming CSVs + WC JSON + cup bracket JSON).
- Current disk state: global `upcoming_matchweek_predictions.csv` MISSING, `upcoming_club_friendlies.csv` MISSING, MLS/Extra stale Jul 12, cups Jun 10, national Jun 8 -> zero leagues detected -> empty `_live_scores` -> `/api/live-scores` returns "No live games" and rows get `live_updates:false`.
- Tiers are CORRECT (`config.py`): PL/La Liga/Serie A/Bundesliga/Ligue 1/Championship/Liga Portugal/Eredivisie/MLS = full; second divisions reduced; ~22 small leagues result-only; UEFA comps deferred until 2026-09-01. Not a tier problem — a detection-data problem.
- Fix path: re-run upstream generators (global upcoming matchweek, MLS/extra upcoming, friendlies sync) so fresh CSVs exist; verify via `/api/debug/live-score-sources`. My publish_to_output fix refreshes Output trees but cannot recreate missing upstream CSVs.

## Pending user decisions (reported, not changed)
- past_games.json parallel write race (3 sub-pipelines + Website archive; `.past_games_counter` missing so pruning never fires)
- MLS model cache built unconditionally despite light-day `--skip-model-train`
- orphaned `MLS/files/Predict_MLS_Cup_Games.py` (writes file nothing reads)
- MLS CSV column drift (long-form `match_date`; extra `match_datetime_et`; missing `match_datetime_utc`)
- `/api/last-refresh` unused by frontend (uses `/api/stats` refreshed_at) + naive-ET vs UTC mismatch
