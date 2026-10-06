"""
Admin, pipeline status/logs, legal, help, info, and system diagnostics routes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .compat import Blueprint, Response, current_app, jsonify, request

import config
import pipeline_log
from legal_docs import get_legal_document, list_legal_documents
from predictions import (
    _load_json_payload,
    _run_full_pipeline_once,
    get_last_pipeline_run,
)
from team_mappings import (
    build_predictor_teams_payload,
    build_unmapped_espn_payload,
)
from .helpers import (
    get_key_error_log,
    load_redeem_code_entries,
    mutation_authorized,
    parse_redeem_code,
)

admin_bp = Blueprint("admin", __name__)
admin_router = admin_bp


# ── Legal Documents ──────────────────────────────────────────────────

@admin_bp.get("/api/legal")
def api_legal_index():
    """List Privacy Policy, Terms of Service, and subscription disclosure endpoints."""
    return jsonify({"ok": True, "documents": list_legal_documents()})


@admin_bp.get("/api/legal/<doc_id>")
def api_legal_document(doc_id):
    """Return a legal document body (privacy, terms, or subscriptions)."""
    doc = get_legal_document(doc_id)
    if not doc:
        return jsonify({"ok": False, "error": f"Unknown legal document: {doc_id}"}), 404
    return jsonify(doc)


# ── Team Mappings Diagnostics ────────────────────────────────────────

@admin_bp.get("/api/team-mappings/unmapped")
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


@admin_bp.get("/api/team-mappings/predictor-teams")
def api_team_mappings_predictor_teams():
    """List canonical team names used by global, MLS, and extra predictors."""
    return jsonify(build_predictor_teams_payload())


# ── Help & API Catalog ───────────────────────────────────────────────

@admin_bp.get("/api/help")
def api_help():
    """Return a listing of every /api/ route with a short description."""
    routes = [
        ("/api/teams", "GET", "List selectable teams for a given mode (?mode=global|mls|extra)"),
        ("/leagues", "GET", "Leagues & cups hub (?competition= opens detail with tables, odds, upcoming)"),
        ("/api/teams/catalog", "GET", "All unique teams in app-available leagues (?competition=)"),
        ("/leagues", "GET", "Leagues & cups hub with tile navigation; ?competition= opens detail view"),
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
        ("/api/cup-data", "GET", "List cup competitions for /api/cup-data/<competition>"),
        ("/api/cup-data/<competition>", "GET", "Unified cup payload (format_style + stage-reach position odds)"),
        ("/api/league-data/<competition>", "GET", "Unified league payload (tables + position odds); prefer cup-data for cups"),
        ("/api/competition-data", "GET", "Unified WC-format data for any competition (?competition=)"),
        ("/api/league-tables", "GET", "Projected league tables for all competitions"),
        ("/api/real-tables", "GET", "Live/recent real league standings"),
        ("/api/league-leaders", "GET", "Predicted winner + current leader per competition"),
        ("/api/live-scores", "GET", "Currently live matches with scores"),
        ("/api/live-score-history", "GET", "Recent live score history"),
        ("/api/past-live-scores", "GET", "Alias for /api/live-score-history (SQLite-backed)"),
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
        ("/live-activities/register", "POST", "Register a Live Activity push token for a match (app alias)"),
        ("/live-activities/unregister", "POST", "Remove a Live Activity registration (app alias)"),
        ("/api/redeem", "GET/POST", "Redeem a promo code (?code= or JSON body)"),
        ("/api/info/changes", "GET", "App changes changelog entries"),
        ("/api/info/roadmap", "GET", "App planned features/roadmap"),
        ("/api/info/upcoming", "GET", "App upcoming features"),
        ("/api/help", "GET", "This listing"),
    ]
    return jsonify({"ok": True, "routes": routes})


@admin_bp.get("/api/help/all")
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

        enc = comp_name.replace("/", "%2F").replace(" ", "+")
        # ── Predicted data ──────────────────────────────────────
        predicted = {
            "upcoming": f"/api/upcoming/{'world-cup' if 'World Cup' in comp_name else 'cups' if is_cup else 'global'}",
            "league_table": f"/api/league-tables?mode={'cups' if is_cup else 'global'}",
            "cup_bracket": f"/api/cup-bracket?competition={enc}" if is_cup else None,
            "cup_data": f"/api/cup-data/{comp_name}" if is_cup else None,
            "league_data": None if is_cup else f"/api/league-data/{comp_name}",
        }
        predicted["leader"] = "/api/league-leaders"
        base["predicted"] = {k: v for k, v in predicted.items() if v is not None}

        # ── Real data ───────────────────────────────────────────
        real = {
            "real_table": f"/api/real-tables?competition={enc}",
            "real_cup_data": f"/api/real-cup-data?competition={enc}" if is_cup else None,
        }
        real["competition_data"] = f"/api/competition-data?competition={enc}"
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


# ── Pipeline & Maintenance ───────────────────────────────────────────

@admin_bp.get("/api/last-refresh")
def api_last_refresh():
    """Return the timestamp of the last pipeline refresh."""
    refreshed_at = get_last_pipeline_run()
    refreshed_at = refreshed_at.isoformat() if refreshed_at else None
    return jsonify({"ok": True, "last_refresh_utc": refreshed_at})


@admin_bp.get("/api/pipeline/status")
def api_pipeline_status():
    """Return pipeline health: last run, per-step pass/fail, and backend metadata."""
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
        elif (
            step_name.startswith("settle")
            or step_name.startswith("update")
            or step_name.startswith("track")
            or step_name.startswith("sync")
        ):
            key = "post"
        sub_pipelines[key].append({"step": step_name, "ok": bool(passed)})

    refreshed = get_last_pipeline_run()
    log_stats = pipeline_log.log_stats()
    apns_configured = bool(
        config.APNS_KEY_ID
        and config.APNS_TEAM_ID
        and config.APNS_AUTH_KEY_PATH
        and os.path.exists(config.APNS_AUTH_KEY_PATH or "")
    )
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


@admin_bp.get("/api/pipeline/logs")
def api_pipeline_logs():
    """Return persisted pipeline terminal output from the latest run."""
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
        return Response(
            payload.get("text") or "",
            mimetype="text/plain; charset=utf-8",
        )
    return jsonify(payload)


@admin_bp.post("/api/refresh")
def api_refresh():
    """Trigger a background pipeline refresh (non-blocking when BackendServer is running)."""
    if not mutation_authorized():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    if not config.PIPELINE_ENABLED:
        return jsonify({"ok": False, "error": "Pipeline disabled (set PIPELINE_ENABLED=1 to enable)"}), 403

    refresh_fn = current_app.config.get("_backend_refresh")
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


@admin_bp.post("/api/retrain")
def api_retrain():
    """Force a full model retrain."""
    if not mutation_authorized():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    if not config.PIPELINE_ENABLED:
        return jsonify({"ok": False, "error": "Pipeline disabled (set PIPELINE_ENABLED=1 to enable)"}), 403

    refresh_fn = current_app.config.get("_backend_refresh")
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


# ── Promo Code Redemption ────────────────────────────────────────────

@admin_bp.route("/api/redeem", methods=["GET", "POST"])
def api_redeem():
    """Redeem a promo code."""
    payload = request.get_json(silent=True) or {}
    raw = payload.get("code", "")
    if raw in (None, "") and request.args.get("code"):
        raw = request.args.get("code", "")
    if not isinstance(raw, str):
        return jsonify({"ok": False, "error": "code must be a string"}), 400

    code = parse_redeem_code(raw)
    if code is None:
        return jsonify({
            "ok": False,
            "error": "missing or invalid code",
            "detail": "code must be letters and digits only (case-sensitive)",
        }), 400
    if not code or len(code) > 200:
        return jsonify({"ok": False, "error": "missing or invalid code"}), 400

    entries = load_redeem_code_entries()
    if not entries:
        return jsonify({
            "ok": False,
            "error": "redeem codes unavailable",
            "detail": f"Expected JSON list at {config.REDEEM_CODES_FILE}",
        }), 503

    for entry in entries:
        entry_code = parse_redeem_code(entry.get("code", ""))
        if not entry_code:
            continue
        if entry_code == code:
            return jsonify({
                "ok": True,
                "value": entry.get("value", True),
            })

    return jsonify({"ok": False, "error": "unknown code"})


# ── App Info & Diagnostic Logs ────────────────────────────────────────

INFO_ENDPOINTS = {
    "changes": config.INFO_CHANGES_FILE,
    "roadmap": config.INFO_ROADMAP_FILE,
    "upcoming": config.INFO_UPCOMING_FILE,
}


def _serve_info_file(name: str):
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


@admin_bp.get("/api/key-errors")
def api_key_errors():
    """Return the KeyError log for diagnosing missing mappings or broken lookups."""
    return jsonify(get_key_error_log())


@admin_bp.get("/api/info/changes")
def api_info_changes():
    return _serve_info_file("changes")


@admin_bp.get("/api/info/roadmap")
def api_info_roadmap():
    return _serve_info_file("roadmap")


@admin_bp.get("/api/info/upcoming")
def api_info_upcoming():
    return _serve_info_file("upcoming")


@admin_bp.get("/api/test-sync")
def api_test_sync():
    """Diagnostic endpoint to verify file synchronization and FastAPI compatibility."""
    return jsonify({"status": "ok", "sync_test": True, "server": "FastAPI"})