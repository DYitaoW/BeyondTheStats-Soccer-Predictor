# Session Status (updated)

## Changes applied (all uncommitted)

### Server / pipeline reliability
1. **`Backend/server.py`** — restart-hang fixed: `_shutdown()` always runs (try/finally); pipeline spawned detached (`start_new_session=True`, `PYTHONUNBUFFERED=1`); kill now terminates whole process tree (`taskkill /PID /T /F` Windows / `os.killpg` POSIX).
2. **`Run_All_Pipeline.py`** — `run_step` streams child output live; added missing 3600s timeouts (`global_upcoming_matchweek`, `global_upcoming_cups`); standalone `main()` now calls `publish_to_output()`.
3. **`files/Update_Live_Prediction_Results.py`** — cups removed (`cup_df = None`); `Track_Cup_Results.py` is sole cup owner (fixes double-settle, lost `completed_cup_predictions.csv` archiving, duplicate ESPN scraping).

### API speed
4. **`football_data_api.py`** — rate limiter REWRITTEN again (2026-09-12): moved from fixed 120s spacing to a **rolling 60s per-minute budget** (`_request_timestamps` deque; default 60 req/min via `FOOTBALL_DATA_API_MAX_REQUESTS_PER_MINUTE`, safe up to 100). Cache hits return instantly; live calls only wait when the window is actually full. Verified: burst of 3 instant, 4th in-window waits ~60s, window prune frees slots. `FOOTBALL_DATA_API_DELAY_SECONDS` now back-compat min-gap (default 0); 429 retries wait for a free slot instead of sleeping blindly. Old `wait_between_competition_requests` removed earlier; 4 call sites updated (Predict_Upcoming_Matchweek :844/:1030/:1097 regions, Predict_Upcoming_National_Team_Games :212).
5. **`espn_api_cache.py` (NEW)** — per-league ESPN disk cache: `Data/ApiCache/espn/<espn_id>/<yyyymmdd>.json`, atomic writes, TTL 2h today/past + 24h future, `max_age_seconds` override param, `clear_cache()`. Wired into:
   - `files/Predict_Upcoming_Cups` 366-day crawl (was 366 uncached calls per cup)
   - `files/Update_Club_Friendlies.py` 365-day crawl + removed dead `fetch_json`/ESPN constant/unused imports
   - friendlies result-sync poller path uses `max_age_seconds=60`

### Live poller call-rate fix
6. **`Website/live_poller.py`** — pre-game summary throttling. Before: EVERY pre+in game re-fetched its full ESPN summary EVERY 60s cycle (~30-50 calls/min busy matchday). Now: `in` games still every cycle (real-time key events), `pre` games once then max once/10min (`_PRE_SUMMARY_RETRY_S=600`, tracked in `_summary_fetch_meta`, cleared on day boundary). Simulated: 35/min -> 17/min (-51%) with 20 pre + 15 in games; static h2h/last-five no longer hammered.

## Diagnosis: why frontend shows "no live events"
- Poller only polls competitions found with fixtures dated TODAY-ET by `_get_todays_competitions()` (sources: 6 upcoming CSVs + WC JSON + cup bracket JSON).
- STATUS 2026-09-12 22:15 ET: all 7 detection CSVs REGENERATED (global upcoming, MLS, extra, national, cup, friendlies) — mtimes 9/12 ~10 PM local (02:09Z). None is "missing" anymore. However `today_count=0` in ALL of them because the fixture windows start 9/13 (MLS+extra recommence Sun) — legitimately no games TODAY via CSV path; poller falls back to live ESPN discovery for in-progress games.
- Root cause of the earlier "missing/stale" message was MISSING/stale source CSVs (pipeline steps hadn't produced them), NOT the football-data.org rate limit. Rate limiting only paces HTTP requests; it cannot mark data stale. A too-strict fixed 120s pause could only delay a run, never corrupt output.
- Tiers are CORRECT (`config.py`): PL/La Liga/Serie A/Bundesliga/Ligue 1/Championship/Liga Portugal/Eredivisie/MLS = full; second divisions reduced; ~22 small leagues result-only; UEFA comps deferred until 2026-09-01. Not a tier problem — a detection-data problem.
- Fix path: re-run upstream generators (global upcoming matchweek, MLS/extra upcoming, friendlies sync) so fresh CSVs exist; verify via `/api/debug/live-score-sources`. My publish_to_output fix refreshes Output trees but cannot recreate missing upstream CSVs. NOW DONE (files exist); confirm detection via the debug endpoint next.

## Pending user decisions (reported, not changed)
- past_games.json parallel write race (3 sub-pipelines + Website archive; `.past_games_counter` missing so pruning never fires)
- MLS model cache built unconditionally despite light-day `--skip-model-train`
- orphaned `MLS/files/Predict_MLS_Cup_Games.py` (writes file nothing reads)
- MLS CSV column drift (long-form `match_date`; extra `match_datetime_et`; missing `match_datetime_utc`)
- `/api/last-refresh` unused by frontend (uses `/api/stats` refreshed_at) + naive-ET vs UTC mismatch
