# BeyondTheStats API v2: Query Guide & Frontend Integration Design Document

This document provides a comprehensive guide for the newly implemented API architecture, detailing all endpoint inputs, response outputs, and the migration strategy for frontend mobile/web applications.

---

## 1. Architectural Overview: The Two-Tier Data Model

### The Problem in v1
Previously, client applications loaded large monolithic datasets on startup (full weeks of fixtures, exhaustive player lineups, and detailed shot maps). This resulted in:
- High network latency and 1–3 MB downloads on cold launch.
- Noticeable UI freeze and frame drops during client-side JSON decoding (`JSONDecoder` in Swift / Android).
- 4–5 uncoordinated parallel HTTP requests on boot.

### The Solution in v2
A **Two-Tier (List View vs. Detail Drilldown) Architecture**:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        COLD APP LAUNCH                                 │
│                                                                        │
│   GET /api/startup (or /api/bootstrap)                                 │
│   - Unified single-request boot (< 20 KB)                              │
│   - Today's compact live scores (scores, clock, live odds, scorers)    │
│   - Today's compact upcoming matches (odds, predictions)               │
│   - Poller status & server timestamp                                   │
└────────────────────────────────────────────────────────────────────────┘
                                 │
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
┌─────────────────────────────────┐   ┌──────────────────────────────────┐
│     SCHEDULE / CALENDAR VIEW    │   │         IN-PLAY REFRESH          │
│                                 │   │                                  │
│ GET /api/upcoming?date=YYYY-MM-DD│   │ GET /api/live-scores?compact=true│
│   &compact=true                 │   │   (or SSE stream: /stream)       │
│ - Day-by-day lazy loading       │   │ - 304 ETag support               │
│ - Match cards render in < 3ms   │   │ - Goalscorers & cards included   │
└─────────────────────────────────┘   └──────────────────────────────────┘
                 │                               │
                 └───────────────┬───────────────┘
                                 │  (User taps a specific match)
                                 ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        MATCH DETAIL VIEW                               │
│                                                                        │
│   GET /api/match/<match_id>                                            │
│   - Full deep-dive dossier (loaded on-demand only)                     │
│   - Lineups, formations, bench, boxscore stats                         │
│   - Shot coordinates, goal locations, momentum graphs, head-to-head    │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. API Endpoint Reference

---

### A. Unified Startup Endpoint: `GET /api/startup` (Alias: `GET /api/bootstrap`)
Replaces multiple boot requests with a single lightweight, anonymous call. No user tokens or authentication required.

#### Query Parameters
*None required.* (Completely stateless and publicly cacheable).

#### Sample Request
```http
GET /api/startup
```

#### Response Payload (`200 OK`)
```json
{
  "ok": true,
  "server_time_utc": "2026-10-14T22:00:00Z",
  "today_date": "2026-10-14",
  "poller": {
    "started": true,
    "thread_alive": true,
    "effective_poll_date": "2026-10-14"
  },
  "live_scores": {
    "total_games": 1,
    "competitions": {
      "England/Premier League": {
        "competition": "England/Premier League",
        "last_polled_utc": "2026-10-14T21:59:45Z",
        "games": [
          {
            "match_id": "700123",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 2,
            "away_score": 1,
            "status": "in",
            "clock": "74'",
            "kickoff_utc": "2026-10-14T19:00:00Z",
            "goalscorers": [
              { "team": "home", "scorer": "B. Saka", "minute": "34'", "type": "Goal" },
              { "team": "away", "scorer": "C. Palmer", "minute": "45+2'", "type": "Penalty" },
              { "team": "home", "scorer": "K. Havertz", "minute": "62'", "type": "Goal" }
            ],
            "red_cards": [],
            "yellow_cards": [
              { "team": "away", "player": "M. Caicedo", "minute": "41'" }
            ],
            "live_prediction": {
              "prob_home": 78.4,
              "prob_draw": 15.2,
              "prob_away": 6.4
            }
          }
        ]
      }
    }
  },
  "upcoming_today": {
    "total_games": 1,
    "games": [
      {
        "match_id": "700124",
        "competition": "England/Premier League",
        "match_date": "2026-10-14",
        "match_date_iso": "2026-10-14",
        "home_team": "Liverpool",
        "away_team": "Manchester City",
        "predicted_result": "H",
        "prob_home": 44.0,
        "prob_draw": 28.5,
        "prob_away": 27.5,
        "odds_home": 2.15,
        "odds_draw": 3.40,
        "odds_away": 3.10
      }
    ]
  }
}
```

---

### B. On-Demand Full Match Detail: `GET /api/match/<match_id>`
Fetches all heavy in-depth stats for a single game when a user opens the match details view.

#### Path Parameters
| Parameter | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `match_id` | string | Yes | Unique ESPN match ID | `700123` |

#### Sample Request
```http
GET /api/match/700123
```

#### Response Payload (`200 OK`)
```json
{
  "ok": true,
  "source": "live",
  "game": {
    "match_id": "700123",
    "competition": "England/Premier League",
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "home_score": 2,
    "away_score": 1,
    "status": "in",
    "clock": "74'",
    "period": 2,
    "lineups": {
      "home": {
        "formation": "4-3-3",
        "startXI": [
          { "name": "David Raya", "number": 22, "position": "G" },
          { "name": "Bukayo Saka", "number": 7, "position": "F" }
        ],
        "substitutes": [ ... ]
      },
      "away": { ... }
    },
    "boxscore_stats": {
      "possession": { "home": 58, "away": 42 },
      "shots": { "home": 12, "away": 6 },
      "shots_on_target": { "home": 5, "away": 2 },
      "fouls": { "home": 8, "away": 11 }
    },
    "shot_origins": [
      { "x": 84, "y": 48, "minute": "34'", "player": "B. Saka", "is_goal": true, "on_target": true }
    ],
    "goal_locations": [
      { "x": 12, "y": 68, "minute": "34'", "player": "B. Saka" }
    ],
    "cumulative_momentum": [
      { "minute": 15, "value": 0.35 },
      { "minute": 34, "value": 0.82 }
    ],
    "live_prediction": {
      "prob_home": 78.4,
      "prob_draw": 15.2,
      "prob_away": 6.4
    }
  }
}
```

---

### C. Upcoming Fixtures: `GET /api/upcoming/<mode>`

#### Query Parameters
| Parameter | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `date` | string | No | Single date filter. Accepts `YYYY-MM-DD` or `"today"` | `today` or `2026-10-15` |
| `match_id` | string | No | Filter to a specific single match ID | `700124` |
| `compact` | boolean | No | If `"true"`, omits bulky logs/reasoning; includes odds, cards, and predictions | `true` |
| `window` | string | No | Multi-day window (`"2week"`, `"4week"`) | `2week` |
| `limit` / `offset` | integer | No | Flat pagination limits | `limit=20&offset=0` |

#### Sample Requests
```http
# Rapid schedule view for tomorrow:
GET /api/upcoming/global?date=2026-10-15&compact=true

# Single match prediction check:
GET /api/upcoming/global?match_id=700124&compact=true
```

---

### D. Historical Completed Games: `GET /api/past-games`

#### Query Parameters
| Parameter | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `date` | string | No | Exact calendar day match filter (`YYYY-MM-DD` or `"today"`) | `today` |
| `match_id` | string | No | Exact match ID filter | `700120` |
| `from` / `to` | string | No | Date range. Accepts `YYYY-MM-DD` or `"today"` | `from=2026-10-01&to=today` |
| `compact` | boolean | No | Formats row into lightweight card payload | `true` |
| `page` | integer | No | Weekly matchweek pagination index (1 = latest) | `1` |
| `limit` / `offset` | integer | No | Activates flat list pagination mode | `limit=25&offset=0` |

#### Sample Requests
```http
# Past games completed today:
GET /api/past-games?date=today&compact=true

# Check prediction vs actual result for a single game:
GET /api/past-games?match_id=700120
```

---

### E. Live Score History: `GET /api/live-score-history`

#### Query Parameters
| Parameter | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `date` | string | No | Single day filter (`YYYY-MM-DD` or `"today"`) | `today` |
| `match_id` | string | No | Single match filter | `700120` |
| `from` / `to` | string | No | Date range (`YYYY-MM-DD` or `"today"`) | `from=today&to=today` |
| `compact` | boolean | No | Strips verbose stats, keeps scores, scorers, cards | `true` |

---

## 3. What Needs to Be Done on the Frontend App

To fully leverage the speed and bandwidth improvements, update your frontend client (iOS Swift / Android / React) according to the steps below:

### 1. Refactor Cold App Launch
- **Old Pattern:** Firing `/api/live-scores` and `/api/upcoming` concurrently on boot.
- **New Pattern:** Call `GET /api/startup` on app startup:
  ```swift
  func bootstrapApp() async throws {
      let url = URL(string: "\(API_BASE)/api/startup")!
      let (data, _) = try await URLSession.shared.data(from: url)
      let boot = try JSONDecoder().decode(StartupResponse.self, from: data)
      
      // Update local state immediately
      self.liveMatches = boot.liveScores.competitions
      self.todaysUpcoming = boot.upcomingToday.games
  }
  ```

### 2. Transition Schedule Screens to Day-by-Day Lazy Loading
- Instead of downloading 4 weeks of fixtures on opening the schedule tab:
  1. Default to today: `GET /api/upcoming/global?date=today&compact=true`.
  2. When the user swipes or selects a new date from the date picker:
     `GET /api/upcoming/global?date=\(selectedDateIso)&compact=true`.
  3. Store results in a client-side dictionary (`cachedDays[dateIso] = games`) to prevent re-fetching previously viewed dates.

### 3. Display Scorecard Summaries at a Glance
Because compact live and upcoming objects now include `goalscorers`, `red_cards`, and `odds`, you can display them directly in your list view cells:
- **Scorers badge:** Display e.g. `B. Saka 34', K. Havertz 62'` below the team name.
- **Red cards:** Show a red card indicator if `red_cards.count > 0`.
- **Live Odds:** Display `live_prediction.prob_home` / `prob_draw` / `prob_away` live in the match card.
- **Prematch Odds:** Display `odds_home`, `odds_draw`, `odds_away` on upcoming fixture cards.

### 4. Implement On-Demand Match Detail Routing
When the user taps any fixture card:
- Navigate immediately to the Match Detail screen.
- Show the basic info (teams, score, minute) from the list card.
- In the background, fire:
  ```swift
  func loadMatchDetail(matchId: String) async throws {
      let url = URL(string: "\(API_BASE)/api/match/\(matchId)")!
      let (data, _) = try await URLSession.shared.data(from: url)
      let detail = try JSONDecoder().decode(MatchDetailResponse.self, from: data)
      self.selectedMatch = detail.game
  }
  ```
- Seamlessly populate lineups, shot maps, and boxscore stats once the request resolves.

---

## 4. Performance Summary

| Metric | v1 Implementation | v2 Implementation | Improvement |
| :--- | :--- | :--- | :--- |
| **Startup Requests** | 4–5 parallel calls | **1 unified call** (`/api/startup`) | **80% fewer handshakes** |
| **Boot Payload Size** | ~1.8 MB – 3.2 MB | **~18 KB – 24 KB** | **98% bandwidth reduction** |
| **JSON Parse Time** | 350ms – 800ms | **< 3ms** | **No UI stutter or freezing** |
| **Live Scores View** | Excluded scorers & cards | **Includes scorers, cards, & live odds** | **Rich summary at a glance** |
| **Detail Deep-Dive** | Bundled in main list | **On-demand** (`/api/match/<id>`) | **Zero wasted bandwidth** |
| **GZip Compression** | Uncompressed raw JSON | **Automatic `GZipMiddleware` (>=1000B)** | **65%–85% wire compression** |

---

## 5. Automatic Wire Compression (GZip)

The backend runs Starlette/FastAPI `GZipMiddleware`:
- **Threshold**: Responses $\ge 1,000$ bytes are automatically compressed using gzip (`Content-Encoding: gzip`).
- **Client Zero-Change Guarantee**:
  - **iOS / Swift (`URLSession`)**: Transparently sends `Accept-Encoding: gzip, deflate` and decompresses responses automatically before passing bytes to `JSONDecoder`. No code changes required.
  - **Web Browsers (`fetch` / `axios`)**: The browser networking stack transparently requests and inflates gzip streams before JavaScript resolves `response.json()`.
  - **Header Support**: Adds `Vary: Accept-Encoding` to ensure CDN/proxy caches (e.g. Cloudflare) isolate compressed from uncompressed clients.

