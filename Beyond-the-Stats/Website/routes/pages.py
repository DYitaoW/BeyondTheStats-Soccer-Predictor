"""
HTML page rendering routes and static page redirects.
"""

from __future__ import annotations

import os
from flask import Blueprint, redirect, render_template, send_from_directory

import config
from team_utils import _team_name_for_display
from predictions import (
    _get_static_predictions,
    _load_teams_from_team_data,
    get_context,
    pm_extra,
    pm_global,
    pm_mls,
)

pages_bp = Blueprint("pages", __name__)
pages_router = pages_bp


def _render_site_page(template_name: str, active_page: str):
    """Render a website tab page with shared team lists for forms and datalists."""
    page_routes = {
        "home": "/",
        "global": "/leagues",
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
        active_page=active_page,
        page_routes=page_routes,
        upcoming_leagues=upcoming_leagues,
        table_leagues=table_leagues,
        teams=global_display_teams,
        mls_teams=mls_display_teams,
        extra_teams=extra_display_teams,
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


@pages_bp.get("/")
def index():
    """Render the home page with shared team context."""
    return _render_site_page("home.html", active_page="home")


@pages_bp.get("/upcoming-matches")
def upcoming_matches():
    """Redirect legacy upcoming tab to the Leagues hub."""
    return redirect("/leagues")


@pages_bp.get("/cups")
def cups_page():
    """Redirect the legacy cups tab to the merged Leagues page."""
    return redirect("/leagues")


@pages_bp.get("/leagues")
def leagues():
    """Render the merged Leagues page (projected tables + cups)."""
    return _render_site_page("leagues.html", active_page="leagues")


@pages_bp.get("/head-to-head")
def head_to_head():
    """Render the head-to-head tab page."""
    return _render_site_page("head_to_head.html", active_page="h2h")


@pages_bp.get("/league-tables")
def league_tables():
    """Redirect the legacy projected league tables page to the merged Leagues page."""
    return redirect("/leagues")


@pages_bp.get("/world-cup")
def world_cup():
    """Render the World Cup tab page."""
    return _render_site_page("world_cup.html", active_page="world-cup")


@pages_bp.get("/about")
def about():
    """Render the about tab page."""
    return _render_site_page("about.html", active_page="about")


@pages_bp.get("/privacy")
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


@pages_bp.get("/terms")
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


@pages_bp.get("/subscriptions")
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


@pages_bp.get("/draftit/about")
def draftit_about():
    """Render the Draft It! app Privacy Policy / about page."""
    return _render_draftit_page("draftit_privacy")


@pages_bp.get("/draftit/privacy")
def draftit_privacy():
    """Render the Draft It! Privacy Policy page."""
    return _render_draftit_page("draftit_privacy")


@pages_bp.get("/tactics")
def tactics():
    """Render the tactics whiteboard page."""
    return render_template("tactics.html")


@pages_bp.get("/players")
def players():
    """Render the players/top scorers page."""
    return render_template("players.html")


@pages_bp.get("/graphics/<path:filename>")
def serve_graphic(filename):
    """Serve assets from Website/graphics for logos and other static artwork."""
    return send_from_directory(config.GRAPHICS_DIR, filename)

