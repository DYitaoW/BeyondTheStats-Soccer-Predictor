"""Canonical competition id helpers and legacy aliases."""
from __future__ import annotations

# Old stored ids → current canonical ids (data keys, CSV competition columns, etc.).
COMPETITION_ID_ALIASES = {
    "Brazil/Serie A": "Brazil/Brasileirão",
}

# UI-friendly labels when the Country/League key alone is ambiguous.
# Famous leagues keep their familiar short name; others get a clearer title.
COMPETITION_DISPLAY_NAMES = {
    "Brazil/Brasileirão": "Brasileirão",
    "Italy/Serie A": "Serie A",
    "England/Premier League": "Premier League",
    "Ukraine/Premier League": "Ukrainian Premier League",
    "Israel/Premier League": "Israeli Premier League",
    "Azerbaijan/Premier League": "Azerbaijan Premier League",
    "Kazakhstan/Premier League": "Kazakhstan Premier League",
    "Belarus/Premier League": "Belarus Premier League",
    "Germany/Bundesliga": "Bundesliga",
    "Austria/Bundesliga": "Austrian Bundesliga",
    "Switzerland/Super League": "Swiss Super League",
    "Greece/Super League": "Super League Greece",
    "Turkey/Super Lig": "Süper Lig",
    "Slovakia/Super Liga": "Slovak Super Liga",
}


def canonical_competition_id(name: str) -> str:
    """Return the current competition id, remapping known legacy names."""
    text = str(name or "").strip()
    return COMPETITION_ID_ALIASES.get(text, text)


def competition_display_name(name: str) -> str:
    """Human-facing league label (may drop the country prefix when unambiguous)."""
    canonical = canonical_competition_id(name)
    if canonical in COMPETITION_DISPLAY_NAMES:
        return COMPETITION_DISPLAY_NAMES[canonical]
    if "/" in canonical:
        return canonical.split("/", 1)[1]
    return canonical
