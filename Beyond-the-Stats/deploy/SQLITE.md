# SQLite history store (backend)

The backend uses Python’s built-in **`sqlite3`** module. There is **nothing to
pip-install** for SQLite itself.

## Persistence (not RAM)

The database is a normal **on-disk file**:

```
Beyond-the-Stats/Output/Status/bts_store.db
```

(+ optional `bts_store.db-wal` / `bts_store.db-shm` while the process is open)

| Event | What happens to the DB |
|-------|-------------------------|
| PC / server reboot | Survives — file is on the filesystem |
| Backend crash / kill | Survives — writes use `synchronous=FULL` + WAL checkpoint |
| `git pull` / checkout | Survives — `Output/**` is gitignored (not in the repo) |
| `git clean -fdx` of `Output/` | **Would delete it** — avoid on the production host |

It is **never** opened as `:memory:`. Override path only with another **disk** path:

```
Environment="BTS_SQLITE_STORE_PATH=/absolute/path/bts_store.db"
```

## What gets created

On first write/read the backend creates the file above under `Output/Status/`.

Tables (auto-created, schema v2):

| Table | Purpose |
|-------|---------|
| `upcoming_games` | All predicted fixtures (incl. unsettled) synced after pipeline upcoming steps |
| `past_games` | Settled prediction archive for `/api/past-games` |
| `live_score_history` | Finished live games with full stats; **never pruned** |
| `store_meta` | Schema / migration flags |

JSON dual-write (`past_games.json`, `live_score_history.json`) still runs for
compatibility. APIs prefer SQLite when it has rows.

## Backend setup (deploy host)

1. **Pull / deploy this branch** and restart the service (e.g. `systemctl restart beyond-the-stats`).
2. Confirm the process user can write `Output/Status/` (same as other status JSON files).
3. Optional override path:
   ```
   Environment="BTS_SQLITE_STORE_PATH=/absolute/path/bts_store.db"
   ```
4. No extra apt package is required for the app. Optional CLI for inspection:
   ```
   sudo apt-get install -y sqlite3
   sqlite3 Beyond-the-Stats/Output/Status/bts_store.db ".tables"
   sqlite3 Beyond-the-Stats/Output/Status/bts_store.db "SELECT COUNT(*) FROM upcoming_games;"
   sqlite3 Beyond-the-Stats/Output/Status/bts_store.db "SELECT COUNT(*) FROM live_score_history;"
   ```

## When rows are written

- **Pipeline start**: sync whatever prediction CSVs already exist (pre-run snapshot).
- **After sub-pipelines**: sync newly written upcoming CSVs (before settle/cups).
- **Pipeline end** (`finally`): always sync again — even if a later step failed —
  so SQLite has the freshest on-disk rows from this run.
- **When a live game ends**: poller upserts the finished game; after ESPN summary
  fetch it re-upserts with lineups / boxscore / key events / etc.
- **SQLite never deletes** history for date limits. JSON live history may still
  prune ~30 days; SQLite keeps everything.

## API parity

Pipeline sync uses the Website `_load_upcoming_rows` enrichment so
`upcoming_games` / `past_games` store the **same fields** returned by
`/api/upcoming` and `/api/past-games` (probs, markets, form, ratings,
display labels, actuals, etc.).

`live_score_history` stores the full live game object (same shape as
`/api/live-scores` games), including summary fields after full-time
(lineups, boxscore, key events, game_info, home/away stats).

`/api/past-games` and `/api/live-score-history` (`/api/past-live-scores`) read
from SQLite first and return those full payloads.

## Quick health check in Python

```python
from shared import sqlite_store
print(sqlite_store.store_info())
```
