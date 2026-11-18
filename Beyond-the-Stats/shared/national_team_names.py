"""Canonical national-team names shared by the national pipeline and the website.

ESPN's display names ("Türkiye", "Korea Republic", "Côte d'Ivoire", ...) and
the pipeline's names ("Turkey", "South Korea", "Ivory Coast", ...) used to
differ: the live poller stored ESPN's spelling, the pipeline its own, so one
country's games split across two names (e.g. Türkiye had 2 of 6 UEFA Nations
League games). Both sides now apply this table to national-team competitions.
"""
from __future__ import annotations

NATIONAL_TEAM_ALIASES = {
    "USA": "United States",
    "US": "United States",
    "Czech Republic": "Czechia",
    "Türkiye": "Turkey",
    "Turkiye": "Turkey",
    "Korea Republic": "South Korea",
    "Bosnia and Herzegovina": "Bosnia-Herzegovina",
    "Côte d'Ivoire": "Ivory Coast",
    "Cote d'Ivoire": "Ivory Coast",
}


def canonical_national_team(name: str) -> str:
    text = str(name or "").strip()
    return NATIONAL_TEAM_ALIASES.get(text, text)
