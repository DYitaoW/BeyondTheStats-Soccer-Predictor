"""SQLite append/upsert store for past games, upcoming predictions, and live history.

JSON files remain as dual-write backups for deploy compatibility; APIs and
in-process readers prefer SQLite when rows exist (auto-migrating from JSON
on first open).

SQLite never deletes rows for date-window limits. Tables:

- ``upcoming_games`` — predicted fixtures synced from upcoming CSVs
- ``past_games`` — settled prediction archive
- ``live_score_history`` — finished live games with full stats
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from shared import paths as _paths
except Exception:  # pragma: no cover - script bootstrap fallback
    _paths = None  # type: ignore

_SCHEMA_VERSION = 2
_WRITE_LOCK = threading.RLock()

_PAST_TABLE = "past_games"
_LIVE_TABLE = "live_score_history"
_UPCOMING_TABLE = "upcoming_games"
_META_TABLE = "store_meta"


def _default_db_path() -> Path:
    if _paths is not None:
        return Path(_paths.SQLITE_STORE_FILE)
    base = Path(__file__).resolve().parent.parent
    return base / "Output" / "Status" / "bts_store.db"


def _past_games_json() -> Path:
    if _paths is not None:
        return Path(_paths.PAST_GAMES_FILE)
    return Path(__file__).resolve().parent.parent / "Output" / "Predictions" / "shared" / "past_games.json"


def _past_games_journal() -> Path:
    if _paths is not None:
        return Path(_paths.PAST_GAMES_JOURNAL_FILE)
    return (
        Path(__file__).resolve().parent.parent
        / "Output"
        / "Predictions"
        / "shared"
        / "past_games_journal.jsonl"
    )


def _live_history_json() -> Path:
    if _paths is not None:
        return Path(_paths.LIVE_SCORE_HISTORY_FILE)
    return Path(__file__).resolve().parent.parent / "Output" / "Status" / "live_score_history.json"


def _national_raw_matches_csv() -> Path:
    if _paths is not None:
        return Path(_paths.DATA_NATIONAL_DIR) / "national_team_recent_matches_raw.csv"
    return (
        Path(__file__).resolve().parent.parent
        / "Data"
        / "National_Team_Data"
        / "national_team_recent_matches_raw.csv"
    )


def _db_path() -> Path:
    """Return the on-disk SQLite path (never ``:memory:`` / RAM-only)."""
    override = os.environ.get("BTS_SQLITE_STORE_PATH", "").strip()
    raw = override if override else str(_default_db_path())
    lowered = raw.strip().lower()
    if (
        not raw
        or lowered in {":memory:", "file::memory:"}
        or lowered.startswith("file:mem")
        or "mode=memory" in lowered
    ):
        raise ValueError(
            f"BTS SQLite store must be an on-disk file path, not in-memory ({raw!r})"
        )
    path = Path(raw).expanduser()
    path = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a durable on-disk SQLite connection (WAL + full sync)."""
    path = Path(db_path) if db_path is not None else _db_path()
    if str(path) == ":memory:":
        raise ValueError("refusing to open in-memory SQLite store")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, uri=False)
    conn.row_factory = sqlite3.Row
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    if mode and str(mode[0]).lower() == "memory":
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=FILE")
    return conn


def _durable_commit(conn: sqlite3.Connection) -> None:
    """Commit and push WAL frames to the main ``.db`` file on disk."""
    conn.execute("COMMIT")
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.Error:
        pass


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_META_TABLE} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_PAST_TABLE} (
            storage_key TEXT PRIMARY KEY,
            match_date_iso TEXT,
            competition TEXT,
            home_team TEXT,
            away_team TEXT,
            prediction_key TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_past_games_date
        ON {_PAST_TABLE}(match_date_iso)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_past_games_comp
        ON {_PAST_TABLE}(competition)
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_LIVE_TABLE} (
            storage_key TEXT PRIMARY KEY,
            match_id TEXT,
            kickoff_utc TEXT,
            competition TEXT,
            home_team TEXT,
            away_team TEXT,
            game_date TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_live_hist_kickoff
        ON {_LIVE_TABLE}(kickoff_utc)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_live_hist_comp
        ON {_LIVE_TABLE}(competition)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_live_hist_date
        ON {_LIVE_TABLE}(game_date)
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_UPCOMING_TABLE} (
            storage_key TEXT PRIMARY KEY,
            match_date_iso TEXT,
            competition TEXT,
            home_team TEXT,
            away_team TEXT,
            prediction_key TEXT,
            source TEXT,
            status TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_upcoming_date
        ON {_UPCOMING_TABLE}(match_date_iso)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_upcoming_comp
        ON {_UPCOMING_TABLE}(competition)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_upcoming_status
        ON {_UPCOMING_TABLE}(status)
        """
    )
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {_META_TABLE}(key, value)
        VALUES ('schema_version', ?)
        """,
        (str(_SCHEMA_VERSION),),
    )


def _json_dumps(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":"))


def _json_loads(raw: str | None) -> Optional[dict]:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def past_game_storage_key(row: dict) -> str:
    """Stable dedupe key for past-games rows (prediction_key preferred)."""
    prediction_key = str(row.get("prediction_key", "") or "").strip()
    if prediction_key:
        return f"prediction:{prediction_key}"
    date_iso = _row_date_iso(row)
    competition = str(row.get("competition", "") or "").strip().lower()
    home = str(row.get("home_team", "") or "").strip().lower()
    away = str(row.get("away_team", "") or "").strip().lower()
    if date_iso and home and away:
        return f"fixture:{date_iso}|{competition}|{home}|{away}"
    return ""


def live_history_storage_key(row: dict) -> str:
    """Stable dedupe key for live-score history rows (match_id preferred)."""
    match_id = str(row.get("match_id", "") or "").strip()
    if match_id:
        return f"id:{match_id}"
    game_date = _row_date_iso(row)
    competition = str(row.get("competition", "") or "").strip().lower()
    home = str(row.get("home_team", "") or "").strip().lower()
    away = str(row.get("away_team", "") or "").strip().lower()
    if game_date and home and away:
        return f"fixture:{game_date}|{competition}|{home}|{away}"
    return ""


def _row_date_iso(row: dict) -> str:
    for field in (
        "match_date_iso",
        "match_date",
        "game_date",
        "scoreboard_date",
        "kickoff_utc",
        "kickoff_et",
        "match_datetime_utc",
        "completed_at",
    ):
        raw = str(row.get(field, "") or "").strip()
        if not raw:
            continue
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            return raw[:10]
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return ""


def _open_ready(db_path: Optional[Path] = None) -> sqlite3.Connection:
    conn = _connect(db_path)
    _ensure_schema(conn)
    return conn


def migrate_json_archives(db_path: Optional[Path] = None, *, force: bool = False) -> dict:
    """One-time (or forced) import of existing JSON archives into SQLite."""
    with _WRITE_LOCK:
        conn = _open_ready(db_path)
        try:
            flag = conn.execute(
                f"SELECT value FROM {_META_TABLE} WHERE key = ?",
                ("json_migrated",),
            ).fetchone()
            if flag and flag["value"] == "1" and not force:
                return {"past_games": 0, "live_score_history": 0, "skipped": True}

            past_count = 0
            live_count = 0
            past_file = _past_games_json()
            if past_file.is_file():
                try:
                    payload = json.loads(past_file.read_text(encoding="utf-8-sig"))
                    if isinstance(payload, list):
                        past_count = _upsert_past_games_conn(
                            conn, [r for r in payload if isinstance(r, dict)]
                        )["upserted"]
                except Exception as exc:
                    print(f"[sqlite-store] past_games.json migrate skipped: {exc}")

            live_file = _live_history_json()
            if live_file.is_file():
                try:
                    payload = json.loads(live_file.read_text(encoding="utf-8"))
                    if isinstance(payload, list):
                        live_count = _upsert_live_history_conn(
                            conn, [r for r in payload if isinstance(r, dict)]
                        )["upserted"]
                except Exception as exc:
                    print(f"[sqlite-store] live_score_history.json migrate skipped: {exc}")

            journal = _past_games_journal()
            if journal.is_file():
                try:
                    journal_rows = []
                    with journal.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                row = json.loads(line)
                            except Exception:
                                continue
                            if isinstance(row, dict):
                                journal_rows.append(row)
                    if journal_rows:
                        past_count += _upsert_past_games_conn(conn, journal_rows)["upserted"]
                except Exception as exc:
                    print(f"[sqlite-store] past_games journal migrate skipped: {exc}")

            conn.execute(
                f"INSERT OR REPLACE INTO {_META_TABLE}(key, value) VALUES (?, ?)",
                ("json_migrated", "1"),
            )
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
            return {
                "past_games": past_count,
                "live_score_history": live_count,
                "skipped": False,
            }
        finally:
            conn.close()


def _national_row_from_csv(raw: dict) -> Optional[dict]:
    """Normalize one national training CSV row into a past_games payload."""
    if not isinstance(raw, dict):
        return None
    row = {str(key): (value.strip() if isinstance(value, str) else value) for key, value in raw.items()}
    home = str(row.get("home_team") or "").strip()
    away = str(row.get("away_team") or "").strip()
    if not home or not away:
        return None
    competition = str(row.get("competition") or "").strip()
    match_date = str(row.get("match_date") or row.get("match_datetime_utc") or "").strip()
    date_iso = ""
    if len(match_date) >= 10 and match_date[4] == "-" and match_date[7] == "-":
        date_iso = match_date[:10]
    pair = sorted([home.lower(), away.lower()])
    prediction_key = f"{date_iso or match_date[:10]}|{competition}|{pair[0]}|{pair[1]}"
    payload = dict(row)
    payload["home_team"] = home
    payload["away_team"] = away
    payload["competition"] = competition
    payload["prediction_key"] = prediction_key
    payload["archive_source"] = "national_training"
    if date_iso:
        payload["match_date_iso"] = date_iso
        payload["match_date"] = date_iso
    for src, dest in (
        ("FTHG", "actual_home_goals"),
        ("FTAG", "actual_away_goals"),
        ("FTR", "actual_result"),
    ):
        if payload.get(src) not in (None, "") and payload.get(dest) in (None, ""):
            payload[dest] = payload.get(src)
    return payload


def migrate_national_training_archive(db_path: Optional[Path] = None, *, force: bool = False) -> dict:
    """One-time (or forced) import of national_team_recent_matches_raw.csv into past_games."""
    with _WRITE_LOCK:
        conn = _open_ready(db_path)
        try:
            flag = conn.execute(
                f"SELECT value FROM {_META_TABLE} WHERE key = ?",
                ("national_training_migrated",),
            ).fetchone()
            if flag and flag["value"] == "1" and not force:
                return {"past_games": 0, "skipped": True}

            csv_path = _national_raw_matches_csv()
            upserted = 0
            if csv_path.is_file():
                try:
                    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                        reader = csv.DictReader(handle)
                        rows = []
                        for raw in reader:
                            payload = _national_row_from_csv(raw)
                            if payload:
                                rows.append(payload)
                        if rows:
                            upserted = _upsert_past_games_conn(conn, rows)["upserted"]
                except Exception as exc:
                    print(f"[sqlite-store] national training CSV migrate skipped: {exc}")

            conn.execute(
                f"INSERT OR REPLACE INTO {_META_TABLE}(key, value) VALUES (?, ?)",
                ("national_training_migrated", "1"),
            )
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
            return {"past_games": upserted, "skipped": False, "source": str(csv_path)}
        finally:
            conn.close()


def ensure_store(db_path: Optional[Path] = None) -> Path:
    """Ensure on-disk DB exists, schema is ready, and archives have been migrated once."""
    path = Path(db_path) if db_path is not None else _db_path()
    if str(path) == ":memory:":
        raise ValueError("refusing to use in-memory SQLite store")
    with _WRITE_LOCK:
        conn = _open_ready(path)
        conn.close()
    try:
        migrate_json_archives(path)
    except Exception as exc:
        print(f"[sqlite-store] migrate skipped: {exc}")
    try:
        migrate_national_training_archive(path)
    except Exception as exc:
        print(f"[sqlite-store] national training migrate skipped: {exc}")
    return path


def _upsert_past_games_conn(conn: sqlite3.Connection, rows: Iterable[dict]) -> dict:
    upserted = 0
    skipped = 0
    now = _utc_now()
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        key = past_game_storage_key(row)
        if not key:
            skipped += 1
            continue
        date_iso = _row_date_iso(row) or None
        if date_iso and not row.get("match_date_iso"):
            row = dict(row)
            row["match_date_iso"] = date_iso
        conn.execute(
            f"""
            INSERT INTO {_PAST_TABLE}(
                storage_key, match_date_iso, competition, home_team, away_team,
                prediction_key, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(storage_key) DO UPDATE SET
                match_date_iso = excluded.match_date_iso,
                competition = excluded.competition,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                prediction_key = excluded.prediction_key,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                key,
                date_iso,
                str(row.get("competition", "") or "").strip() or None,
                str(row.get("home_team", "") or "").strip() or None,
                str(row.get("away_team", "") or "").strip() or None,
                str(row.get("prediction_key", "") or "").strip() or None,
                _json_dumps(row),
                now,
            ),
        )
        upserted += 1
    return {"upserted": upserted, "skipped": skipped}


def upsert_past_games(rows: Iterable[dict], db_path: Optional[Path] = None) -> dict:
    """Append/update past-games rows. Safe under concurrent writers."""
    rows = list(rows or [])
    if not rows:
        return {"upserted": 0, "skipped": 0}
    with _WRITE_LOCK:
        conn = _open_ready(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _upsert_past_games_conn(conn, rows)
            _durable_commit(conn)
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _deep_merge_payload(prior: dict, incoming: dict) -> dict:
    """Merge live-score payloads without dropping richer nested fields."""
    out = dict(prior or {})
    for key, value in (incoming or {}).items():
        if value in (None, ""):
            continue
        existing = out.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            out[key] = _deep_merge_payload(existing, value)
        elif isinstance(value, list):
            # Prefer non-empty newer lists (key_events, lineups lists, etc.).
            if value:
                out[key] = value
            elif existing in (None, [], ""):
                out[key] = value
        else:
            out[key] = value
    return out


def _upsert_live_history_conn(conn: sqlite3.Connection, rows: Iterable[dict]) -> dict:
    upserted = 0
    skipped = 0
    now = _utc_now()
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        key = live_history_storage_key(row)
        if not key:
            # Keep keyless rows under a synthetic key so they are not dropped.
            key = f"keyless:{now}:{upserted + skipped}:{id(row)}"
        existing = conn.execute(
            f"SELECT payload_json FROM {_LIVE_TABLE} WHERE storage_key = ?",
            (key,),
        ).fetchone()
        if existing:
            prior = _json_loads(existing["payload_json"]) or {}
            row = _deep_merge_payload(prior, row)
        game_date = _row_date_iso(row) or None
        conn.execute(
            f"""
            INSERT INTO {_LIVE_TABLE}(
                storage_key, match_id, kickoff_utc, competition, home_team,
                away_team, game_date, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(storage_key) DO UPDATE SET
                match_id = excluded.match_id,
                kickoff_utc = COALESCE(excluded.kickoff_utc, {_LIVE_TABLE}.kickoff_utc),
                competition = COALESCE(excluded.competition, {_LIVE_TABLE}.competition),
                home_team = COALESCE(excluded.home_team, {_LIVE_TABLE}.home_team),
                away_team = COALESCE(excluded.away_team, {_LIVE_TABLE}.away_team),
                game_date = COALESCE(excluded.game_date, {_LIVE_TABLE}.game_date),
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                key,
                str(row.get("match_id", "") or "").strip() or None,
                str(row.get("kickoff_utc", "") or row.get("match_datetime_utc", "") or "").strip()
                or None,
                str(row.get("competition", "") or "").strip() or None,
                str(row.get("home_team", "") or "").strip() or None,
                str(row.get("away_team", "") or "").strip() or None,
                game_date,
                _json_dumps(row),
                now,
            ),
        )
        upserted += 1
    return {"upserted": upserted, "skipped": skipped}


def upsert_live_score_history(rows: Iterable[dict], db_path: Optional[Path] = None) -> dict:
    """Append/update live-score history rows. Never hard-prunes by default."""
    rows = list(rows or [])
    if not rows:
        return {"upserted": 0, "skipped": 0}
    with _WRITE_LOCK:
        conn = _open_ready(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _upsert_live_history_conn(conn, rows)
            _durable_commit(conn)
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _decode_rows(cursor_rows) -> list[dict]:
    out: list[dict] = []
    for row in cursor_rows:
        payload = _json_loads(row["payload_json"])
        if payload is None:
            continue
        match_id = str(payload.get("match_id", "")).strip().lower()
        if match_id.startswith("test-") or "test-past-games" in match_id:
            continue
        out.append(payload)
    return out


def load_past_games(
    *,
    competition_substr: str = "",
    from_date: str = "",
    to_date: str = "",
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return past-games payloads, newest first."""
    ensure_store(db_path)
    conn = _open_ready(db_path)
    try:
        clauses = []
        params: list[Any] = []
        if competition_substr:
            clauses.append("LOWER(COALESCE(competition, '')) LIKE ?")
            params.append(f"%{competition_substr.lower()}%")
        if from_date:
            clauses.append("COALESCE(match_date_iso, '') >= ?")
            params.append(from_date[:10])
        if to_date:
            clauses.append("COALESCE(match_date_iso, '') <= ?")
            params.append(to_date[:10])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT payload_json FROM {_PAST_TABLE}
            {where}
            ORDER BY COALESCE(match_date_iso, '') DESC, updated_at DESC
            """,
            params,
        ).fetchall()
        return _decode_rows(rows)
    finally:
        conn.close()


def load_live_score_history(
    *,
    competition_substr: str = "",
    from_date: str = "",
    to_date: str = "",
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return live-score history payloads, newest kickoff first."""
    ensure_store(db_path)
    conn = _open_ready(db_path)
    try:
        clauses = []
        params: list[Any] = []
        if competition_substr:
            clauses.append("LOWER(COALESCE(competition, '')) LIKE ?")
            params.append(f"%{competition_substr.lower()}%")
        if from_date:
            clauses.append(
                "COALESCE(game_date, substr(COALESCE(kickoff_utc, ''), 1, 10), '') >= ?"
            )
            params.append(from_date[:10])
        if to_date:
            clauses.append(
                "COALESCE(game_date, substr(COALESCE(kickoff_utc, ''), 1, 10), '') <= ?"
            )
            params.append(to_date[:10])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT payload_json FROM {_LIVE_TABLE}
            {where}
            ORDER BY COALESCE(kickoff_utc, game_date, '') DESC, updated_at DESC
            """,
            params,
        ).fetchall()
        return _decode_rows(rows)
    finally:
        conn.close()


def count_rows(table: str, db_path: Optional[Path] = None) -> int:
    if table not in {_PAST_TABLE, _LIVE_TABLE, _UPCOMING_TABLE}:
        raise ValueError(f"unknown table: {table}")
    ensure_store(db_path)
    conn = _open_ready(db_path)
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"] if row else 0)
    finally:
        conn.close()


def _row_status(row: dict) -> str:
    actual = str(row.get("actual_result", "") or "").strip().upper()
    if actual in {"H", "D", "A"}:
        return "settled"
    return "upcoming"


def _sanitize_csv_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        import math

        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
    except Exception:
        pass
    try:
        import pandas as pd

        if isinstance(value, pd.Timestamp):
            return None if pd.isna(value) else value.isoformat()
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
    except Exception:
        pass
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def _frame_rows_to_dicts(frame) -> list[dict]:
    rows: list[dict] = []
    for _, series in frame.iterrows():
        row = {str(k): _sanitize_csv_value(series[k]) for k in frame.columns}
        rows.append(row)
    return rows


def _upsert_upcoming_conn(conn: sqlite3.Connection, rows: Iterable[dict], source: str = "") -> dict:
    upserted = 0
    skipped = 0
    now = _utc_now()
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        payload = dict(row)
        if source and not payload.get("source"):
            payload["source"] = source
        key = past_game_storage_key(payload)
        if not key:
            skipped += 1
            continue
        date_iso = _row_date_iso(payload) or None
        if date_iso and not payload.get("match_date_iso"):
            payload["match_date_iso"] = date_iso
        status = _row_status(payload)
        payload["status"] = status
        conn.execute(
            f"""
            INSERT INTO {_UPCOMING_TABLE}(
                storage_key, match_date_iso, competition, home_team, away_team,
                prediction_key, source, status, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(storage_key) DO UPDATE SET
                match_date_iso = excluded.match_date_iso,
                competition = excluded.competition,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                prediction_key = excluded.prediction_key,
                source = COALESCE(excluded.source, {_UPCOMING_TABLE}.source),
                status = excluded.status,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                key,
                date_iso,
                str(payload.get("competition", "") or "").strip() or None,
                str(payload.get("home_team", "") or "").strip() or None,
                str(payload.get("away_team", "") or "").strip() or None,
                str(payload.get("prediction_key", "") or "").strip() or None,
                str(payload.get("source", "") or source or "").strip() or None,
                status,
                _json_dumps(payload),
                now,
            ),
        )
        upserted += 1
        # Settled rows also land in past_games so /api/past-games has them.
        if status == "settled":
            _upsert_past_games_conn(conn, [payload])
    return {"upserted": upserted, "skipped": skipped}


def upsert_upcoming_games(
    rows: Iterable[dict],
    *,
    source: str = "",
    db_path: Optional[Path] = None,
) -> dict:
    """Append/update predicted upcoming (and settled) fixture rows."""
    rows = list(rows or [])
    if not rows:
        return {"upserted": 0, "skipped": 0}
    with _WRITE_LOCK:
        conn = _open_ready(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _upsert_upcoming_conn(conn, rows, source=source)
            _durable_commit(conn)
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def load_upcoming_games(
    *,
    competition_substr: str = "",
    status: str = "",
    from_date: str = "",
    to_date: str = "",
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return upcoming/predicted game payloads, soonest first."""
    ensure_store(db_path)
    conn = _open_ready(db_path)
    try:
        clauses = []
        params: list[Any] = []
        if competition_substr:
            clauses.append("LOWER(COALESCE(competition, '')) LIKE ?")
            params.append(f"%{competition_substr.lower()}%")
        if status:
            clauses.append("LOWER(COALESCE(status, '')) = ?")
            params.append(status.lower())
        if from_date:
            clauses.append("COALESCE(match_date_iso, '') >= ?")
            params.append(from_date[:10])
        if to_date:
            clauses.append("COALESCE(match_date_iso, '') <= ?")
            params.append(to_date[:10])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT payload_json FROM {_UPCOMING_TABLE}
            {where}
            ORDER BY COALESCE(match_date_iso, '') ASC, updated_at DESC
            """,
            params,
        ).fetchall()
        return _decode_rows(rows)
    finally:
        conn.close()


def load_upcoming_fixtures_dataframe(
    competitions: Optional[Iterable[str]] = None,
    reference_date: Optional[Any] = None,
    window_days: Optional[int] = None,
    db_path: Optional[Path] = None,
):
    """Return future unplayed fixtures from SQLite as a DataFrame for prediction fallback.

    Strictly read-only: never deletes, drops, or mutates any rows in SQLite.
    Maps storage payloads to the standard fixture schema:
        ['match_date', 'match_datetime_et', 'match_datetime_utc', 'competition', 'home_team', 'away_team']
    """
    try:
        import pandas as pd
    except ImportError:
        return None

    ref = pd.Timestamp(
        reference_date
        if reference_date is not None
        else datetime.now(timezone.utc).date()
    ).normalize()
    from_date_str = ref.strftime("%Y-%m-%d")
    to_date_str = ""
    if window_days is not None and int(window_days) > 0:
        end_ref = ref + pd.Timedelta(days=int(window_days))
        to_date_str = end_ref.strftime("%Y-%m-%d")

    target_comps = set(str(c).strip() for c in (competitions or []) if str(c).strip())

    # 1) Try uncompleted games (status != 'settled') from today onward.
    all_upcoming = load_upcoming_games(
        status="upcoming",
        from_date=from_date_str,
        to_date=to_date_str,
        db_path=db_path,
    )
    if not all_upcoming:
        # Fallback to querying without status filter in case status was empty
        all_upcoming = load_upcoming_games(
            from_date=from_date_str,
            to_date=to_date_str,
            db_path=db_path,
        )

    rows = []
    seen = set()
    for item in all_upcoming:
        if not isinstance(item, dict):
            continue
        actual = str(item.get("actual_result", "") or "").strip().upper()
        if actual in {"H", "D", "A"}:
            continue
        comp = str(item.get("competition", "") or "").strip()
        if target_comps and comp not in target_comps:
            continue
        match_date = pd.to_datetime(
            item.get("match_date") or item.get("match_date_iso"), errors="coerce"
        )
        if pd.isna(match_date):
            continue
        match_date = match_date.normalize()
        if match_date < ref:
            continue

        home_team = str(item.get("home_team", "") or "").strip()
        away_team = str(item.get("away_team", "") or "").strip()
        if not home_team or not away_team:
            continue

        key = (comp, match_date.strftime("%Y-%m-%d"), home_team, away_team)
        if key in seen:
            continue
        seen.add(key)

        dt_utc = str(
            item.get("match_datetime_utc", "") or item.get("kickoff_utc", "") or ""
        ).strip()
        dt_et = str(item.get("match_datetime_et", "") or "").strip()
        if not dt_utc and match_date is not None:
            dt_utc = match_date.isoformat()
        if not dt_et and match_date is not None:
            dt_et = match_date.isoformat()

        rows.append(
            {
                "match_date": match_date,
                "match_datetime_et": dt_et,
                "match_datetime_utc": dt_utc,
                "competition": comp,
                "home_team": home_team,
                "away_team": away_team,
            }
        )

    cols = [
        "match_date",
        "match_datetime_et",
        "match_datetime_utc",
        "competition",
        "home_team",
        "away_team",
    ]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values(
        ["match_date", "competition", "home_team", "away_team"]
    ).reset_index(drop=True)


def _default_upcoming_sources() -> list[tuple[str, Path]]:
    if _paths is None:
        base = Path(__file__).resolve().parent.parent / "Output" / "Predictions"
        return [
            ("global", base / "europe" / "upcoming_matchweek_predictions.csv"),
            ("mls", base / "mls" / "upcoming_matchweek_predictions.csv"),
            ("extra", base / "extra" / "upcoming_matchweek_predictions.csv"),
            ("cups", base / "cups" / "upcoming_cup_predictions.csv"),
            ("national", base / "national" / "upcoming_national_team_predictions.csv"),
            ("friendlies", base / "friendlies" / "upcoming_club_friendlies.csv"),
        ]
    return [
        ("global", Path(_paths.GLOBAL_UPCOMING_FILE)),
        ("mls", Path(_paths.MLS_UPCOMING_FILE)),
        ("extra", Path(_paths.EXTRA_UPCOMING_FILE)),
        ("cups", Path(_paths.CUP_UPCOMING_FILE)),
        ("national", Path(_paths.NATIONAL_UPCOMING_FILE)),
        ("friendlies", Path(_paths.FRIENDLIES_UPCOMING_FILE)),
    ]


def sync_upcoming_predictions_from_csvs(
    sources: Optional[Iterable[tuple[str, Path | str]]] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Push all predicted upcoming CSV rows into SQLite (backup + continuity).

    Called from the pipeline after upcoming prediction steps so the DB holds
    fixtures even if later settle/live steps fail.
    """
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pandas is required to sync upcoming CSVs") from exc

    pairs = list(sources) if sources is not None else _default_upcoming_sources()
    totals = {"files": 0, "upserted": 0, "skipped": 0, "missing": 0, "by_source": {}}
    ensure_store(db_path)

    for source, path in pairs:
        csv_path = Path(path)
        if not csv_path.is_file():
            totals["missing"] += 1
            totals["by_source"][source] = {"upserted": 0, "skipped": 0, "missing": True}
            continue
        try:
            frame = pd.read_csv(csv_path)
        except Exception as exc:
            print(f"[sqlite-store] failed reading {csv_path}: {exc}")
            totals["by_source"][source] = {"upserted": 0, "skipped": 0, "error": str(exc)}
            continue
        if frame is None or frame.empty:
            totals["by_source"][source] = {"upserted": 0, "skipped": 0, "empty": True}
            continue
        rows = _frame_rows_to_dicts(frame)
        result = upsert_upcoming_games(rows, source=source, db_path=db_path)
        totals["files"] += 1
        totals["upserted"] += result["upserted"]
        totals["skipped"] += result["skipped"]
        totals["by_source"][source] = {
            "upserted": result["upserted"],
            "skipped": result["skipped"],
            "rows": len(rows),
        }
    return totals


def replace_live_score_history_snapshot(
    rows: Iterable[dict], db_path: Optional[Path] = None
) -> dict:
    """Upsert a live-history snapshot without deleting older SQLite rows.

    JSON may still prune to ~30 days; SQLite retains forever.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    with _WRITE_LOCK:
        conn = _open_ready(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not rows:
                _durable_commit(conn)
                return {"upserted": 0, "deleted": 0, "total": count_rows(_LIVE_TABLE, db_path)}
            before_keys = {
                r["storage_key"]
                for r in conn.execute(f"SELECT storage_key FROM {_LIVE_TABLE}").fetchall()
            }
            result = _upsert_live_history_conn(conn, rows)
            after_keys = {live_history_storage_key(r) for r in rows if live_history_storage_key(r)}
            _durable_commit(conn)
            return {
                "upserted": result["upserted"],
                "deleted": 0,
                "skipped": result["skipped"],
                "known_before": len(before_keys),
                "known_after": len(after_keys),
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def store_info(db_path: Optional[Path] = None) -> dict:
    """Return path + row counts for ops / health checks."""
    path = ensure_store(db_path)
    size = path.stat().st_size if path.is_file() else 0
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    return {
        "path": str(path),
        "exists": path.is_file(),
        "on_disk": path.is_file() and str(path) != ":memory:",
        "bytes": size,
        "wal_bytes": wal.stat().st_size if wal.is_file() else 0,
        "shm_present": shm.is_file(),
        "schema_version": _SCHEMA_VERSION,
        "tables": {
            _PAST_TABLE: count_rows(_PAST_TABLE, path),
            _LIVE_TABLE: count_rows(_LIVE_TABLE, path),
            _UPCOMING_TABLE: count_rows(_UPCOMING_TABLE, path),
        },
        "never_deletes": True,
        "persists_across_reboot": True,
        "gitignored": True,  # Output/** — survives git pull; not wiped by checkout
    }
