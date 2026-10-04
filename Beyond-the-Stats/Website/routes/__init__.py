"""
Routes package initialization.
Exports all domain routers and the master registration helper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

from .pages import pages_bp, pages_router
from .predictions import predictions_bp, predictions_router
from .leagues import leagues_bp, leagues_router
from .live_scores import live_scores_bp, live_scores_router
from .admin import admin_bp, admin_router

__all__ = [
    "pages_bp",
    "pages_router",
    "predictions_bp",
    "predictions_router",
    "leagues_bp",
    "leagues_router",
    "live_scores_bp",
    "live_scores_router",
    "admin_bp",
    "admin_router",
    "register_all_routers",
    "register_all_blueprints",
]


def register_all_routers(app: FastAPI) -> None:
    """Register all domain routers on the FastAPI application instance."""
    app.include_router(pages_router)
    app.include_router(predictions_router)
    app.include_router(leagues_router)
    app.include_router(live_scores_router)
    app.include_router(admin_router)


# Alias for backwards compatibility
register_all_blueprints = register_all_routers

