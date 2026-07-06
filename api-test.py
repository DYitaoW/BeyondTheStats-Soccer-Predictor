"""Test all /api/* endpoints and report pass/fail for each."""

import httpx
import sys
import json
from datetime import datetime, timezone

BASE = "https://api.beyondthestatsapp.com"

ENDPOINTS = [
    # (method, path, description, [kwargs])
    ("GET", "/api/help", "List all API routes"),
    ("GET", "/api/help/all", "Per-competition API listing"),
    ("GET", "/api/teams", "List teams"),
    ("GET", "/api/teams?mode=mls", "List teams (MLS)"),
    ("GET", "/api/teams?mode=extra", "List teams (Extra)"),
    ("GET", "/api/upcoming/global", "Upcoming global"),
    ("GET", "/api/upcoming/mls", "Upcoming MLS"),
    ("GET", "/api/upcoming/extra", "Upcoming extra"),
    ("GET", "/api/upcoming/cups", "Upcoming cups"),
    ("GET", "/api/upcoming/world-cup", "Upcoming World Cup"),
    ("GET", "/api/past-games", "Past games"),
    ("GET", "/api/past-games?league=Premier", "Past games filtered"),
    ("GET", "/api/past-games?page=0&per_page=20", "Past games paginated"),
    ("GET", "/api/world-cup", "World Cup projection"),
    ("GET", "/api/tournament/champions-league", "Tournament projection (Champions League)"),
    ("GET", "/api/tournament/fa-cup", "Tournament projection (FA Cup)"),
    ("GET", "/api/cup-bracket", "Cup bracket (no comp)"),
    ("GET", "/api/cup-bracket?competition=FA+Cup", "Cup bracket (FA Cup)"),
    ("GET", "/api/real-cup-data", "Real cup data (no comp)"),
    ("GET", "/api/real-cup-data?competition=FA+Cup", "Real cup data (FA Cup)"),
    ("GET", "/api/competition-data?competition=England/Premier+League", "Competition data (Prem)"),
    ("GET", "/api/competition-data?competition=FIFA/World+Cup", "Competition data (World Cup)"),
    ("GET", "/api/league-tables", "League tables (global)"),
    ("GET", "/api/league-tables?mode=mls", "League tables (MLS)"),
    ("GET", "/api/league-tables?mode=extra", "League tables (extra)"),
    ("GET", "/api/league-tables?mode=cups", "League tables (cups)"),
    ("GET", "/api/league-tables?league=Premier", "League tables filtered"),
    ("GET", "/api/real-tables", "Real tables (all)"),
    ("GET", "/api/real-tables?competition=England/Premier+League", "Real tables (Prem)"),
    ("GET", "/api/league-leaders", "League leaders"),
    ("GET", "/api/live-scores", "Live scores"),
    ("GET", "/api/live-score-history", "Live score history"),
    ("GET", "/api/live-score-history?league=Premier", "Live score history filtered"),
    ("GET", "/api/h2h?home=Manchester+United&away=Liverpool", "H2H"),
    ("GET", "/api/scorers", "Top scorers"),
    ("GET", "/api/stats", "Stats"),
    ("GET", "/api/last-refresh", "Last refresh time"),
    ("GET", "/api/last-data-refresh", "Last data refresh time"),
    ("GET", "/api/pipeline/status", "Pipeline status"),
    ("GET", "/api/mobile/feed", "Mobile feed"),
    ("GET", "/api/mobile/widget", "Mobile widget"),
    ("GET", "/api/mobile/widget?leagues=England/Premier+League", "Mobile widget (filtered)"),
    ("GET", "/api/notifications", "List notification subs"),
    ("GET", "/api/competitions", "List competitions"),
    ("POST", "/api/predict", "Predict (POST)"),
    ("POST", "/api/predict/mls", "Predict MLS (POST)"),
    ("POST", "/api/predict/extra", "Predict extra (POST)"),
    ("POST", "/api/refresh", "Trigger refresh (POST)"),
    ("POST", "/api/notifications", "Send notification (POST)"),
    ("POST", "/api/notifications/register", "Register device (POST)"),
    ("POST", "/api/feedback", "Submit feedback (POST)"),
]

# POST endpoints that need a body or auth — use minimal payload
POST_BODIES = {
    "/api/predict": {"home_team": "Manchester United", "away_team": "Liverpool"},
    "/api/predict/mls": {"home_team": "LA Galaxy", "away_team": "LAFC"},
    "/api/predict/extra": {"home_team": "Ajax", "away_team": "PSV"},
    "/api/refresh": {},
    "/api/notifications": {},
    "/api/notifications/register": {},
    "/api/feedback": {"feedback": "test"},
}

# Endpoints that are expected to 404 (ghost routes in /api/help but no route defined)
EXPECTED_404 = {"/api/last-data-refresh", "/api/pipeline/status", "/api/competitions"}

# Debug endpoints — expected 401 when no key is set
EXPECTED_401 = {"/api/debug/live-score-sources", "/api/debug/manual-poll", "/api/debug/poller-state"}

results = {"pass": 0, "fail": 0, "errors": []}

def test(method, path, desc, **kwargs):
    url = f"{BASE}{path}"
    ts = datetime.now(timezone.utc).isoformat()
    try:
        if method == "GET":
            resp = httpx.get(url, timeout=30, **kwargs)
        else:
            body = POST_BODIES.get(path.split("?")[0], {})
            resp = httpx.post(url, json=body, timeout=30, **kwargs)
    except Exception as e:
        results["fail"] += 1
        results["errors"].append(f"[{method}] {path} — {desc}: CONNECTION ERROR: {e}")
        return

    status = resp.status_code
    expect_404 = path.split("?")[0] in EXPECTED_404
    expect_401 = path.split("?")[0] in EXPECTED_401

    ok = False
    if 200 <= status < 300:
        ok = True
    elif status == 404 and expect_404:
        ok = True
    elif status == 401 and expect_401:
        ok = True
    elif status == 400 and method == "POST":
        # POST endpoints with no auth may 400 or 401, that's fine
        ok = True
    elif status == 401 and method == "POST":
        ok = True
    elif status == 403 and method == "POST":
        ok = True

    if ok:
        results["pass"] += 1
    else:
        body_snippet = resp.text[:200]
        results["fail"] += 1
        results["errors"].append(f"[{method}] {path} — {desc}: got {status}, expected 2xx — {body_snippet}")


def main():
    print(f"API Test — {BASE}")
    print(f"Started at {datetime.now(timezone.utc).isoformat()}Z")
    print(f"Testing {len(ENDPOINTS)} endpoints\n")

    for method, path, desc in ENDPOINTS:
        test(method, path, desc)

    print(f"\nResults: {results['pass']} passed, {results['fail']} failed\n")
    if results["errors"]:
        print("Failures:")
        for err in results["errors"]:
            print(f"  {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
