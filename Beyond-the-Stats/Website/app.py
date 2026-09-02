"""
Flask API server — the main web serving layer for Beyond the Stats.

Architecture
------------
This Flask application serves all REST API endpoints plus the static frontend.
Subsystem logic lives in dedicated modules; this file contains only:

- Flask app initialization
- Middleware (CORS, cache headers, before/after hooks)
- REST endpoint routes (@app.route decorators)
- Imports from modular subsystems
"""
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory

import config

if config.PROJECT_DIR not in sys.path:
    sys.path.insert(0, config.PROJECT_DIR)
import pipeline_log
from cache import _cache_clear_pattern, _cached_response
from rate_limit import check_rate_limit, client_identifier
from accuracy_tracker import (
    _build_persistent_accuracy_stats,
    _compute_accuracy_stats,
    _compute_league_accuracy_stats,
    _load_prediction_tracking,
    update_accuracy_history_files,
)
from espn_api import (
    _fetch_competition_schedule,
    _fetch_competition_scores,
    _fetch_competition_teams,
    _fetch_team_info,
    _ROSTER_CACHE,
)
from knockout import (
    _append_projected_cup_matches,
    _build_cup_knockout_payload,
    _build_knockout_framework,
    _build_knockout_wc_format,
    _compute_odds_bracket,
    _enrich_league_data_cup_fields,
    _enrich_tournament_payload,
    _gather_competition_cup_matches,
    _normalize_round_label,
)
from math_utils import _safe_float
from live_poller import (
    _effective_poller_date,
    _get_todays_competitions,
    _live_score_poller_loop,
    _live_scores,
    _live_scores_lock,
    start_live_score_poller,
)
from league_data import build_league_data_payload
from team_mappings import (
    build_app_teams_catalog_payload,
    build_predictor_teams_payload,
    build_unmapped_espn_payload,
)
from notifications import (
    _apns_notification_queue,
    _notifications,
    device_tokens,
    ios_device_tokens,
    send_live_activity_update,
    send_live_activity_end,
    start_apns_worker,
    subscribe_match,
    unsubscribe_match,
)
import notifications as live_activities
from predictions import (
    _enrich_json_past_row,
    _file_mtime_utc,
    _format_percent_value,
    _get_static_predictions,
    _is_placeholder_game,
    _invalidate_prediction_caches,
    _load_all_fixtures_by_competition,
    _load_context,
    _load_current_season_tables,
    _load_h2h_and_form,
    _load_json_payload,
    _load_last_data_refresh,
    _load_last_refresh,
    _load_projected_tables,
    _load_projected_competition_table,
    _build_winner_probability_payload,
    _build_mls_winners_odds_bundle,
    _normalize_mls_conference_tables,
    _build_past_game_prediction_lookup,
    _collect_live_past_game_rows,
    _merge_prediction_onto_past_row,
    _past_row_date_iso,
    _load_team_recent_matches,
    _load_teams_from_team_data,
    _load_upcoming_rows,
    _normalize_h2h_payload,
    _normalize_recent_form_payload,
    _predict,
    _run_full_pipeline_once,
    _save_last_data_refresh,
    _save_last_refresh,
    _should_run_startup_tasks,
    _to_float_or_none,
    _to_int,
    _utc_to_et,
    _valid_date_iso,
    _week_based_cutoff,
    _winner_label,
    get_context,
    get_last_pipeline_run,
    set_last_pipeline_run,
    pm_extra,
    pm_global,
    pm_mls,
    run_live_results_updater,
    uefa,
)
from standings import (
    _UEFA_COMPETITIONS,
    _build_fallback_standings,
    _clear_leaders_cache,
    _clear_standings_cache,
    _compute_standings_from_history,
    _fill_placeholder_tables,
    _get_or_fetch_leaders,
    _get_or_fetch_standings,
    _load_league_teams,
    _load_live_score_history,
    _real_tables,
    _real_tables_lock,
    _sanitize_real_standings,
)
from team_utils import _normalize_team_key, _team_name_for_db, _team_name_for_display, _to_float

# ── KeyError tracking (for user to diagnose missing mappings / keys) ──
from threading import Lock
_key_error_log: list[dict] = []
_key_error_log_lock = Lock()
_KEY_ERROR_LOG_MAX = 200


def _log_key_error(context: str, exc: KeyError):
    """Record a KeyError with context for the /api/key-errors endpoint."""
    entry = {
        "context": context,
        "key": str(exc.args[0]) if exc.args else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with _key_error_log_lock:
        _key_error_log.append(entry)
        if len(_key_error_log) > _KEY_ERROR_LOG_MAX:
            _key_error_log.pop(0)


app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # 1 MB request body limit

@app.errorhandler(KeyError)
def _handle_key_error(exc):
    """Log unhandled KeyErrors and return a 500 with error info."""
    _log_key_error("unhandled", exc)
    return jsonify({"error": f"KeyError: {exc}"}), 500


@app.after_request
def _add_cache_headers(response):
    """Attach a sensible Cache-Control header to every served response.

    The website re-fetches the same JSON + static assets on every page
    load because no headers were previously set. This handler adds modest
    browser + shared cache lifetimes so repeat visits are instant.
    """
    if request.path.startswith("/api/"):
        # JSON endpoints: short max-age + must-revalidate so the browser
        # revalidates on the next page load but can serve stale-while-
        # revalidate if the user navigates quickly back to the page.
        response.headers["Cache-Control"] = (
            f"private, max-age={config._API_CACHE_MAX_AGE}, must-revalidate"
        )
    elif request.path.startswith("/static/"):
        # Static JS / CSS / images. Filenames are stable between deploys;
        # version-bumping the URL is the cache-bust strategy. Override the
        # Flask default ("no-cache") so browsers actually cache them.
        response.headers["Cache-Control"] = (
            f"public, max-age={config._STATIC_CACHE_MAX_AGE}"
        )
    elif request.path.startswith("/graphics/"):
        response.headers["Cache-Control"] = (
            f"public, max-age={int(config._STATIC_CACHE_MAX_AGE * 24)}"
        )

    # Security headers
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-XSS-Protection", "0")
    # HSTS — only set when using HTTPS
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # CSP — strict but permissive enough for the legacy UI
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "img-src 'self' data: https:; "
        "font-src 'self' https://cdnjs.cloudflare.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'",
    )

    # CORS for the Cloudflare Pages frontend (and any future static origin).
    # Allow-list is read from ALLOWED_ORIGINS env var (comma-separated).
    origin = request.headers.get("Origin")
    if origin:
        allowed = config.ALLOWED_ORIGINS
        if origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Refresh-Token, X-Notifications-Key, X-Debug-Key"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"

    return response


@app.before_request
def _handle_cors_preflight():
    """Respond to CORS preflight (OPTIONS) requests immediately."""
    if request.method == "OPTIONS":
        # Build a minimal preflight response; after_request adds the
        # Access-Control-* headers based on the Origin.
        return ("", 204)


@app.before_request
def _enforce_api_rate_limits():
    """Per-IP rate limits for all ``/api/*`` calls (redeem is tighter)."""
    if request.method == "OPTIONS":
        return None
    path = request.path or ""
    if not path.startswith("/api/"):
        return None

    client_id = client_identifier()
    allowed, retry_after = check_rate_limit(
        "api",
        config.API_RATE_LIMIT_PER_MINUTE,
        60,
        client_id=client_id,
    )
    if not allowed:
        resp = jsonify({
            "ok": False,
            "error": "rate_limit_exceeded",
            "detail": f"Limit {config.API_RATE_LIMIT_PER_MINUTE} requests per minute",
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after or 60)
        return resp

    if path.rstrip("/") == "/api/redeem":
        allowed, retry_after = check_rate_limit(
            "redeem",
            config.REDEEM_RATE_LIMIT_PER_MINUTE,
            60,
            client_id=client_id,
        )
        if not allowed:
            resp = jsonify({
                "ok": False,
                "error": "rate_limit_exceeded",
                "detail": f"Redeem limit {config.REDEEM_RATE_LIMIT_PER_MINUTE} requests per minute",
            })
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry_after or 60)
            return resp
    return None



_UPCOMING_MODE_MAP = {
    "global": (config.GLOBAL_UPCOMING_FILE, "global"),
    "mls": (config.MLS_UPCOMING_FILE, "mls"),
    "extra": (config.EXTRA_UPCOMING_FILE, "extra"),
    "cups": (config.CUP_UPCOMING_FILE, "cups"),
    "world-cup": (config.NATIONAL_UPCOMING_FILE, "national"),
    "friendlies": (config.FRIENDLIES_UPCOMING_FILE, "friendlies"),
}

_ALL_UPCOMING_SOURCES = [
    ("global", config.GLOBAL_UPCOMING_FILE),
    ("global_projected", config.GLOBAL_PROJECTED_MATCHES_FILE),
    ("mls", config.MLS_UPCOMING_FILE),
    ("extra", config.EXTRA_UPCOMING_FILE),
    ("extra_projected", config.EXTRA_PROJECTED_MATCHES_FILE),
    ("cups", config.CUP_UPCOMING_FILE),
    ("national", config.NATIONAL_UPCOMING_FILE),
    ("friendlies", config.FRIENDLIES_UPCOMING_FILE),
]


def _merged_upcoming_file_is_fresh(merged_path):
    """True when ``merged_path`` exists and is at least as new as every source CSV.

    The API prefers ``Output/Upcoming/all_upcoming.csv`` for a single-file read,
    but that merge is only refreshed by ``publish_to_output()``. If publish was
    skipped or failed, the merged file can be days/weeks stale while the
    per-pipeline prediction CSVs are fresh — fall back to the sources instead.
    """
    if not merged_path or not os.path.exists(merged_path):
        return False
    try:
        merged_mtime = os.path.getmtime(merged_path)
    except OSError:
        return False
    for _, csv_path in _ALL_UPCOMING_SOURCES:
        if not csv_path or not os.path.exists(csv_path):
            continue
        try:
            if os.path.getmtime(csv_path) > merged_mtime + 1.0:
                return False
        except OSError:
            continue
    return True


def _regional_espn_schedule_fallback(existing_rows):
    """Add schedule-only MLS/Liga MX rows when the generated MLS CSV is absent/empty."""
    rows = list(existing_rows or [])
    present = {
        str(row.get("competition", "")).strip()
        for row in rows
        if str(row.get("competition", "")).strip()
    }
    for competition in ("United States/MLS", "Mexico/Liga MX"):
        if competition in present:
            continue
        espn_id = config.LIVE_SCORE_COMPETITIONS.get(competition)
        if not espn_id:
            continue
        try:
            games = _fetch_competition_schedule(competition, espn_id, days_forward=365) or []
        except Exception:
            games = []
        for game in games:
            if str(game.get("status", "")).strip().lower() not in ("", "pre"):
                continue
            match_date = str(game.get("match_date", "") or "").strip()
            home = _team_name_for_display(game.get("home_team", ""))
            away = _team_name_for_display(game.get("away_team", ""))
            if not match_date or not home or not away:
                continue
            rows.append({
                "match_id": str(game.get("match_id", "") or ""),
                "match_date": match_date,
                "match_date_iso": match_date,
                "match_datetime_et": str(game.get("kickoff_utc", "") or ""),
                "competition": competition,
                "home_team": home,
                "away_team": away,
                "schedule_only": True,
                "has_prediction": False,
                "prediction_quality": "no_prediction",
                "prediction_note": "Fixture available; prediction pending team/model mapping.",
                "live_updates": False,
                "live_status": "scheduled",
            })
    return rows


def _exclude_upcoming_only_rows(rows):
    """Drop competitions that have a dedicated upcoming source only."""
    blocked = config.UPCOMING_ONLY_COMPETITIONS | config.LEAGUE_API_EXCLUDED_COMPETITIONS
    return [r for r in rows if str(r.get("competition", "")).strip() not in blocked]


def _is_league_api_competition(comp_name):
    """Return True when a competition should appear in league-facing APIs."""
    comp = str(comp_name or "").strip()
    if not comp:
        return False
    if comp in config.UPCOMING_ONLY_COMPETITIONS:
        return False
    if comp in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
        return False
    return True


def _filter_league_tables_payload(data):
    """Remove fallback-only / upcoming-only leagues from table API payloads."""
    excluded = config.LEAGUE_API_EXCLUDED_COMPETITIONS
    leagues = [c for c in (data.get("leagues") or []) if c not in excluded]
    tables = {
        k: v for k, v in (data.get("tables") or {}).items()
        if k not in excluded
    }
    fixtures = data.get("fixtures")
    if isinstance(fixtures, dict):
        fixtures = {k: v for k, v in fixtures.items() if k not in excluded}
    return {**data, "leagues": leagues, "tables": tables, "fixtures": fixtures}


def _merge_projected_with_season_tables(projected: dict, season_data: dict | None, dataset_competitions: tuple[str, ...]) -> dict:
    """Prefer projected CSV rows; fill gaps from season rosters and placeholders."""
    tables = dict(projected.get("tables") or {})
    leagues = set(projected.get("leagues") or [])
    if season_data:
        for comp, rows in (season_data.get("tables") or {}).items():
            if comp not in tables or not tables.get(comp):
                tables[comp] = rows
            leagues.add(comp)
    for comp in dataset_competitions:
        leagues.add(comp)
    data = {"leagues": sorted(leagues), "tables": tables}
    _fill_placeholder_tables(data)
    data["leagues"] = sorted(set(data.get("leagues") or []) | set(dataset_competitions))
    return _filter_league_tables_payload(data)


def _pick_league_winner_row(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            float(row.get("win_league_pct") or 0),
            -(int(row.get("position") or 999)),
        ),
    )


def _build_global_api_payload() -> dict:
    projected = _load_projected_tables(config.GLOBAL_PROJECTED_TABLE_FILE)
    season_data = _load_current_season_tables()
    return _merge_projected_with_season_tables(
        projected,
        season_data,
        config.GLOBAL_DATASET_COMPETITIONS,
    )


def _build_extra_api_payload() -> dict:
    projected = _load_projected_tables(config.EXTRA_PROJECTED_TABLE_FILE)
    season_data = _load_current_season_tables()
    return _merge_projected_with_season_tables(
        projected,
        season_data,
        config.EXTRA_DATASET_COMPETITIONS,
    )


def _build_home_league_sidebar_entries() -> list[dict]:
    priority = {
        name: index
        for index, name in enumerate(
            list(config.GLOBAL_DATASET_COMPETITIONS)
            + [config.MLS_COMPETITION, config.LIGA_MX_COMPETITION]
            + list(config.EXTRA_DATASET_COMPETITIONS)
        )
    }
    entries: list[dict] = []
    seen: set[str] = set()
    for dataset, builder in (
        ("global", _build_global_api_payload),
        ("mls", _build_mls_api_payload),
        ("extra", _build_extra_api_payload),
    ):
        payload = builder()
        tables = payload.get("tables") or {}
        for league, rows in tables.items():
            if league in config.HOME_SIDEBAR_SKIP_COMPETITIONS or league == "__mls_bracket__":
                continue
            if not rows or league in seen:
                continue
            winner = _pick_league_winner_row(rows)
            entries.append({
                "dataset": dataset,
                "league": league,
                "winner": (winner or {}).get("team") or "N/A",
                "win_pct": float((winner or {}).get("win_league_pct") or 0),
            })
            seen.add(league)
    entries.sort(
        key=lambda item: (
            priority.get(item["league"], 1000),
            item["league"],
        )
    )
    return entries


def _date_window_bounds():
    """Return the website's stored match window as ISO date bounds."""
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    # Full-season window: from the start of the prior week through one year ahead.
    season_end = today_et + timedelta(days=365)
    current_week_start = today_et - timedelta(days=today_et.weekday())
    return current_week_start - timedelta(days=7), season_end


def _parse_query_date(value, fallback):
    """Parse a YYYY-MM-DD query date, falling back when invalid."""
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return fallback


def _row_date_iso(row):
    """Return the normalized ISO match date used by website filters."""
    raw = str(row.get("match_date_iso") or row.get("match_date") or "").strip()
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    parsed = pd.to_datetime(raw, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%Y-%m-%d")


def _group_rows_by_league(rows):
    """Group matches by league, keeping games chronologically ordered."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("competition") or "Other"].append(row)
    groups = []
    for league, league_rows in grouped.items():
        league_rows.sort(key=lambda r: (
            _row_date_iso(r),
            str(r.get("match_datetime_et") or r.get("match_datetime_utc") or ""),
            str(r.get("home_team") or ""),
        ))
        groups.append({"league": league, "matches": league_rows})
    groups.sort(key=lambda group: (
        _row_date_iso(group["matches"][0]) if group["matches"] else "",
        group["league"],
    ))
    return groups


# ═══════════════════════════════════════════════════════════════════
#  ROUTES — HTML Pages
# ═══════════════════════════════════════════════════════════════════

@app.get("/")
def index():
    """Render the home page with shared team context."""
    return _render_site_page("home.html", active_page="home")


def _render_site_page(template_name, active_page):
    """Render a website tab page with shared team lists for forms and datalists."""
    # Shared route map used by template JS navigation helpers.
    page_routes = {
        "home": "/",
        "global": "/upcoming-matches",
        "leagues": "/leagues",
        "h2h": "/head-to-head",
        "world-cup": "/world-cup",
        "players": "/players",
        "tactics": "/tactics",
        "about": "/about",
        "privacy": "/privacy",
        "terms": "/terms",
        "subscriptions": "/subscriptions",
    }
    # Template defaults prevent Undefined errors for pages that serialize these values.
    upcoming_leagues = {"global": [], "mls": [], "extra": [], "cups": [], "friendlies": []}
    table_leagues = {"global": [], "mls": [], "extra": [], "cups": []}

    if config.STATIC_PREDICTIONS:
        _, global_teams = _get_static_predictions("global")
        _, mls_teams = _get_static_predictions("mls")
        _, extra_teams = _get_static_predictions("extra")
        if not global_teams:
            global_teams = set(_load_teams_from_team_data(pm_global))
        if not mls_teams:
            mls_teams = set(_load_teams_from_team_data(pm_mls))
        if not extra_teams:
            extra_teams = set(_load_teams_from_team_data(pm_extra))
        global_display_teams = sorted({_team_name_for_display(team) for team in global_teams})
        mls_display_teams = sorted({_team_name_for_display(team) for team in mls_teams})
        extra_display_teams = sorted({_team_name_for_display(team) for team in extra_teams})
    else:
        global_ctx = get_context("global")
        mls_ctx = get_context("mls")
        global_display_teams = sorted({_team_name_for_display(team) for team in global_ctx.available_teams})
        mls_display_teams = sorted({_team_name_for_display(team) for team in mls_ctx.available_teams})
        try:
            extra_ctx = get_context("extra")
            extra_display_teams = sorted({_team_name_for_display(team) for team in extra_ctx.available_teams})
        except Exception:
            extra_display_teams = sorted({_team_name_for_display(team) for team in _load_teams_from_team_data(pm_extra)})
    return render_template(
        template_name,
        # Active page keeps nav highlighting/panel state aligned per template.
        active_page=active_page,
        page_routes=page_routes,
        upcoming_leagues=upcoming_leagues,
        table_leagues=table_leagues,
        teams=global_display_teams,
        mls_teams=mls_display_teams,
        extra_teams=extra_display_teams,
    )


@app.get("/upcoming-matches")
def upcoming_matches():
    """Render the upcoming matches tab page."""
    return _render_site_page("upcoming_matches.html", active_page="global")


@app.get("/cups")
def cups_page():
    """Redirect the legacy cups tab to the merged Leagues page."""
    return redirect(url_for("leagues"))


@app.get("/leagues")
def leagues():
    """Render the merged Leagues page (projected tables + cups)."""
    return _render_site_page("leagues.html", active_page="leagues")


@app.get("/head-to-head")
def head_to_head():
    """Render the head-to-head tab page."""
    return _render_site_page("head_to_head.html", active_page="h2h")


@app.get("/league-tables")
def league_tables():
    """Redirect the legacy projected league tables page to the merged Leagues page."""
    return redirect(url_for("leagues"))


@app.get("/world-cup")
def world_cup():
    """Render the World Cup tab page."""
    return _render_site_page("world_cup.html", active_page="world-cup")


@app.get("/about")
def about():
    """Render the about tab page."""
    return _render_site_page("about.html", active_page="about")


@app.get("/privacy")
def privacy_page():
    """Render the Privacy Policy page."""
    from legal_docs import get_legal_document, plain_text_to_html_paragraphs

    doc = get_legal_document("privacy")
    if not doc:
        return "Privacy Policy unavailable", 404
    return render_template(
        "legal.html",
        active_page="privacy",
        doc=doc,
        body_html=plain_text_to_html_paragraphs(doc["body"]),
    )


@app.get("/terms")
def terms_page():
    """Render the Terms of Service page."""
    from legal_docs import get_legal_document, plain_text_to_html_paragraphs

    doc = get_legal_document("terms")
    if not doc:
        return "Terms of Service unavailable", 404
    return render_template(
        "legal.html",
        active_page="terms",
        doc=doc,
        body_html=plain_text_to_html_paragraphs(doc["body"]),
    )


@app.get("/subscriptions")
def subscriptions_page():
    """Render the Apple IAP auto-renewable subscription disclosure page."""
    from legal_docs import get_legal_document, plain_text_to_html_paragraphs

    doc = get_legal_document("subscriptions")
    if not doc:
        return "Subscription disclosure unavailable", 404
    return render_template(
        "legal.html",
        active_page="subscriptions",
        doc=doc,
        body_html=plain_text_to_html_paragraphs(doc["body"]),
    )


def _render_draftit_page(doc_id: str):
    """Render a Draft It! legal/info page using the Draft It! template."""
    from legal_docs import get_legal_document, plain_text_to_html_paragraphs

    doc = get_legal_document(doc_id)
    if not doc:
        return "Document unavailable", 404
    return render_template(
        "draftit.html",
        active_page="draftit",
        doc=doc,
        body_html=plain_text_to_html_paragraphs(doc["body"]),
    )


@app.get("/draftit/about")
def draftit_about():
    """Render the Draft It! app Privacy Policy / about page."""
    return _render_draftit_page("draftit_privacy")


@app.get("/draftit/privacy")
def draftit_privacy():
    """Render the Draft It! Privacy Policy page."""
    return _render_draftit_page("draftit_privacy")


# ═══════════════════════════════════════════════════════════════════
#  REST API ENDPOINTS — 32+ ``/api/*`` routes
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/teams")
def api_teams():
    """Return selectable teams for the requested prediction mode."""
    mode = str(request.args.get("mode", "global")).strip().lower()
    if mode not in {"global", "mls", "extra"}:
        mode = "global"
    if config.STATIC_PREDICTIONS:
        _, teams = _get_static_predictions(mode)
        if not teams:
            if mode == "mls":
                teams = _load_teams_from_team_data(pm_mls)
            elif mode == "extra":
                teams = _load_teams_from_team_data(pm_extra)
            else:
                teams = _load_teams_from_team_data(pm_global)
        display_teams = sorted({_team_name_for_display(team) for team in teams})
    else:
        try:
            teams = get_context(mode).available_teams
            display_teams = sorted({_team_name_for_display(team) for team in teams})
        except Exception:
            display_teams = []
    return jsonify({"teams": display_teams})


@app.get("/api/teams/catalog")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_teams_catalog():
    """Return all unique canonical teams for app-available leagues.

    Query params:
        competition — optional filter to one league/cup (e.g. ``England/Premier League``)
    """
    competition = str(request.args.get("competition", "")).strip() or None
    payload = build_app_teams_catalog_payload(competition_filter=competition)
    if not payload.get("ok"):
        return jsonify(payload), 404
    return jsonify(payload)


def _resolve_team_api_payload(team_input: str, mode: str):
    """Build the single-team API payload used by ``/api/team``."""
    mode = (mode or "global").strip().lower()
    if mode not in {"global", "mls", "extra"}:
        mode = "global"

    if mode == "mls":
        pm_mod = pm_mls
    elif mode == "extra":
        pm_mod = pm_extra
    else:
        pm_mod = pm_global

    head_to_head, current_form = _load_h2h_and_form(pm_mod)
    form_teams = current_form.get("teams", {}) if isinstance(current_form, dict) else {}
    h2h_root = head_to_head if isinstance(head_to_head, dict) else {}

    team = _team_name_for_db(team_input)
    team_lower = team.lower()

    # Prefer exact DB key, then case-insensitive match in form / h2h maps.
    form_key = team if team in form_teams else next(
        (name for name in form_teams if str(name).strip().lower() == team_lower),
        team,
    )
    h2h_key = team if team in h2h_root else next(
        (name for name in h2h_root if str(name).strip().lower() == team_lower),
        team,
    )

    team_form = _normalize_recent_form_payload(form_teams.get(form_key, {}))
    recent_matches = _load_team_recent_matches(team, pm_mod.PROCESSED_DIR, 10)

    all_h2h = {}
    for opponent, payload in (h2h_root.get(h2h_key) or {}).items():
        all_h2h[opponent] = _normalize_h2h_payload(payload)

    upcoming = []
    csv_path = config.UPCOMING_CSV_FILES.get(mode) or config.UPCOMING_CSV_FILES.get("global")
    if csv_path and os.path.exists(csv_path):
        try:
            frame = pd.read_csv(csv_path, dtype=str)
            for _, row in frame.iterrows():
                home = str(row.get("home_team", "") or "").strip()
                away = str(row.get("away_team", "") or "").strip()
                if team_lower not in (home.lower(), away.lower()):
                    continue
                upcoming.append({
                    "competition": str(row.get("competition", "") or "").strip(),
                    "match_date": str(row.get("match_date", "") or "").strip(),
                    "match_datetime_utc": _utc_to_et(str(row.get("match_datetime_utc", "") or "").strip()),
                    "home_team": home,
                    "away_team": away,
                    "predicted_result": str(row.get("predicted_result", "") or "").strip(),
                })
        except Exception:
            pass

    team_pred_data = {}
    tracking = _load_prediction_tracking()
    pt = tracking.get("per_team", {}) if isinstance(tracking, dict) else {}
    if isinstance(pt, dict):
        if team in pt:
            team_pred_data = pt[team]
        else:
            for tname, tdata in pt.items():
                if str(tname).strip().lower() == team_lower:
                    team_pred_data = tdata
                    break

    return {
        "ok": True,
        "team": team,
        "display_team": _team_name_for_display(team),
        "mode": mode,
        "form": team_form,
        "recent_matches": recent_matches,
        "upcoming_games": upcoming,
        "head_to_head": all_h2h,
        "prediction_accuracy": team_pred_data,
    }


@app.get("/api/team")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_team():
    """Return form, recent results, upcoming games, and H2H for one team.

    Query params:
        team  -- team name (required)
        mode  -- global / mls / extra (default: global)
    """
    team_input = request.args.get("team", "").strip()
    mode = request.args.get("mode", "global").strip().lower()
    if not team_input:
        return jsonify({"ok": False, "error": "Missing team"}), 400
    return jsonify(_resolve_team_api_payload(team_input, mode))


@app.get("/api/team/<path:team_name>")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_team_by_path(team_name):
    """Path-style alias for ``/api/team?team=...`` (e.g. ``/api/team/Arsenal``)."""
    mode = request.args.get("mode", "global").strip().lower()
    team_input = str(team_name or "").strip()
    if not team_input:
        return jsonify({"ok": False, "error": "Missing team"}), 400
    return jsonify(_resolve_team_api_payload(team_input, mode))


@app.get("/api/teams/<team_id>/roster")
def api_team_roster(team_id):
    """Return full roster and season stats for a team by ESPN team ID.

    Query params:
        competition  -- required, e.g. ``England/Premier League``
        refresh      -- if ``1``/``true``, bypass cache
    """
    comp = request.args.get("competition", "").strip()
    if not comp:
        return jsonify({"ok": False, "error": "Missing 'competition' query parameter"}), 400
    if comp not in config.LIVE_SCORE_COMPETITIONS:
        return jsonify({"ok": False, "error": f"Unknown competition: {comp}"}), 400
    espn_id = config.LIVE_SCORE_COMPETITIONS[comp]
    if request.args.get("refresh", "").strip().lower() in ("1", "true"):
        _ROSTER_CACHE.pop(f"roster_{comp}_{team_id}", None)
    info = _fetch_team_info(comp, espn_id, team_id)
    if info is None:
        return jsonify({"ok": False, "error": f"Could not fetch roster for team {team_id}"}), 502
    return jsonify({"ok": True, "team": info})


@app.get("/api/legal")
def api_legal_index():
    """List Privacy Policy, Terms of Service, and subscription disclosure endpoints."""
    from legal_docs import list_legal_documents

    return jsonify({"ok": True, "documents": list_legal_documents()})


@app.get("/api/legal/<doc_id>")
def api_legal_document(doc_id):
    """Return a legal document body (privacy, terms, or subscriptions)."""
    from legal_docs import get_legal_document

    doc = get_legal_document(doc_id)
    if not doc:
        return jsonify({"ok": False, "error": f"Unknown legal document: {doc_id}"}), 404
    return jsonify(doc)


@app.get("/api/team-mappings/unmapped")
def api_team_mappings_unmapped():
    """List ESPN / API upcoming team names missing or blank in the mapping master."""
    raw_lookahead = request.args.get("lookahead_days")
    competition = str(request.args.get("competition", "")).strip() or None
    lookahead_days = None
    if raw_lookahead is not None and str(raw_lookahead).strip() != "":
        try:
            lookahead_days = int(raw_lookahead)
        except (TypeError, ValueError):
            lookahead_days = None
    return jsonify(
        build_unmapped_espn_payload(
            lookahead_days=lookahead_days,
            competition_filter=competition,
        )
    )


@app.get("/api/team-mappings/predictor-teams")
def api_team_mappings_predictor_teams():
    """List canonical team names used by global, MLS, and extra predictors."""
    return jsonify(build_predictor_teams_payload())


# ── Simple mutation auth ──────────────────────────────────────────────

def _mutation_authorized():
    """Return True if the caller has a valid admin token."""
    token = request.headers.get("X-Admin-Token", "").strip()
    if not token:
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return bool(token and config.MUTATION_API_TOKEN and token == config.MUTATION_API_TOKEN)


@app.get("/api/help")
def api_help():
    """Return a listing of every /api/ route with a short description."""
    routes = [
        ("/api/teams", "GET", "List selectable teams for a given mode (?mode=global|mls|extra)"),
        ("/api/teams/catalog", "GET", "All unique teams in app-available leagues (?competition=)"),
        ("/api/team", "GET", "Single-team form, recent matches, upcoming, H2H (?team=&mode=)"),
        ("/api/team/<team>", "GET", "Path-style single-team lookup (same payload as /api/team)"),
        ("/api/teams/<team_id>/roster", "GET", "ESPN roster/stats for a team (?competition=)"),
        ("/api/legal", "GET", "List Privacy Policy, Terms of Service, and subscription disclosure endpoints"),
        ("/api/legal/privacy", "GET", "Full Privacy Policy text (JSON)"),
        ("/api/legal/terms", "GET", "Full Terms of Service text (JSON)"),
        ("/api/legal/subscriptions", "GET", "Apple IAP auto-renewable subscription disclosure (JSON)"),
        ("/api/team-mappings/unmapped", "GET", "ESPN upcoming teams missing/blank in mapping master (?lookahead_days=30&competition=)"),
        ("/api/team-mappings/predictor-teams", "GET", "All canonical predictor team names (global/mls/extra) with duplicate detection"),
        ("/api/upcoming/<mode>", "GET", "Upcoming prediction rows (mode=global|mls|extra|cups|world-cup)"),
        ("/api/past-games", "GET", "Completed games with predictions, optional ?competition= filter"),
        ("/api/world-cup", "GET", "World Cup standings + knockout brackets (odds + real)"),
        ("/api/cup-bracket", "GET", "Domestic cup projected brackets (?competition=)"),
        ("/api/real-cup-data", "GET", "Domestic cup real-life brackets (?competition=)"),
        ("/api/competition-data", "GET", "Unified WC-format data for any competition (?competition=)"),
        ("/api/league-tables", "GET", "Projected league tables for all competitions"),
        ("/api/real-tables", "GET", "Live/recent real league standings"),
        ("/api/league-leaders", "GET", "Predicted winner + current leader per competition"),
        ("/api/live-scores", "GET", "Currently live matches with scores"),
        ("/api/live-score-history", "GET", "Recent live score history"),
        ("/api/h2h", "GET", "Head-to-head stats between two teams (?home=&away=)"),
        ("/api/scorers", "GET", "Top scorers data"),
        ("/api/stats", "GET", "Aggregate prediction statistics"),
        ("/api/predict", "POST", "Predict a single matchup (? JSON: home_team, away_team, mode)"),
        ("/api/predict/mls", "POST", "Predict a single MLS matchup"),
        ("/api/predict/extra", "POST", "Predict a single extra-league matchup"),
        ("/api/pipeline/status", "GET", "Pipeline health: step pass/fail + last refresh"),
        ("/api/pipeline/logs", "GET", "Pipeline terminal output (tail, WARN/ERROR filters)"),
        ("/api/refresh", "POST", "Trigger background pipeline refresh (light, no model retrain)"),
        ("/api/retrain", "POST", "Force full model retrain (Tue/Fri-style, all pipelines)"),
        ("/api/mobile/widget", "GET", "Mobile widget data"),
        ("/api/debug/live-score-sources", "GET", "Debug: show live score source files"),
        ("/api/debug/manual-poll", "GET", "Debug: trigger manual live-score poll"),
        ("/api/debug/poller-state", "GET", "Debug: show poller state"),
        ("/api/debug/poller-state", "GET", "Debug: show poller state"),
        ("/api/notifications", "POST", "Queue a push notification"),
        ("/api/notifications", "GET", "List recent notifications"),
        ("/api/notifications/register", "POST", "Register a device push token"),
        ("/api/notifications/unregister", "POST", "Remove a device push token"),
        ("/api/notifications/subscribe", "POST", "Subscribe a device to a match's live-event alerts"),
        ("/api/notifications/unsubscribe", "POST", "Unsubscribe a device from a match's live-event alerts"),
        ("/api/live-activities/register", "POST", "Register a Live Activity push token for a match"),
        ("/api/live-activities/unregister", "POST", "Remove a Live Activity registration"),
        ("/api/live-activities/update", "POST", "Push a content-state update to Live Activities for a match"),
        ("/api/live-activities/end", "POST", "End/dismiss Live Activities for a match"),
        ("/api/redeem", "GET/POST", "Redeem a promo code (?code= or JSON body)"),
        ("/api/info/changes", "GET", "App changes changelog entries"),
        ("/api/info/roadmap", "GET", "App planned features/roadmap"),
        ("/api/info/upcoming", "GET", "App upcoming features"),
        ("/api/help", "GET", "This listing"),
    ]
    return jsonify({"ok": True, "routes": routes})


@app.get("/api/help/all")
def api_help_all():
    """Return every competition with its available API calls (predicted + real)."""
    now_str = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    leagues_list = []
    cups_list = []

    for comp_name in sorted(config.LIVE_SCORE_COMPETITIONS, key=str.lower):
        if comp_name in config.UPCOMING_ONLY_COMPETITIONS:
            continue
        if comp_name in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
            continue
        is_cup = comp_name in config._CUP_FORMATS
        base = {
            "competition": comp_name,
            "is_cup": is_cup,
        }

        # ── Predicted data ──────────────────────────────────────
        predicted = {
            "upcoming": f"/api/upcoming/{'world-cup' if 'World Cup' in comp_name else 'cups' if is_cup else 'global'}",
            "league_table": f"/api/league-tables?mode={'cups' if is_cup else 'global'}",
            "cup_bracket": f"/api/cup-bracket?competition={comp_name.replace('/', '%2F').replace(' ', '+')}" if is_cup else None,
        }
        # Predicted winner / league leader
        predicted["leader"] = f"/api/league-leaders"
        base["predicted"] = {k: v for k, v in predicted.items() if v is not None}

        # ── Real data ───────────────────────────────────────────
        real = {
            "real_table": f"/api/real-tables?competition={comp_name.replace('/', '%2F').replace(' ', '+')}",
            "real_cup_data": f"/api/real-cup-data?competition={comp_name.replace('/', '%2F').replace(' ', '+')}" if is_cup else None,
        }
        real["competition_data"] = f"/api/competition-data?competition={comp_name.replace('/', '%2F').replace(' ', '+')}"
        base["real"] = {k: v for k, v in real.items() if v is not None}

        # ── Past games ──────────────────────────────────────────
        base["past_games"] = f"/api/past-games?league={comp_name.replace(' ', '+')}"

        if is_cup:
            cups_list.append(base)
        else:
            leagues_list.append(base)

    return jsonify({
        "ok": True,
        "generated_at_utc": now_str,
        "total": len(leagues_list) + len(cups_list),
        "leagues": leagues_list,
        "cups": cups_list,
        "mls": {
            "competition": "United States/MLS",
            "liga_mx": "Mexico/Liga MX",
            "projected": {
                "league_tables": "/api/league-tables?mode=mls",
                "liga_mx_data": "/api/league-data/Mexico/Liga%20MX",
                "league_data_shield": "/api/league-data/United%20States/MLS%20-%20Supporters%20Shield%20Table",
                "league_data_east": "/api/league-data/United%20States/MLS%20-%20Eastern%20Conference",
                "league_data_west": "/api/league-data/United%20States/MLS%20-%20Western%20Conference",
                "leaders": "/api/league-leaders",
            },
            "real": {
                "real_table": "/api/real-tables?competition=United+States/MLS",
                "real_table_east": "/api/real-tables?competition=United+States/MLS+-+Eastern+Conference",
                "real_table_west": "/api/real-tables?competition=United+States/MLS+-+Western+Conference",
                "real_table_shield": "/api/real-tables?competition=United+States/MLS+-+Supporters+Shield+Table",
            },
            "league_data": "/api/league-data/United%20States/MLS",
            "liga_mx_league_data": "/api/league-data/Mexico/Liga%20MX",
            "upcoming": "/api/upcoming/mls",
        },
    })


@app.get("/api/upcoming/<mode>")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_upcoming(mode):
    """Return upcoming prediction rows for the given source mode.

    The response matches the expected structure from
    ``loadUpcoming()`` in the frontend:

    .. code-block:: json

        {"ok": true, "rows": [...], "stats": {...},
         "league_stats": [...], "available_leagues": [...]}

    The ``global`` mode aggregates rows from **all** sources.

    Optional query parameters for filtering (``global`` mode only):
      ``month`` (str) — ISO month prefix like ``"2026-07"``
      ``window`` (str) — ``"2week"`` (today + 14d), ``"4week"`` (prev week + today + 21d)
    """
    month = str(request.args.get("month", "")).strip()
    window = str(request.args.get("window", "")).strip()
    if window == "4week":
        window_days = 28
        window_include_past = True
    elif window == "2week":
        window_days = 14
        window_include_past = False
    else:
        window_days = None
        window_include_past = False
    is_filtered = bool(month or window)

    def _match_window(date_iso: str) -> bool:
        if month:
            if not str(date_iso).startswith(month):
                return False
        if window_days:
            try:
                d = datetime.strptime(str(date_iso)[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return False
            if window_include_past:
                # 4week mode: past 30 days + next 21 days
                try:
                    from zoneinfo import ZoneInfo
                    today_et = datetime.now(ZoneInfo("America/New_York")).date()
                except Exception:
                    today_et = datetime.now(timezone.utc).date()
                lower = today_et - timedelta(days=30)
                cutoff = today_et + timedelta(days=21)
            else:
                today = datetime.now(timezone.utc).date()
                lower = today
                cutoff = today + timedelta(days=window_days)
            if d < lower or d > cutoff:
                return False
        return True

    # 4week mode: include past games + ALL competitions (friendlies, excluded leagues, etc.)
    four_week_mode = window_include_past
    date_range = "all" if four_week_mode else "upcoming"

    if mode == "global":
        # Prefer the pipeline-generated merged CSV (single read) over
        # loading all source CSVs — but only when the merge is fresh.
        if four_week_mode and _merged_upcoming_file_is_fresh(config.FOUR_WEEK_WINDOW_FILE):
            all_rows, combined_stats, combined_league_stats = \
                _load_upcoming_rows(config.FOUR_WEEK_WINDOW_FILE, "global", date_range="all")
            combined_stats = dict(combined_stats or {})
            combined_league_stats = {ls.get("competition", ""): ls for ls in (combined_league_stats or []) if ls.get("competition")}
        elif (not four_week_mode) and _merged_upcoming_file_is_fresh(config.ALL_UPCOMING_FILE):
            all_rows, combined_stats, combined_league_stats = \
                _load_upcoming_rows(config.ALL_UPCOMING_FILE, "global", date_range=date_range, window_days=window_days)
            combined_stats = dict(combined_stats or {})
            combined_league_stats = {ls.get("competition", ""): ls for ls in (combined_league_stats or []) if ls.get("competition")}
        else:
            all_rows = []
            combined_stats = {"correct_total": 0, "total_predictions": 0, "pending_total": 0, "accuracy_pct": 0.0}
            combined_league_stats = {}
            seen_keys = set()
            for source, csv_path in _ALL_UPCOMING_SOURCES:
                rows, _st, _ls = _load_upcoming_rows(csv_path, source, date_range=date_range, window_days=window_days)
                for r in rows:
                    ck = "|".join(
                        str(r.get(k, "")).strip().lower()
                        for k in ("match_date_iso", "competition", "home_team", "away_team")
                    )
                    if ck and ck not in seen_keys:
                        seen_keys.add(ck)
                        all_rows.append(r)
                for k, v in (_st or {}).items():
                    if k not in combined_stats or isinstance(v, (int, float)):
                        combined_stats[k] = (combined_stats.get(k, 0) if isinstance(v, (int, float)) else 0) + (v if isinstance(v, (int, float)) else 0)
                for ls in (_ls or []):
                    comp = ls.get("competition", "")
                    if comp and comp not in combined_league_stats:
                        combined_league_stats[comp] = ls
        # The generated MLS source can be missing after an upstream provider
        # returns no fixtures. Do not make both regional leagues disappear:
        # expose ESPN schedule-only fixtures until predictions are regenerated.
        all_rows = _regional_espn_schedule_fallback(all_rows)
        seen_keys = set()
        deduped_rows = []
        for row in all_rows:
            ck = "|".join(
                str(row.get(k, "")).strip().lower()
                for k in ("match_date_iso", "competition", "home_team", "away_team")
            )
            if ck and ck not in seen_keys:
                seen_keys.add(ck)
                deduped_rows.append(row)
        all_rows = deduped_rows
        if is_filtered:
            all_rows = [r for r in all_rows if _match_window(str(r.get("match_date_iso", "")))]
        # Re-sort aggregated rows: date → league → time
        all_rows.sort(key=lambda r: (
            _row_date_iso(r),
            str(r.get("competition", "")),
            str(r.get("match_datetime_et", "") or r.get("match_datetime_utc", "")),
            str(r.get("home_team", "")),
        ))
        league_names = sorted({r.get("competition", "") for r in all_rows if r.get("competition")})
        available_leagues = [
            {"name": name, "live_score_tier": config.get_live_score_tier(name)}
            for name in league_names
        ]
        return jsonify({
            "ok": True,
            "rows": all_rows,
            "stats": combined_stats,
            "league_stats": list(combined_league_stats.values()),
            "available_leagues": available_leagues,
        })

    entry = _UPCOMING_MODE_MAP.get(mode)
    if not entry:
        return jsonify({"ok": False, "error": f"Unknown mode: {mode}"}), 400
    csv_path, source_mode = entry
    rows, stats, league_stats = _load_upcoming_rows(csv_path, source_mode, date_range=date_range, window_days=window_days)
    if mode == "mls":
        rows = _regional_espn_schedule_fallback(rows)
    if is_filtered:
        rows = [r for r in rows if _match_window(str(r.get("match_date_iso", "")))]
    league_names = sorted({r.get("competition", "") for r in rows if r.get("competition")})
    available_leagues = [
        {"name": name, "live_score_tier": config.get_live_score_tier(name)}
        for name in league_names
    ]
    return jsonify({
        "ok": True,
        "rows": rows,
        "stats": stats,
        "league_stats": league_stats,
        "available_leagues": available_leagues,
    })


@app.get("/api/home/league-sidebar")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_home_league_sidebar():
    """Compact predicted-winner list for the home page league sidebar."""
    return jsonify({
        "ok": True,
        "leagues": _build_home_league_sidebar_entries(),
    })


@app.get("/api/home/upcoming")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_home_upcoming():
    """Return home-page upcoming matches grouped by league for a date range."""
    window_start, window_end = _date_window_bounds()
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    default_day = min(max(today_et, window_start), window_end)
    start_date = _parse_query_date(request.args.get("start"), default_day)
    end_date = _parse_query_date(request.args.get("end"), start_date)

    # Keep user-selected ranges inside the locally stored fixture window.
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    start_date = min(max(start_date, window_start), window_end)
    end_date = min(max(end_date, window_start), window_end)

    # Prefer the merged CSV when fresh; otherwise load individual sources.
    if _merged_upcoming_file_is_fresh(config.ALL_UPCOMING_FILE):
        merged_rows, _, _ = _load_upcoming_rows(config.ALL_UPCOMING_FILE, "global", date_range="all")
        feed = merged_rows
    else:
        feed = []
        seen_keys = set()
        for source, csv_path in _ALL_UPCOMING_SOURCES:
            rows, _, _ = _load_upcoming_rows(csv_path, source, date_range="all")
            for row in rows:
                key = "|".join(
                    str(row.get(field, "")).strip().lower()
                    for field in ("match_date_iso", "competition", "home_team", "away_team")
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                feed.append(row)

    all_rows = []
    seen_keys = set()
    for row in feed:
            comp = str(row.get("competition", "")).strip()
            if comp in config.UPCOMING_ONLY_COMPETITIONS:
                continue
            if comp in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
                continue
            match_date = _row_date_iso(row)
            if not match_date:
                continue
            try:
                parsed_date = datetime.strptime(match_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if parsed_date < start_date or parsed_date > end_date:
                continue
            key = "|".join(
                str(row.get(field, "")).strip().lower()
                for field in ("match_date_iso", "competition", "home_team", "away_team")
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_rows.append(row)

    all_rows = _exclude_upcoming_only_rows(all_rows)
    all_rows.sort(key=lambda row: (
        _row_date_iso(row),
        str(row.get("competition") or ""),
        str(row.get("match_datetime_et") or row.get("match_datetime_utc") or ""),
        str(row.get("home_team") or ""),
    ))
    return jsonify({
        "ok": True,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "groups": _group_rows_by_league(all_rows),
        "rows": all_rows,
    })


@app.get("/api/world-cup")
def api_world_cup():
    """Return the World Cup projection data (archived; 2026 tournament ended)."""
    world_cup_file = os.path.join(config.PROJECT_DIR, "Data", "Predictions", "world_cup_projection.json")
    if not os.path.exists(world_cup_file):
        return jsonify({
            "ok": True,
            "available": False,
            "error": "World Cup projection not available",
            "group_tables": [],
            "simulations": {"winner_probabilities": {}},
        })
    try:
        with open(world_cup_file, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("ok", True)
        return jsonify(data)
    except Exception:
        return jsonify({
            "ok": True,
            "available": False,
            "error": "Could not load World Cup projection",
            "group_tables": [],
            "simulations": {"winner_probabilities": {}},
        })


@app.get("/api/tournament/<key>")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_tournament(key):
    """Return tournament projection data in World Cup format for the mobile app.

    ``key`` is a short slug such as ``champions-league`` or ``fa-cup``.
    """
    comp_name = config.TOURNAMENT_KEY_MAP.get(str(key or "").strip().lower())
    if not comp_name:
        return jsonify({"ok": False, "error": f"Unknown tournament: {key}"}), 404
    if comp_name == "International/World Cup":
        return api_world_cup()

    with app.test_request_context(query_string={"competition": comp_name}):
        response = api_competition_data()
    data = response.get_json()
    if not isinstance(data, dict) or data.get("ok") is False:
        return response
    return jsonify(_enrich_tournament_payload(comp_name, data))


@app.get("/api/last-refresh")
def api_last_refresh():
    """Return the timestamp of the last pipeline refresh."""
    refreshed_at = get_last_pipeline_run()
    refreshed_at = refreshed_at.isoformat() if refreshed_at else None
    return jsonify({"ok": True, "last_refresh_utc": refreshed_at})


@app.get("/api/pipeline/status")
def api_pipeline_status():
    """Return pipeline health: last run, per-step pass/fail, and backend metadata.

    Use this endpoint (or the mobile feed's ``step_results``) to see which
    sub-pipeline steps failed without SSH-ing into the host logs.
    """
    pipeline = _load_json_payload(config.PIPELINE_STATUS_FILE) or {}
    backend = _load_json_payload(config.BACKEND_RUN_STATUS_FILE) or {}

    sub_pipelines = {"global": [], "mls": [], "extra": [], "post": [], "other": []}
    for step_name, passed in (pipeline.get("steps") or {}).items():
        key = "other"
        if step_name.startswith("global") or step_name in {
            "build_real_standings", "upcoming_world_cup_predictions", "projected_world_cup",
        }:
            key = "global"
        elif step_name.startswith("mls"):
            key = "mls"
        elif step_name.startswith("extra"):
            key = "extra"
        elif step_name.startswith("settle") or step_name.startswith("update") or step_name.startswith("track") or step_name.startswith("sync"):
            key = "post"
        sub_pipelines[key].append({"step": step_name, "ok": bool(passed)})

    refreshed = get_last_pipeline_run()
    log_stats = pipeline_log.log_stats()
    apns_configured = bool(config.APNS_KEY_ID and config.APNS_TEAM_ID and config.APNS_AUTH_KEY_PATH and os.path.exists(config.APNS_AUTH_KEY_PATH or ""))
    return jsonify({
        "ok": True,
        "last_refresh_utc": refreshed.isoformat() if refreshed else None,
        "pipeline": pipeline,
        "backend": backend,
        "sub_pipelines": sub_pipelines,
        "failed_steps": pipeline.get("failed_steps") or [
            k for k, v in (pipeline.get("steps") or {}).items() if not v
        ],
        "log": {
            "file": log_stats.get("log_file"),
            "bytes": log_stats.get("bytes", 0),
            "lines": log_stats.get("lines", 0),
            "exists": log_stats.get("exists", False),
            "highlights": pipeline.get("log_highlights") or [],
            "logs_api": "/api/pipeline/logs",
        },
        "services": {
            "live_score_poller": True,
            "apns_notifications": apns_configured,
            "apns_live_activities": apns_configured,
            "mutation_auth": bool(config.MUTATION_API_TOKEN),
        },
    })


@app.get("/api/pipeline/logs")
def api_pipeline_logs():
    """Return persisted pipeline terminal output from the latest run.

    Query params:
        tail   — max lines from end of log (default 500, max 5000)
        level  — ``all`` | ``notable`` (OK+WARN+ERROR) | ``warn`` | ``error``
        grep   — optional case-insensitive regex filter on line text
        format — ``json`` (default) or ``text`` (plain-text body for quick copy)
    """
    try:
        tail = int(request.args.get("tail", "500"))
    except (TypeError, ValueError):
        tail = 500
    level = str(request.args.get("level", "all")).strip().lower() or "all"
    grep = str(request.args.get("grep", "")).strip()
    fmt = str(request.args.get("format", "json")).strip().lower() or "json"

    payload = pipeline_log.read_log(tail=tail, level=level, grep=grep)
    payload["ok"] = True

    if fmt == "text":
        from flask import Response
        return Response(
            payload.get("text") or "",
            mimetype="text/plain; charset=utf-8",
        )
    return jsonify(payload)


@app.post("/api/refresh")
def api_refresh():
    """Trigger a background pipeline refresh (non-blocking when BackendServer is running)."""
    if not _mutation_authorized():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    if not config.PIPELINE_ENABLED:
        return jsonify({"ok": False, "error": "Pipeline disabled (set PIPELINE_ENABLED=1 to enable)"}), 403

    refresh_fn = app.config.get("_backend_refresh")
    if callable(refresh_fn):
        started = refresh_fn(trigger="api", full_retrain=False)
        if not started:
            return jsonify({
                "ok": False,
                "queued": False,
                "mode": "backend",
                "full_retrain": False,
                "error": "Pipeline already running or could not start.",
            }), 409
        return jsonify({"ok": True, "queued": True, "mode": "backend", "full_retrain": False})

    started = _run_full_pipeline_once(full_retrain=False)
    return jsonify({
        "ok": bool(started),
        "queued": False,
        "mode": "inline",
        "full_retrain": False,
        "message": "Light refresh finished inline (no BackendServer hook registered).",
    }), (200 if started else 500)


@app.post("/api/retrain")
def api_retrain():
    """Force a full model retrain.
    Same as the scheduled Tuesday/Friday run: downloads data and retrains all
    model caches. Non-blocking when :class:`BackendServer` is running.
    """
    if not _mutation_authorized():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    if not config.PIPELINE_ENABLED:
        return jsonify({"ok": False, "error": "Pipeline disabled (set PIPELINE_ENABLED=1 to enable)"}), 403

    refresh_fn = app.config.get("_backend_refresh")
    if callable(refresh_fn):
        started = refresh_fn(trigger="api-retrain", full_retrain=True)
        if not started:
            return jsonify({
                "ok": False,
                "queued": False,
                "mode": "backend",
                "full_retrain": True,
                "error": "Pipeline already running or could not start.",
            }), 409
        return jsonify({
            "ok": True,
            "queued": True,
            "mode": "backend",
            "full_retrain": True,
            "message": "Full model retrain queued.",
        })

    started = _run_full_pipeline_once(full_retrain=True)
    return jsonify({
        "ok": bool(started),
        "queued": False,
        "mode": "inline",
        "full_retrain": True,
        "message": "Full retrain finished inline (no BackendServer hook registered).",
    }), (200 if started else 500)


@app.get("/api/mobile/widget")
def api_mobile_widget():
    """Lightweight widget feed: upcoming games filtered by league/team/random.
    
    Query params:
        league  — comma-separated league names (e.g. \"England/Premier League,Spain/La Liga\")
        team    — team name to filter by (e.g. \"Chelsea\")
        limit   — max rows to return (default 10, max 50)
        mode    — \"random\" to shuffle and return random picks
    """
    import random as _random

    leagues_param = request.args.get("league", "").strip()
    team_param = request.args.get("team", "").strip()
    try:
        limit = min(max(1, int(request.args.get("limit", "10"))), 50)
    except (ValueError, TypeError):
        limit = 10
    mode = request.args.get("mode", "").strip().lower()

    filter_leagues = [l.strip() for l in leagues_param.split(",") if l.strip()] if leagues_param else []

    rows = []
    seen = set()
    for csv_path in config.UPCOMING_CSV_FILES.values():
        if not os.path.exists(csv_path):
            continue
        try:
            frame = pd.read_csv(csv_path, dtype=str)
        except Exception:
            continue
        for _, row in frame.iterrows():
            comp = str(row.get("competition", "") or "").strip()
            home = str(row.get("home_team", "") or "").strip()
            away = str(row.get("away_team", "") or "").strip()
            dedup_key = f"{comp}|{home}|{away}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            if filter_leagues and comp not in filter_leagues:
                continue
            if team_param and team_param.lower() not in (home.lower(), away.lower()):
                continue
            rows.append({
                "competition": comp,
                "match_date": str(row.get("match_date", "") or "").strip(),
                "match_datetime_utc": _utc_to_et(str(row.get("match_datetime_utc", "") or "").strip()),
                "home_team": home,
                "away_team": away,
                "predicted_result": str(row.get("predicted_result", "") or "").strip(),
                "prob_home": _to_float(row.get("prob_home")),
                "prob_draw": _to_float(row.get("prob_draw")),
                "prob_away": _to_float(row.get("prob_away")),
            })

    if mode == "random":
        _random.shuffle(rows)

    return jsonify({
        "ok": True,
        "count": min(len(rows), limit),
        "total": len(rows),
        "rows": rows[:limit],
        "generated_at_utc": datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def _to_float(val):
    try:
        return round(float(val), 1)
    except (ValueError, TypeError):
        return None


@app.post("/api/predict")
def api_predict():
    """Predict a single matchup from user input.
    
    JSON body:
        home_team (str, required)
        away_team (str, required)
        mode (str, optional) — "global" (default), "mls", or "extra"
    """
    payload = request.get_json(silent=True) or request.form
    home_team = str(payload.get("home_team", "")).strip()
    away_team = str(payload.get("away_team", "")).strip()
    mode = str(payload.get("mode", "global")).strip().lower()
    if mode not in ("global", "mls", "extra"):
        mode = "global"
    try:
        result = _predict(home_team, away_team, mode=mode)
    except Exception:
        return jsonify({"ok": False, "error": "Prediction failed"}), 400
    return jsonify({"ok": True, "prediction": result})


@app.post("/api/predict/mls")
def api_predict_mls():
    """Predict a single MLS matchup from user input."""
    payload = request.get_json(silent=True) or request.form
    home_team = str(payload.get("home_team", "")).strip()
    away_team = str(payload.get("away_team", "")).strip()
    try:
        result = _predict(home_team, away_team, mode="mls")
    except Exception:
        return jsonify({"ok": False, "error": "Prediction failed"}), 400
    return jsonify({"ok": True, "prediction": result})


@app.post("/api/predict/extra")
def api_predict_extra():
    """Predict a single extra-league matchup from user input."""
    payload = request.get_json(silent=True) or request.form
    home_team = str(payload.get("home_team", "")).strip()
    away_team = str(payload.get("away_team", "")).strip()
    try:
        result = _predict(home_team, away_team, mode="extra")
    except Exception:
        return jsonify({"ok": False, "error": "Prediction failed"}), 400
    return jsonify({"ok": True, "prediction": result})


@app.route("/api/redeem", methods=["GET", "POST"])
def api_redeem():
    """Redeem a promo code.

    Body (JSON) or query param:
        code (str, required) — the promo code to redeem

    File format (``Data/redeem_codes.json`` next to ``team_name_mapping_master.json``)::

        [{"code": "CODEHERE", "value": true}]

    Codes are case-sensitive and may contain letters and digits only
    (no spaces or special characters).

    Response (success):
        ``{"ok": true, "value": <entry.value>}``

    Response (unknown code):
        ``{"ok": false, "error": "unknown code"}`` with HTTP 200

    Errors:
        400 — missing / invalid code body (empty or non-alphanumeric)
        503 — codes file missing or unreadable on the server
    """
    payload = request.get_json(silent=True) or {}
    raw = payload.get("code", "")
    if raw in (None, "") and request.args.get("code"):
        raw = request.args.get("code", "")
    if not isinstance(raw, str):
        return jsonify({"ok": False, "error": "code must be a string"}), 400

    code = _parse_redeem_code(raw)
    if code is None:
        return jsonify({
            "ok": False,
            "error": "missing or invalid code",
            "detail": "code must be letters and digits only (case-sensitive)",
        }), 400
    if not code or len(code) > 200:
        return jsonify({"ok": False, "error": "missing or invalid code"}), 400

    entries = _load_redeem_code_entries()
    if not entries:
        return jsonify({
            "ok": False,
            "error": "redeem codes unavailable",
            "detail": f"Expected JSON list at {config.REDEEM_CODES_FILE}",
        }), 503

    for entry in entries:
        entry_code = _parse_redeem_code(entry.get("code", ""))
        if not entry_code:
            continue
        # Case-sensitive exact match (alphanumeric only on both sides).
        if entry_code == code:
            return jsonify({
                "ok": True,
                "value": entry.get("value", True),
            })

    return jsonify({"ok": False, "error": "unknown code"})


def _parse_redeem_code(raw) -> str | None:
    """Parse a redeem code for case-sensitive comparison.

    Rules:
      - strip leading/trailing whitespace only
      - allow letters (A–Z, a–z) and digits (0–9) only
      - no spaces or special characters inside the code
      - case is preserved (``AbC`` ≠ ``abc``)

    Returns ``None`` when the code contains disallowed characters.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if not text.isalnum():
        return None
    return text


def _load_redeem_code_entries() -> list[dict]:
    """Load redeem codes from repo ``Data/redeem_codes.json``.

    Expected shape only::

        [{"code": "CODEHERE", "value": true}, ...]
    """
    path = config.REDEEM_CODES_FILE
    payload = _load_json_payload(path)
    if not isinstance(payload, list):
        example_path = getattr(config, "REDEEM_CODES_EXAMPLE_FILE", "")
        if example_path and path != example_path and os.path.isfile(example_path):
            payload = _load_json_payload(example_path)
    if not isinstance(payload, list):
        return []
    entries = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if code is None or str(code).strip() == "":
            continue
        parsed = _parse_redeem_code(code)
        if not parsed:
            continue
        entries.append({
            "code": parsed,
            "value": item.get("value", True),
        })
    return entries


@app.get("/api/live-scores")
def api_live_scores():
    """Return live scores for active competitions (polled every 5 min from ESPN).

    Query params:
        competition  -- optional, filter to specific competition(s) (comma-separated)
    """
    comp_filter = request.args.get("competition", "").strip()
    with _live_scores_lock:
        if not _live_scores:
            return jsonify({"ok": True, "competitions": {}, "message": "No live games at this time."})
        if comp_filter:
            wanted = {c.strip() for c in comp_filter.split(",") if c.strip()}
            filtered = {k: v for k, v in _live_scores.items() if k in wanted}
            return jsonify({"ok": True, "competitions": filtered})
        return jsonify({"ok": True, "competitions": dict(_live_scores)})


@app.get("/api/h2h")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_h2h():
    """Return head-to-head and form data for two teams."""
    team1_input = request.args.get("team1", "").strip()
    team2_input = request.args.get("team2", "").strip()
    mode = request.args.get("mode", "global").strip().lower()

    if not team1_input or not team2_input:
        return jsonify({"ok": False, "error": "Missing teams"}), 400

    if mode == "mls":
        pm_mod = pm_mls
    elif mode == "extra":
        pm_mod = pm_extra
    else:
        pm_mod = pm_global

    # Always load H2H/form JSON directly — never pull in the full ML model context.
    head_to_head, current_form = _load_h2h_and_form(pm_mod)
    form_teams = current_form.get("teams", {}) if isinstance(current_form, dict) else {}

    team1 = _team_name_for_db(team1_input)
    team2 = _team_name_for_db(team2_input)

    t1_form = _normalize_recent_form_payload(form_teams.get(team1, {}))
    t2_form = _normalize_recent_form_payload(form_teams.get(team2, {}))

    h2h_data = _normalize_h2h_payload((head_to_head or {}).get(team1, {}).get(team2))
    h2h_data_reverse = _normalize_h2h_payload((head_to_head or {}).get(team2, {}).get(team1))
    h2h_total_games = max(h2h_data.get("games", 0), h2h_data_reverse.get("games", 0))

    return jsonify({
        "ok": True,
        "team1_form": t1_form,
        "team2_form": t2_form,
        "h2h_data": h2h_data,
        "h2h_data_reverse": h2h_data_reverse,
        "h2h_total_games": h2h_total_games,
    })


@app.get("/api/debug/live-score-sources")
def api_debug_live_score_sources():
    """Debug endpoint: show what _get_todays_competitions() detects and which files exist/stale."""
    if not _mutation_authorized():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    info = {}
    info["today_date"] = date.today().isoformat()
    info["now_et"] = datetime.now(ZoneInfo("America/New_York")).isoformat()
    info["csv_files"] = {}
    for name, path in config.UPCOMING_CSV_FILES.items():
        entry = {"exists": os.path.exists(path)}
        if entry["exists"]:
            entry["size_bytes"] = os.path.getsize(path)
            entry["mtime_utc"] = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
            try:
                df = pd.read_csv(path, dtype=str)
                entry["rows"] = len(df)
                if "match_datetime_utc" in df.columns:
                    utc_dates = pd.to_datetime(df["match_datetime_utc"], errors="coerce")
                    if hasattr(utc_dates.dt, "tz") and utc_dates.dt.tz is not None:
                        et_dates = utc_dates.dt.tz_convert(ZoneInfo("America/New_York"))
                    else:
                        et_dates = utc_dates.dt.tz_localize("UTC", ambiguous="NaT").dt.tz_convert(ZoneInfo("America/New_York"))
                    entry["date_range"] = [et_dates.min().strftime("%Y-%m-%d") if pd.notna(et_dates.min()) else None,
                                           et_dates.max().strftime("%Y-%m-%d") if pd.notna(et_dates.max()) else None]
                    entry["today_count"] = int((et_dates.dt.date == date.today()).sum())
                elif "match_date" in df.columns:
                    parsed = pd.to_datetime(df["match_date"], errors="coerce", dayfirst=False)
                    entry["date_range"] = [parsed.min().strftime("%Y-%m-%d") if pd.notna(parsed.min()) else None,
                                           parsed.max().strftime("%Y-%m-%d") if pd.notna(parsed.max()) else None]
                    entry["today_count"] = int((parsed.dt.date == date.today()).sum())
                entry["competitions"] = sorted(df["competition"].dropna().unique().tolist()) if "competition" in df.columns else []
            except Exception as e:
                entry["read_error"] = str(e)
        info["csv_files"][name] = entry
    info["wc_projection"] = {"exists": os.path.exists(config.WORLD_CUP_PROJECTION_FILE)}
    if info["wc_projection"]["exists"]:
        info["wc_projection"]["size_bytes"] = os.path.getsize(config.WORLD_CUP_PROJECTION_FILE)
    info["cup_bracket"] = {"exists": os.path.exists(config.CUP_PROJECTED_BRACKET_FILE)}
    if info["cup_bracket"]["exists"]:
        info["cup_bracket"]["size_bytes"] = os.path.getsize(config.CUP_PROJECTED_BRACKET_FILE)
    todays_comps = _get_todays_competitions()
    info["todays_competitions"] = {k: [v.isoformat() for v in vs] for k, vs in todays_comps.items()}
    return jsonify({"ok": True, "debug": info})


@app.get("/api/debug/manual-poll")
def api_debug_manual_poll():
    """Manually run one ESPN poll cycle and return the results."""
    if not _mutation_authorized():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    today_str = date.today().strftime("%Y%m%d")
    all_results = {}
    for comp_name, espn_id in config.LIVE_SCORE_COMPETITIONS.items():
        try:
            games = _fetch_competition_scores(comp_name, espn_id, today_str)
        except Exception:
            continue
        if games:
            all_results[comp_name] = {
                "competition": comp_name,
                "games": games,
                "last_polled_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
            }
    return jsonify({
        "ok": True,
        "today_str": today_str,
        "competitions_found": len(all_results),
        "total_games": sum(len(v["games"]) for v in all_results.values()),
        "live_scores": all_results,
    })


@app.get("/api/debug/poller-state")
def api_debug_poller_state():
    """Show what the poller thread currently has stored and its last poll timing."""
    if not _mutation_authorized():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    with _live_scores_lock:
        state = {
            "poll_competitions": list(_live_scores.keys()),
            "total_games": sum(len(v.get("games", [])) for v in _live_scores.values()),
            "last_polled_utc": max(
                (v.get("last_polled_utc", "") for v in _live_scores.values()),
                default=None,
            ),
            "poller_date": getattr(_live_score_poller_loop, "_poller_date", None),
            "live_scores": _live_scores,
        }
    return jsonify({"ok": True, "state": state})


@app.get("/api/live-score-history")
def api_live_score_history():
    """Return historical completed games, grouped by competition
    (matching ``/api/live-scores`` response structure).

    Query params:
        league   -- filter by competition name (substring match, case-insensitive)
        from     -- start date (ISO, e.g. 2026-06-01), filters by kickoff_utc >=
        to       -- end date (ISO, e.g. 2026-06-18), filters by kickoff_utc <=
    """
    league = request.args.get("league", "").strip()
    from_date = request.args.get("from", "").strip()
    to_date = request.args.get("to", "").strip()

    # Validate date format
    if from_date and not _valid_date_iso(from_date):
        return jsonify({"ok": False, "error": "Invalid 'from' date format (use YYYY-MM-DD)"}), 400
    if to_date and not _valid_date_iso(to_date):
        return jsonify({"ok": False, "error": "Invalid 'to' date format (use YYYY-MM-DD)"}), 400

    games = _load_live_score_history()

    if league:
        league_lower = league.lower()
        games = [g for g in games if league_lower in g.get("competition", "").lower()]
    if from_date:
        games = [g for g in games if g.get("kickoff_utc", "") >= from_date]
    if to_date:
        games = [g for g in games if g.get("kickoff_utc", "") <= to_date]

    games.sort(key=lambda g: g.get("kickoff_utc", ""), reverse=True)

    competitions = {}
    for g in games:
        comp = g.get("competition", "Unknown")
        if comp not in competitions:
            competitions[comp] = {
                "competition": comp,
                "games": [],
                "last_polled_utc": datetime.now(timezone.utc).isoformat(),
            }
        competitions[comp]["games"].append(g)

    return jsonify({
        "ok": True,
        "competitions": competitions,
    })


@app.get("/api/past-games")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_past_games():
    """Return completed games persisted across pipeline runs.

    Response structure matches ``/api/upcoming/global`` per-row format.

    Data is sourced from ``past_games.json`` (updated each pipeline run with
    today's rows copied from the upcoming API shape), ``live_score_history.json``
    / in-memory live scores, and settled prediction CSV rows.
    Rows older than 30 days are excluded.

    For full live-score details (lineups, stats, key events, game info),
    use ``/api/live-score-history``.

    Query params:
        league   -- filter by competition name (substring match, case-insensitive)
        page     -- page number (default 1)
        per_page -- results per page (default 50, max 200)
    """
    league = request.args.get("league", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(200, max(1, int(request.args.get("per_page", "50"))))
    except (ValueError, TypeError):
        per_page = 50

    prediction_lookup = _build_past_game_prediction_lookup()

    # ── 1. Rows from persistent archive ────────────────────────────
    by_key = {}
    archive = _load_json_payload(config.PAST_GAMES_FILE)
    if isinstance(archive, list):
        for r in archive:
            if _is_placeholder_game(r):
                continue
            _enrich_json_past_row(r)
            ck = "|".join(
                [
                    _past_row_date_iso(r),
                    str(r.get("competition", "")).strip().lower(),
                    str(r.get("home_team", "")).strip().lower(),
                    str(r.get("away_team", "")).strip().lower(),
                ]
            )
            if ck.strip("|"):
                by_key[ck] = r

    # ── 2. Completed games from live scores (today + recent) ───────
    for r in _collect_live_past_game_rows("2000-01-01"):
        r = _merge_prediction_onto_past_row(r, prediction_lookup)
        ck = "|".join(
            [
                _past_row_date_iso(r),
                str(r.get("competition", "")).strip().lower(),
                str(r.get("home_team", "")).strip().lower(),
                str(r.get("away_team", "")).strip().lower(),
            ]
        )
        if ck.strip("|"):
            by_key[ck] = r

    # ── 3. Supplement with CSV rows (richest prediction data) ───────
    for source, csv_path in (
        ("global", config.GLOBAL_UPCOMING_FILE),
        ("mls", config.MLS_UPCOMING_FILE),
        ("extra", config.EXTRA_UPCOMING_FILE),
        ("cups", config.CUP_UPCOMING_FILE),
        ("national", config.NATIONAL_UPCOMING_FILE),
    ):
        rows, _st, _ls = _load_upcoming_rows(csv_path, source, date_range="completed")
        for r in rows:
            # Only include actually settled games — skip placeholders
            if str(r.get("actual_result", "")).strip().upper() not in {"H", "D", "A"}:
                continue
            if _is_placeholder_game(r):
                continue
            ck = "|".join(
                [
                    _past_row_date_iso(r),
                    str(r.get("competition", "")).strip().lower(),
                    str(r.get("home_team", "")).strip().lower(),
                    str(r.get("away_team", "")).strip().lower(),
                ]
            )
            if ck.strip("|"):
                by_key[ck] = r  # CSV row (enriched) takes priority

    all_rows = list(by_key.values())

    if league:
        league_lower = league.lower()
        all_rows = [r for r in all_rows if league_lower in r.get("competition", "").lower()]

    all_rows.sort(key=lambda r: _past_row_date_iso(r), reverse=True)

    total = len(all_rows)
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = all_rows[start:end]

    return jsonify({
        "ok": True,
        "rows": page_rows,
        "total": total,
        "page": page,
        "per_page": per_page,
    })


# ── Push Notifications (no auth required) ─────────────────────────


@app.post("/api/notifications")
def api_push_notification():
    """Queue a push notification for delivery via APNs."""
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", "")).strip()
    if not title or not body:
        return jsonify({"ok": False, "error": "title and body required"}), 400
    badge = payload.get("badge", 0)
    try:
        badge = max(0, int(badge))
    except (TypeError, ValueError):
        badge = 0
    _notifications.append({
        "id": len(_notifications),
        "title": title,
        "body": body,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "type": payload.get("type", "info"),
    })
    for device_token in list(ios_device_tokens):
        _apns_notification_queue.append({
            "token": device_token,
            "title": title,
            "body": body,
            "badge": badge,
        })
    return jsonify({"ok": True})


@app.get("/api/notifications")
def api_get_notifications():
    """Return recent notifications."""
    limit = min(int(request.args.get("limit", "20")), 100)
    items = list(_notifications)[-limit:]
    return jsonify({"ok": True, "notifications": items})


@app.post("/api/notifications/register")
def api_register_device():
    """Register a device token for push notifications.

    Body params:
        token    (str, required) — device push token
        platform (str)           — ``"ios"`` or ``"generic"`` (default)
    """
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    if not token:
        return jsonify({"ok": False, "error": "token required"}), 400
    if len(token) > 512:
        return jsonify({"ok": False, "error": "token too long"}), 400
    platform = str(payload.get("platform", "generic")).strip().lower()
    if platform == "ios":
        ios_device_tokens.add(token)
    else:
        device_tokens.add(token)
    return jsonify({"ok": True, "registered": True})


@app.post("/api/notifications/unregister")
def api_unregister_device():
    """Remove a device token — stop receiving push notifications.

    Body params:
        token    (str, required) — device push token to remove
        platform (str)           — ``"ios"`` or ``"generic"`` (default)
    """
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    if not token:
        return jsonify({"ok": False, "error": "token required"}), 400
    platform = str(payload.get("platform", "generic")).strip().lower()
    if platform == "ios":
        ios_device_tokens.discard(token)
    else:
        device_tokens.discard(token)
    return jsonify({"ok": True, "removed": True})


@app.post("/api/notifications/subscribe")
def api_subscribe_match_notifications():
    """Subscribe a device to live-event alerts for a specific match.

    Body params:
        token       (str, required) — device push token
        match_id    (str, required) — match identifier
        competition (str, required) — competition name
    """
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    match_id = str(payload.get("match_id", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    if not token or not match_id or not competition:
        return jsonify({"ok": False, "error": "token, match_id, and competition required"}), 400
    if len(token) > 512 or len(match_id) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400
    ok = subscribe_match(token, match_id, competition)
    return jsonify({"ok": True, "subscribed": ok})


@app.post("/api/notifications/unsubscribe")
def api_unsubscribe_match_notifications():
    """Remove a device from a match's live-event alert list.

    Body params:
        token       (str, required) — device push token
        match_id    (str, required) — match identifier
        competition (str, required) — competition name
    """
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    match_id = str(payload.get("match_id", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    if not token or not match_id or not competition:
        return jsonify({"ok": False, "error": "token, match_id, and competition required"}), 400
    if len(token) > 512 or len(match_id) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400
    ok = unsubscribe_match(token, match_id, competition)
    return jsonify({"ok": True, "unsubscribed": ok})


# ── Live Activity endpoints (iOS 16.1+) ────────────────────────────


@app.post("/api/live-activities/register")
def api_register_live_activity():
    """Register a Live Activity push token for a specific match.

    Body:
        activity_token (str, required) — push token from the Live Activity
        device_token  (str, optional) — device push token
        match_id      (str, required) — match identifier
        competition   (str, required) — competition name
    """
    payload = request.get_json(silent=True) or {}
    activity_token = str(payload.get("activity_token", "")).strip()
    match_id = str(payload.get("match_id", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    if not activity_token or not match_id or not competition:
        return jsonify({"ok": False, "error": "activity_token, match_id, and competition required"}), 400
    if len(activity_token) > 1024 or len(match_id) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400
    device_token = str(payload.get("device_token", "")).strip()
    if len(device_token) > 512:
        return jsonify({"ok": False, "error": "device_token too long"}), 400
    ok = live_activities.register(activity_token, device_token, match_id, competition)
    if ok and device_token:
        subscribe_match(device_token, match_id, competition)
    return jsonify({"ok": True, "registered": ok, "total": len(live_activities.all_activities())})


@app.post("/api/live-activities/unregister")
def api_unregister_live_activity():
    """Remove a Live Activity registration."""
    payload = request.get_json(silent=True) or {}
    activity_token = str(payload.get("activity_token", "")).strip()
    if not activity_token:
        return jsonify({"ok": False, "error": "activity_token required"}), 400
    if len(activity_token) > 1024:
        return jsonify({"ok": False, "error": "activity_token too long"}), 400
    ok = live_activities.unregister(activity_token)
    if ok:
        match_id = str(payload.get("match_id", "")).strip()
        competition = str(payload.get("competition", "")).strip()
        device_token = str(payload.get("device_token", "")).strip()
        if match_id and competition and device_token:
            unsubscribe_match(device_token, match_id, competition)
    return jsonify({"ok": True, "removed": ok})


@app.post("/api/live-activities/update")
def api_update_live_activity():
    """Manually push a content-state update to all Live Activities for a match.

    Body:
        match_id      (str, required)
        competition   (str, required)
        content_state (dict, required)
    """
    payload = request.get_json(silent=True) or {}
    match_id = str(payload.get("match_id", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    content_state = payload.get("content_state")
    if not match_id or not competition or not isinstance(content_state, dict):
        return jsonify({"ok": False, "error": "match_id, competition, and content_state required"}), 400
    if len(match_id) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400
    activities = live_activities.for_match(match_id, competition)
    sent = 0
    for entry in activities:
        _apns_notification_queue.append({
            "type": "liveactivity",
            "token": entry["activity_token"],
            "content_state": content_state,
            "event": "update",
        })
        sent += 1
    return jsonify({"ok": True, "sent": sent})


@app.post("/api/live-activities/end")
def api_end_live_activity():
    """End/dismiss Live Activities for a match.

    Body:
        match_id      (str, required)
        competition   (str, required)
        content_state (dict, optional) — final state before dismissal
    """
    payload = request.get_json(silent=True) or {}
    match_id = str(payload.get("match_id", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    if not match_id or not competition:
        return jsonify({"ok": False, "error": "match_id and competition required"}), 400
    if len(match_id) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400
    content_state = payload.get("content_state", {})
    if not isinstance(content_state, dict):
        content_state = {}
    activities = live_activities.for_match(match_id, competition)
    sent = 0
    for entry in activities:
        _apns_notification_queue.append({
            "type": "liveactivity",
            "token": entry["activity_token"],
            "content_state": content_state,
            "event": "end",
        })
        sent += 1
    live_activities.unregister_by_match(match_id, competition)
    return jsonify({"ok": True, "sent": sent, "deregistered": True})


@app.get("/api/cup-bracket")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_cup_bracket():
    """Return real bracket for a cup competition in World Cup format.

    Response mirrors ``/api/world-cup``:

    * ``knockout`` — ``{stage_key: [matches]}}`` with actual winners
      from live scores / projected bracket.
    * ``odds_knockout`` — same topology, each match's ``winner`` is the
      team with higher ``prob_home`` / ``prob_away``.
    * ``real_knockout`` — winners only from completed (``post``) games;
      unplayed matches show both teams but ``winner: null``.

    Additional cup-specific fields:
    ``knockout_rounds`` (bracket topology with feeds_to),
    ``league_phase`` (UEFA league-phase table),
    ``cup_format`` (format descriptor).

    Query params:
        competition  -- required, e.g. "England/FA Cup", "International/World Cup",
                        "Europe/Champions League"
    """
    comp = request.args.get("competition", "").strip()
    if not comp:
        return jsonify({"ok": False, "error": "Missing 'competition' parameter"}), 400
    if comp not in config.LIVE_SCORE_COMPETITIONS:
        return jsonify({"ok": False, "error": f"Unknown competition: {comp}"}), 400

    matches = []

    # 1. Completed games from live score history
    history = _load_live_score_history()
    seen_ids = set()
    for g in history:
        if g.get("competition") == comp:
            mid = g.get("match_id", "")
            if mid:
                seen_ids.add(mid)
            matches.append(g)

    # 2. In-progress / finished today from live scores
    with _live_scores_lock:
        current = _live_scores.get(comp, {}).get("games", [])
    for g in current:
        mid = g.get("match_id", "")
        if mid not in seen_ids:
            if mid:
                seen_ids.add(mid)
            matches.append(g)

    # 3. Upcoming games from the projected cup bracket JSON
    bracket_data = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    _append_projected_cup_matches(matches, comp, bracket_data)

    # 4. Enrich with odds from cup predictions CSV
    odds_index = {}
    try:
        odds_df = pd.read_csv(config.CUP_UPCOMING_FILE)
        if not odds_df.empty and all(c in odds_df.columns for c in ("home_team", "away_team", "prob_home", "prob_draw", "prob_away")):
            for _, row in odds_df.iterrows():
                key = (str(row["home_team"]).strip().lower(), str(row["away_team"]).strip().lower())
                odds_index[key] = {
                    "prob_home": _safe_float(row["prob_home"], None),
                    "prob_draw": _safe_float(row["prob_draw"], None),
                    "prob_away": _safe_float(row["prob_away"], None),
                }
    except Exception:
        pass

    for g in matches:
        rnd = _normalize_round_label(g.get("round"))
        order = g.get("round_order", 0)
        if not isinstance(order, (int, float)):
            try:
                order = int(order)
            except (ValueError, TypeError):
                order = 0
        g["round_order"] = order
        g["round"] = rnd

        # Compute winner for completed games
        if g.get("status") == "post":
            hs = g.get("home_score")
            aws = g.get("away_score")
            if hs is not None and aws is not None:
                if hs > aws:
                    g["winner"] = g.get("home_team", "")
                elif aws > hs:
                    g["winner"] = g.get("away_team", "")

        # Attach odds
        hm_name = str(g.get("home_team", "")).strip().lower()
        aw_name = str(g.get("away_team", "")).strip().lower()
        odds = odds_index.get((hm_name, aw_name)) or odds_index.get((aw_name, hm_name), {})
        g["prob_home"] = odds.get("prob_home")
        g["prob_draw"] = odds.get("prob_draw")
        g["prob_away"] = odds.get("prob_away")

    # 5. Build WC-style knockout structures
    knockout, odds_knockout, real_knockout = _build_cup_knockout_payload(matches, comp)

    result = {
        "ok": True,
        "competition": comp,
        "knockout": knockout,
        "odds_knockout": odds_knockout,
        "real_knockout": real_knockout,
    }
    ko_framework = _build_knockout_framework(comp)
    if ko_framework:
        result["knockout_rounds"] = ko_framework
    if comp in _UEFA_COMPETITIONS:
        league_table = _compute_standings_from_history(comp)
        if league_table:
            result["league_phase"] = league_table
    cup_format = config._CUP_FORMATS.get(comp)
    if cup_format:
        result["cup_format"] = cup_format

    return jsonify(result)


@app.get("/api/real-cup-data")
def api_real_cup_data():
    """Return real cup data in World Cup format.

    Response mirrors ``/api/world-cup``:

    * ``knockout`` — ``{stage_key: [matches]}}`` with actual winners
      from live scores / projected bracket.
    * ``odds_knockout`` — same topology, each match's ``winner`` is the
      team with higher ``prob_home`` / ``prob_away``.
    * ``real_knockout`` — winners only from completed (``post``) games.

    Additional fields: ``cup_format``, ``table``, ``knockout_rounds``.

    Query params:
        competition  -- required, e.g. "Europe/Champions League", "England/FA Cup"
    """
    comp = request.args.get("competition", "").strip()
    if not comp:
        return jsonify({"ok": False, "error": "Missing 'competition' parameter"}), 400
    if comp not in config.LIVE_SCORE_COMPETITIONS:
        return jsonify({"ok": False, "error": f"Unknown competition: {comp}"}), 400

    # 1. Format metadata
    cup_format = config._CUP_FORMATS.get(comp)
    if comp in _UEFA_COMPETITIONS and cup_format is None:
        for key, fmt in config._CUP_FORMATS.items():
            if key in _UEFA_COMPETITIONS and config.LIVE_SCORE_COMPETITIONS.get(comp) == config.LIVE_SCORE_COMPETITIONS.get(key):
                cup_format = fmt
                break

    # 2. Group / league phase tables from history
    table = _compute_standings_from_history(comp)

    # 3. Gather all matches for this competition
    matches = _gather_competition_cup_matches(comp)

    # 4. Build WC-style knockout structures
    knockout, odds_knockout, real_knockout = _build_cup_knockout_payload(matches, comp)

    result = {
        "ok": True,
        "competition": comp,
        "cup_format": cup_format,
        "knockout": knockout,
        "odds_knockout": odds_knockout,
        "real_knockout": real_knockout,
    }

    ko_framework = _build_knockout_framework(comp)
    if ko_framework:
        result["knockout_rounds"] = ko_framework

    if table is not None:
        result["table"] = table

    return jsonify(result)


@app.get("/api/real-tables")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_real_tables():
    """Return real league tables computed from live-score history.

    Supports standard leagues, group-stage (World Cup, UCL),
    MLS conferences, Belgian 2-phase, Scottish Premiership split,
    and UEFA league-phase competitions.

    Query params:
        competition  -- optional, fetch a specific competition only
                        (e.g. "England/Premier League", "International/World Cup")
                        If omitted, returns tables for all known competitions.
    """
    comp_filter = request.args.get("competition", "").strip()
    force_refresh = request.args.get("refresh", "").strip().lower() in ("1", "true")

    from competition_rules import should_use_persisted_table

    if comp_filter:
        if comp_filter in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
            return jsonify({
                "ok": False,
                "error": f"Competition not available in league APIs: {comp_filter}",
            }), 404
        if comp_filter not in config.LIVE_SCORE_COMPETITIONS and comp_filter not in config.MLS_TABLE_VIEW_ALIASES:
            return jsonify({"ok": False, "error": f"Unknown competition: {comp_filter}"}), 400
        if force_refresh:
            _clear_standings_cache(comp_filter)
            _clear_leaders_cache(comp_filter)
        persisted = _load_json_payload(config.REAL_TABLES_PERSIST_FILE)
        if isinstance(persisted, dict) and comp_filter in persisted:
            cached = persisted[comp_filter]
            if should_use_persisted_table(cached, force_refresh):
                cleaned = _sanitize_real_standings(cached, comp_filter) or cached
                return jsonify({"ok": True, "table": cleaned})
        table = _compute_standings_from_history(comp_filter)
        if table is not None:
            return jsonify({"ok": True, "table": table})
        fallback = _build_fallback_standings(comp_filter)
        return jsonify({"ok": True, "table": fallback})

    results = {}
    now_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    # Try persisted standings cache first (built by the daily pipeline)
    persisted = _load_json_payload(config.REAL_TABLES_PERSIST_FILE)
    if isinstance(persisted, dict):
        for comp_name in config.LIVE_SCORE_COMPETITIONS:
            if comp_name in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
                continue
            cached = persisted.get(comp_name)
            if should_use_persisted_table(cached, force_refresh):
                results[comp_name] = _sanitize_real_standings(cached, comp_name) or cached
                continue
            if force_refresh:
                _clear_standings_cache(comp_name)
                _clear_leaders_cache(comp_name)
            table = _compute_standings_from_history(comp_name)
            if table is not None:
                results[comp_name] = table
            else:
                fallback = _build_fallback_standings(comp_name)
                if fallback is not None:
                    results[comp_name] = fallback
                else:
                    results[comp_name] = {
                        "competition": comp_name,
                        "updated_at": now_utc,
                        "groups": [{"name": "Overall", "entries": []}],
                        "source": "placeholder",
                    }
    else:
        for comp_name in config.LIVE_SCORE_COMPETITIONS:
            if comp_name in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
                continue
            if force_refresh:
                _clear_standings_cache(comp_name)
                _clear_leaders_cache(comp_name)
            table = _compute_standings_from_history(comp_name)
            if table is not None:
                results[comp_name] = table
            else:
                fallback = _build_fallback_standings(comp_name)
                if fallback is not None:
                    results[comp_name] = fallback
                else:
                    results[comp_name] = {
                        "competition": comp_name,
                        "updated_at": now_utc,
                        "groups": [{"name": "Overall", "entries": []}],
                        "source": "placeholder",
                    }
    for alias in config.MLS_TABLE_VIEW_ALIASES:
        if alias in results:
            continue
        if force_refresh:
            _clear_standings_cache(alias)
        table = _compute_standings_from_history(alias)
        if table:
            results[alias] = table
    return jsonify({"ok": True, "tables": results, "total": len(results)})


@app.get("/api/competition-data")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_competition_data():
    """Return full competition data in World Cup format for any competition.

    Works for *all* competitions tracked by the system (leagues, cups,
    World Cup).  The response mirrors ``/api/world-cup``:

    * ``knockout`` — ``{stage_key: [matches]}}`` with predicted winners
      from the projection / simulation.
    * ``odds_knockout`` — same topology, each match's ``winner`` is the
      team with higher ``prob_home`` / ``prob_away``.
    * ``real_knockout`` — winners only from completed (``post``) games;
      unplayed matches show both teams but ``winner: null``.
    * ``group_tables`` / ``table`` — group / league phase standings when
      applicable.
    * ``champion`` — aggregate most-likely champion from simulation.
    * ``simulations_run`` — how many tournament simulations were aggregated.
    * ``winner_probabilities`` — per-team champion probability map.
    * ``cup_format``, ``knockout_rounds`` — cup/stage metadata.

    For regular leagues (no knockout bracket) the ``knockout`` / odds /
    real fields are omitted and only ``table`` is returned.

    Query params:
        competition  -- required, e.g. "Europe/Champions League",
                        "England/Premier League", "International/World Cup"
    """
    comp = request.args.get("competition", "").strip()
    if not comp:
        return jsonify({"ok": False, "error": "Missing 'competition' parameter"}), 400
    if comp not in config.LIVE_SCORE_COMPETITIONS:
        return jsonify({"ok": False, "error": f"Unknown competition: {comp}"}), 400

    # ── Special case: World Cup uses its dedicated projection file ──
    if comp == "International/World Cup":
        return api_world_cup()

    # ── Gather basic building blocks ──────────────────────────────
    table = _compute_standings_from_history(comp)
    cup_format = config._CUP_FORMATS.get(comp)
    ko_framework = _build_knockout_framework(comp)

    result = {
        "ok": True,
        "competition": comp,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    if table is not None:
        result["table"] = table
    if cup_format:
        result["cup_format"] = cup_format
    if ko_framework:
        result["knockout_rounds"] = ko_framework

    # ── Simulation / champion data from projected cup brackets ────
    bracket_data = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    if isinstance(bracket_data, dict):
        comps = bracket_data.get("competitions", bracket_data)
        if isinstance(comps, dict) and comp in comps:
            entry = comps[comp]
            if isinstance(entry, dict):
                for key in ("champion", "simulations_run", "winner_probabilities", "sim_index"):
                    if key in entry:
                        result[key] = entry[key]
                if "generated_at_utc" in bracket_data:
                    result["generated_at_utc"] = bracket_data["generated_at_utc"]

    # ── Knockout bracket (WC-style) ──────────────────────────────
    matches = _gather_competition_cup_matches(comp)

    if not matches:
        if comp in config._CUP_FORMATS:
            result = _enrich_tournament_payload(comp, result)
        result = _attach_projected_winner_fields(comp, result)
        return jsonify(result)

    knockout, odds_knockout, real_knockout = _build_cup_knockout_payload(matches, comp)
    result["knockout"] = knockout
    result["odds_knockout"] = odds_knockout
    result["real_knockout"] = real_knockout

    if comp in config._CUP_FORMATS:
        result = _enrich_tournament_payload(comp, result)

    result = _attach_projected_winner_fields(comp, result)
    return jsonify(result)


def _attach_projected_winner_fields(comp, result):
    """Add World Cup-style winner probability fields when not already present."""
    if not isinstance(result, dict) or result.get("winner_probabilities"):
        return result
    winner_payload = _build_winner_probability_payload(_load_projected_competition_table(comp))
    for key in ("winner_probabilities", "champion", "simulations_run"):
        if winner_payload.get(key) is not None:
            result[key] = winner_payload[key]
    return result


def _build_mls_api_payload():
    """Shared MLS payload for ``/api/league-tables?mode=mls``."""
    projected = _load_projected_tables(config.MLS_PROJECTED_TABLE_FILE)
    last_refresh = (
        _file_mtime_utc(config.MLS_PROJECTED_TABLE_FILE)
        if os.path.exists(config.MLS_PROJECTED_TABLE_FILE)
        else None
    )

    tables = dict(projected.get("tables") or {})
    leagues = set(projected.get("leagues") or [])

    season_data = _load_current_season_tables()
    if season_data:
        for comp, rows in (season_data.get("tables") or {}).items():
            if comp not in tables or not tables.get(comp):
                tables[comp] = rows
            leagues.add(comp)

    for comp in config.MLS_DATASET_COMPETITIONS:
        leagues.add(comp)

    data = {"leagues": sorted(leagues), "tables": tables}
    _fill_placeholder_tables(data)
    data["leagues"] = sorted(set(data.get("leagues") or []) | set(config.MLS_DATASET_COMPETITIONS))

    payload = {
        "leagues": data.get("leagues") or [],
        "tables": data.get("tables") or {},
        "bracket": _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE),
        "fixtures": _load_all_fixtures_by_competition(config.MLS_UPCOMING_FILE),
        "last_prediction_refresh": last_refresh,
        "mls_winners_odds": _build_mls_winners_odds_bundle(),
    }
    _normalize_mls_conference_tables(payload)
    return payload


def _enrich_league_data_mls_fields(comp, payload):
    """Attach MLS Cup bracket, all MLS winner-odds views, and fixtures."""
    if not str(comp or "").startswith("United States/MLS"):
        return payload

    mls_winners = _build_mls_winners_odds_bundle()
    if mls_winners:
        payload["mls_winners_odds"] = mls_winners

    from competition_rules import resolve_competition_query

    base_comp, view = resolve_competition_query(comp)
    view_key_map = {
        "shield": "supporters_shield",
        "east": "eastern_conference",
        "west": "western_conference",
    }
    if comp == config.MLS_CUP_COMPETITION:
        view_key = "mls_cup"
    elif view:
        view_key = view_key_map.get(view)
    else:
        view_key = None

    if view_key and view_key in mls_winners:
        view_payload = mls_winners[view_key]
        for key in ("winner_probabilities", "winners_odds", "champion", "simulations_run"):
            if view_payload.get(key) is not None:
                payload[key] = view_payload[key]

    bracket = _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE)
    if isinstance(bracket, dict) and bracket:
        payload["bracket"] = bracket
        cup_data = bracket.get("mls_cup") or {}
        if cup_data.get("winner"):
            payload["mls_cup_winner"] = cup_data.get("winner")

    if not payload.get("fixtures"):
        from competition_rules import resolve_competition_query

        base_comp, _view = resolve_competition_query(comp)
        fixture_comp = base_comp if base_comp == "United States/MLS" else comp
        for csv_path in (config.MLS_UPCOMING_FILE, config.GLOBAL_UPCOMING_FILE):
            try:
                rows, _, _ = _load_upcoming_rows(csv_path, date_range="all")
            except Exception:
                continue
            comp_fixtures = [
                r for r in rows
                if r.get("competition") in (comp, fixture_comp, "United States/MLS")
            ]
            if comp_fixtures:
                payload["fixtures"] = comp_fixtures
                break
    return payload


@app.get("/api/league-tables")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_league_tables():
    """Return projected league tables (and MLS playoff bracket when requested).

    Includes a ``last_prediction_refresh`` field with the file mtime in UTC.
    Also includes all scheduled fixtures (``fixtures``) for the requested mode,
    grouped by competition, so the frontend can show the full season schedule
    for each league alongside the projected standings.
    """
    mode = str(request.args.get("mode", "global")).strip().lower()

    if mode == "mls":
        data = _build_mls_api_payload()
        return jsonify({"ok": True, **data})
    if mode == "cups":
        csv_path = config.CUP_PROJECTED_TABLE_FILE
        data = _load_projected_tables(csv_path)
        if not data.get("leagues"):
            data["leagues"] = list(config._CUP_FORMATS)
        brackets = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
        data["last_prediction_refresh"] = _file_mtime_utc(csv_path)
        known_cups = set(data.get("leagues") or [])
        for comp_name in config._CUP_FORMATS:
            if comp_name not in known_cups:
                known_cups.add(comp_name)
        # World Cup is temporarily excluded from the cups competitions API.
        known_cups.discard("International/World Cup")
        data["leagues"] = sorted(known_cups)
        if isinstance(data.get("tables"), dict):
            data["tables"].pop("International/World Cup", None)
        cup_formats = {}
        if isinstance(brackets, dict):
            comps = brackets.get("competitions", brackets)
            if isinstance(comps, dict):
                for comp_name in comps:
                    fmt = config._CUP_FORMATS.get(comp_name)
                    if fmt:
                        cup_formats[comp_name] = fmt
        for comp_name in data.get("leagues") or []:
            if comp_name not in cup_formats:
                fmt = config._CUP_FORMATS.get(comp_name)
                if fmt:
                    cup_formats[comp_name] = fmt
        odds_bracket = _compute_odds_bracket()
        knockout_frameworks = {}
        for comp_name in list(cup_formats.keys()) + (data.get("leagues") or []):
            kf = _build_knockout_framework(comp_name)
            if kf:
                knockout_frameworks[comp_name] = kf
        fixtures = _load_all_fixtures_by_competition(config.CUP_UPCOMING_FILE)
        return jsonify({
            "ok": True, **data,
            "cup_brackets": brackets, "cup_formats": cup_formats,
            "odds_bracket": odds_bracket,
            "knockout_frameworks": knockout_frameworks,
            "fixtures": fixtures,
        })
    if mode == "extra":
        data = _build_extra_api_payload()
        data["last_prediction_refresh"] = (
            _file_mtime_utc(config.EXTRA_PROJECTED_TABLE_FILE)
            if os.path.exists(config.EXTRA_PROJECTED_TABLE_FILE)
            else None
        )
        fixtures = _load_all_fixtures_by_competition(config.EXTRA_UPCOMING_FILE)
        return jsonify({"ok": True, **data, "fixtures": fixtures})
    data = _build_global_api_payload()
    data["last_prediction_refresh"] = (
        _file_mtime_utc(config.GLOBAL_PROJECTED_TABLE_FILE)
        if os.path.exists(config.GLOBAL_PROJECTED_TABLE_FILE)
        else None
    )
    fixtures = _load_all_fixtures_by_competition(config.GLOBAL_UPCOMING_FILE)
    excluded = config.LEAGUE_API_EXCLUDED_COMPETITIONS
    if isinstance(fixtures, dict):
        fixtures = {k: v for k, v in fixtures.items() if k not in excluded}
    return jsonify({"ok": True, **data, "fixtures": fixtures})


@app.get("/api/league-data/<path:competition>")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_league_data(competition):
    """Return consolidated data for a single league / cup / competition.

    Uses a unified schema across all competitions (see ``league_data.py``):

    .. code-block:: json

        {"ok": true, "competition": "...",
         "format": {"standings_layout": "...", "tiebreaker": "gd|h2h", "notes": [...]},
         "predicted": {
           "table": [...],
           "groups": [{"name": "...", "entries": [...]}] | null,
           "winner": {"champion": "...", "probabilities": {...}, "simulations_run": 200},
           "winners_odds": [...],
           "position_odds": {"simple": {...}, "detailed": [...], "detailed_same_as_simple": false}
         },
         "real": {"standings": {"groups": [...], "standings_layout": "...", ...}},
         "bracket": {"projected": {...}, "knockout": {...}, "odds_knockout": {...}},
         "fixtures": [...]}

    For ``United States/MLS``, ``bracket.projected`` is the full MLS Cup playoff
    bracket JSON when available, and ``mls_winners_odds.mls_cup`` carries Cup
    winner probabilities (also mirrored under ``predicted.mls_cup``).
    """
    comp = competition.strip()

    if comp in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
        return jsonify({
            "ok": False,
            "error": f"Competition not available in league APIs: {comp}",
        }), 404

    from league_data import _load_league_data_from_cache
    cached = _load_league_data_from_cache(comp)
    if cached is not None:
        return jsonify(cached)

    return jsonify(build_league_data_payload(comp))


@app.get("/api/stats")
def api_stats():
    """Return overall site stats: accuracy, league count, last refresh time."""
    try:
        rows, stats, league_stats = _load_upcoming_rows(config.GLOBAL_UPCOMING_FILE, "global")
        accuracy_pct = (stats or {}).get("accuracy_pct", 0.0)
    except Exception:
        accuracy_pct = 0.0
    refreshed = get_last_pipeline_run()
    refreshed_at = refreshed.isoformat() if refreshed else None
    return jsonify({
        "ok": True,
        "accuracy_pct": accuracy_pct,
        "league_count": 18,
        "refreshed_at": refreshed_at,
    })


@app.get("/api/league-leaders")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_league_leaders():
    """Return predicted and real (live) leaders for every league and cup.

    Response:

    .. code-block:: json

        {"ok": true, "leagues": [{"competition": "...",
          "predicted_winner": "...", "predicted_winner_odds": 0.0,
          "current_leader": "...", "leader_source": "real"|"predicted"},
          ...],
         "cups": [{"competition": "...",
          "predicted_winner": "...", "predicted_winner_odds": 0.0}, ...]}
    """
    leagues = []
    cups = []

    # Competition names that are aliases — skip to avoid duplicates
    _COMPETITION_ALIASES = {
        "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
    }

    # ── Predicted winners from projected table CSVs ───────────────
    table_sources = [
        ("global", config.GLOBAL_PROJECTED_TABLE_FILE),
        ("mls", config.MLS_PROJECTED_TABLE_FILE),
        ("extra", config.EXTRA_PROJECTED_TABLE_FILE),
        ("cups", config.CUP_PROJECTED_TABLE_FILE),
    ]

    # Collect MLS sub-competition data for nested entry
    mls_supporters = {}
    mls_east = {}
    mls_west = {}

    for source_mode, csv_path in table_sources:
        proj = _load_projected_tables(csv_path)
        comp_list = proj.get("leagues") or []
        for comp_name in comp_list:
            if comp_name in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
                continue
            # MLS sub-competitions — collect separately
            if comp_name.startswith("United States/MLS"):
                comp_tbl = proj.get("tables", {}).get(comp_name, [])
                pos1 = next((r for r in comp_tbl if r.get("position") == 1), None)
                if pos1:
                    if "Supporters Shield" in comp_name:
                        mls_supporters = pos1
                    elif "Eastern Conference" in comp_name:
                        mls_east = pos1
                    elif "Western Conference" in comp_name:
                        mls_west = pos1
                continue
            if comp_name in _COMPETITION_ALIASES:
                continue
            comp_tbl = proj.get("tables", {}).get(comp_name, [])
            predicted = None
            for row in comp_tbl:
                if row.get("position") == 1:
                    predicted = {
                        "winner": row.get("team", ""),
                        "odds": row.get("win_league_pct"),
                    }
                    break
            if not predicted:
                if source_mode == "cups":
                    if comp_name not in config._CUP_FORMATS and comp_name != "International/World Cup":
                        continue
                else:
                    continue
            entry = {
                "competition": comp_name,
                "predicted_winner": predicted["winner"] if predicted else "—",
                "predicted_winner_odds": round(predicted["odds"], 1) if predicted and predicted["odds"] is not None else None,
            }
            if source_mode == "cups":
                cups.append(entry)
            else:
                leagues.append(entry)

    # Add unified MLS entry
    mls_bracket = _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE)
    mls_cup_winner = None
    mls_cup_odds = None
    if isinstance(mls_bracket, dict):
        cup_data = mls_bracket.get("mls_cup") or {}
        mls_cup_winner = cup_data.get("winner")
    mls_entry = {
        "competition": "United States/MLS",
        "predicted_winner": (mls_supporters.get("team") if mls_supporters else None) or "—",
        "predicted_winner_odds": round(mls_supporters.get("win_league_pct"), 1) if mls_supporters and mls_supporters.get("win_league_pct") is not None else None,
        "east_leader": (mls_east.get("team") if mls_east else None) or None,
        "west_leader": (mls_west.get("team") if mls_west else None) or None,
        "mls_cup_winner": mls_cup_winner or None,
    }
    leagues.append(mls_entry)

    # ── Cup champions from cup brackets JSON (simulation data) ────
    bracket_data = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    if isinstance(bracket_data, dict):
        comps = bracket_data.get("competitions", bracket_data)
        if isinstance(comps, dict):
            for comp_name, comp_entry in comps.items():
                if not isinstance(comp_entry, dict):
                    continue
                if comp_name in _COMPETITION_ALIASES:
                    continue
                champion = comp_entry.get("champion")
                winner_probs = comp_entry.get("winner_probabilities") or {}
                prob = winner_probs.get(champion, None) if champion else None
                # Avoid duplicating entries already from cup table CSV
                if not any(c["competition"] == comp_name for c in cups):
                    cups.append({
                        "competition": comp_name,
                        "predicted_winner": champion or "—",
                        "predicted_winner_odds": round(prob * 100, 1) if prob is not None else None,
                    })
                else:
                    # Update odds with simulation data when available
                    if prob is not None:
                        for c in cups:
                            if c["competition"] == comp_name and champion:
                                c["predicted_winner"] = champion
                                c["predicted_winner_odds"] = round(prob * 100, 1)

    # World Cup temporarily excluded from cups competitions API.
    # wc_file = os.path.join(config.PROJECT_DIR, "Data", "Predictions", "world_cup_projection.json")
    # if os.path.exists(wc_file):
    #     try:
    #         with open(wc_file, "r") as f:
    #             wc = json.load(f)
    #         wc_champion = wc.get("champion")
    #         wc_probs = wc.get("winner_probabilities") or {}
    #         wc_prob = wc_probs.get(wc_champion, None) if wc_champion else None
    #         if not any(c["competition"] == "International/World Cup" for c in cups):
    #             cups.append({
    #                 "competition": "International/World Cup",
    #                 "predicted_winner": wc_champion or "—",
    #                 "predicted_winner_odds": round(wc_prob * 100, 1) if wc_prob is not None else None,
    #             })
    #     except Exception:
    #         pass

    # ── Include all known competitions with predictions or real tables ──
    cup_set = set(c["competition"] for c in cups)
    league_names = set(e["competition"] for e in leagues)

    # Add any cup competitions from config.LIVE_SCORE_COMPETITIONS not yet listed
    for comp_name in config.LIVE_SCORE_COMPETITIONS:
        if comp_name in cup_set or comp_name in league_names:
            continue
        if comp_name in _COMPETITION_ALIASES:
            continue
        if comp_name in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
            continue
        if comp_name in config._CUP_FORMATS:
            cups.append({
                "competition": comp_name,
                "predicted_winner": "—",
                "predicted_winner_odds": None,
            })
        else:
            leagues.append({
                "competition": comp_name,
                "predicted_winner": "—",
                "predicted_winner_odds": None,
            })

    # ── Real leaders from live score history / ESPN ──────────────
    cup_set = set(c["competition"] for c in cups)
    for entry in leagues:
        comp = entry["competition"]
        if comp in cup_set:
            continue
        # MLS real leaders — read conference groups from the unified MLS table
        if comp == "United States/MLS":
            real = _compute_standings_from_history("United States/MLS")
            if real and isinstance(real, dict):
                for g in (real.get("groups") or []):
                    name = str(g.get("name", "")).strip()
                    leader = (g.get("entries") or [{}])[0].get("team", "")
                    if not leader:
                        continue
                    if name == "Eastern Conference":
                        entry["east_leader"] = leader
                    elif name == "Western Conference":
                        entry["west_leader"] = leader
                    elif name == "Supporters Shield":
                        entry["current_leader"] = leader
                        entry["leader_source"] = "real"
            if "leader_source" not in entry:
                entry["current_leader"] = entry.get("predicted_winner") if entry.get("predicted_winner") and entry["predicted_winner"] != "—" else None
                entry["leader_source"] = "predicted"
            continue
        real = _compute_standings_from_history(comp)
        if real and isinstance(real, dict):
            for g in (real.get("groups") or []):
                if g.get("entries"):
                    entry["current_leader"] = g["entries"][0].get("team", "")
                    entry["leader_source"] = "real"
                    break
            else:
                if entry.get("predicted_winner") and entry["predicted_winner"] != "—":
                    entry["current_leader"] = entry["predicted_winner"]
                    entry["leader_source"] = "predicted"
                else:
                    entry["current_leader"] = None
                    entry["leader_source"] = "predicted"
        else:
            if entry.get("predicted_winner") and entry["predicted_winner"] != "—":
                entry["current_leader"] = entry["predicted_winner"]
                entry["leader_source"] = "predicted"
            else:
                entry["current_leader"] = None
                entry["leader_source"] = "predicted"

    leagues = [e for e in leagues if _is_league_api_competition(e.get("competition"))]

    return jsonify({
        "ok": True,
        "leagues": leagues,
        "cups": cups,
    })


@app.get("/tactics")


@app.get("/tactics")
def tactics():
    """Render the tactics whiteboard page."""
    return render_template("tactics.html")


@app.get("/players")
def players():
    """Render the players/top scorers page."""
    return render_template("players.html")


@app.get("/api/scorers")
def api_scorers():
    """Return current season top scorers by competition."""
    if not os.path.exists(config.TOP_SCORERS_FILE):
        return jsonify({"ok": False, "error": "Scorers data not available", "competitions": {}}), 404
    
    try:
        with open(config.TOP_SCORERS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return jsonify({"ok": False, "error": "Could not load scorers", "competitions": {}}), 500
    
    competitions = data.get("competitions", {})
    last_updated = data.get("last_updated_utc", "Unknown")
    
    return jsonify({
        "ok": True,
        "last_updated_utc": last_updated,
        "competitions": competitions,
        "available_leagues": sorted(competitions.keys()),
    })


# ─────────────────────────────────────────────────────────────────────
# Helper: serve a JSON info file (changes / roadmap / upcoming)
# ─────────────────────────────────────────────────────────────────────

INFO_ENDPOINTS = {
    "changes": config.INFO_CHANGES_FILE,
    "roadmap": config.INFO_ROADMAP_FILE,
    "upcoming": config.INFO_UPCOMING_FILE,
}


def _serve_info_file(name):
    filepath = INFO_ENDPOINTS.get(name)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"ok": False, "error": f"No {name} data available"}), 404
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return jsonify({"ok": False, "error": f"Could not load {name} data"}), 500
    entries = data if isinstance(data, list) else data.get("entries", [])
    return jsonify({"ok": True, "entries": entries})


@app.get("/api/key-errors")
def api_key_errors():
    """Return the KeyError log for diagnosing missing mappings or broken lookups."""
    with _key_error_log_lock:
        return jsonify(list(_key_error_log))


@app.get("/api/info/changes")
def api_info_changes():
    return _serve_info_file("changes")


@app.get("/api/info/roadmap")
def api_info_roadmap():
    return _serve_info_file("roadmap")


@app.get("/api/info/upcoming")
def api_info_upcoming():
    return _serve_info_file("upcoming")


@app.get("/graphics/<path:filename>")
def serve_graphic(filename):
    """Serve assets from Website/graphics for logos and other static artwork."""
    return send_from_directory(config.GRAPHICS_DIR, filename)


if __name__ == "__main__":
    import argparse
    import socket

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind to")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode (with auto-reload; spawns a reloader process)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable Werkzeug file-change reloader (requires --debug)",
    )
    args = parser.parse_args()

    if args.reload and not args.debug:
        raise SystemExit("--reload requires --debug")

    use_reloader = bool(args.debug and args.reload)

    if args.host == "0.0.0.0":
        try:
            s = socket.socket(socket.AF_INET, socket.sock_dgram)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            print(f"\n * Connect from other devices at: http://{ip}:{args.port}\n")
        except Exception:
            pass

    if not config.MUTATION_API_TOKEN:
        print("[startup] WARNING: no mutation auth configured — write-capable API endpoints are disabled!")

    start_live_score_poller()

    # Start APNs background worker (does nothing if env vars not set)
    start_apns_worker()

    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        use_reloader=use_reloader,
    )
