# Cloudflare Caching & Tunnel Setup Guide

This guide explains how to put Cloudflare in front of the BeyondTheStats backend (e.g. running on an M-series Mac mini, home server, or VPS) to protect your home network, reduce server load by 90-95%, and achieve global edge response times under 15ms.

---

## 1. Overview: How Cloudflare Works with This Backend

By default, Cloudflare proxies traffic and protects against DDoS attacks, but it **does not cache API JSON responses** unless explicitly instructed via **Cache Rules**.

Because BeyondTheStats endpoints serve different types of data (real-time live scores vs. daily predictions), a two-tier caching strategy is recommended:

```
[ Mobile / Web Clients ]
           │
           ▼
[ Cloudflare Global Edge ]  ◄── Absorbs 90-95% of requests
   ├── Micro-cached (/api/live-scores, TTL: 5s)
   ├── Long-cached (/api/upcoming, /api/past-games, TTL: 15m - 1h)
   └── Stream bypassed (/api/live-scores/stream, SSE direct proxy)
           │
      (Cloudflare Tunnel)
           │
           ▼
[ Local Mac mini Backend ]  ◄── Only receives ~1 request per 5-10 seconds!
```

---

## 2. Setting Up Cloudflare Tunnel (`cloudflared`)

A Cloudflare Tunnel connects your local machine directly to Cloudflare without opening any router ports (no port 80/443 forwarding) and without exposing your home IP address.

### Step 1: Install `cloudflared` on macOS
Using Homebrew:
```bash
brew install cloudflare/cloudflare/cloudflared
```

### Step 2: Authenticate
```bash
cloudflared tunnel login
```
This opens a browser window to link your Cloudflare account.

### Step 3: Create the Tunnel
```bash
cloudflared tunnel create beyondthestats
```
This outputs a Tunnel ID (e.g., `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`) and creates a credentials file in `~/.cloudflared/`.

### Step 4: Configure the Tunnel
Create `~/.cloudflared/config.yml`:
```yaml
tunnel: beyondthestats
credentials-file: /Users/<your-username>/.cloudflared/<tunnel-id>.json

ingress:
  # Map your API subdomain to the local backend port (default 8000)
  - hostname: api.beyondthestats.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

### Step 5: Route DNS to the Tunnel
```bash
cloudflared tunnel route dns beyondthestats api.beyondthestats.com
```

### Step 6: Run as a Background Service
To have the tunnel run automatically 24/7 on your Mac mini:
```bash
sudo cloudflared service install
sudo launchctl start com.cloudflare.cloudflared
```

---

## 3. Configuring Cloudflare Cache Rules

In the [Cloudflare Dashboard](https://dash.cloudflare.com):
1. Select your domain.
2. Go to **Caching** > **Cache Rules**.
3. Click **Create rule** for each rule below.

---

### Rule 1: Live Scores Micro-Caching (`/api/live-scores`)
Prevents thousands of simultaneous active users from hammering the backend during live matches.

- **Rule Name:** `Live Scores Micro-Cache`
- **When incoming requests match:**
  - Field: `URI Path`
  - Operator: `equals`
  - Value: `/api/live-scores`
- **Cache eligibility:** `Eligible for cache`
- **Edge TTL:**
  - Status: `Override origin`
  - TTL: `5 seconds` (or `10 seconds`)
- **Browser TTL:**
  - Status: `Respect origin` (or `Bypass cache` to force the app to always ask Cloudflare edge)

> **Impact:** If 5,000 users refresh the live scoreboard within 5 seconds, **only 1 request** hits your Mac mini. The other 4,999 requests are served instantly by Cloudflare's edge cache.

---

### Rule 2: Predictions & Historical Games Caching
For endpoints where data only updates once a day or after a matchweek.

- **Rule Name:** `Predictions & Past Games Cache`
- **When incoming requests match:**
  - Expression:
    ```
    (http.request.uri.path eq "/api/past-games") or
    (http.request.uri.path eq "/api/upcoming") or
    (starts_with(http.request.uri.path, "/api/home/")) or
    (starts_with(http.request.uri.path, "/api/tournaments/")) or
    (starts_with(http.request.uri.path, "/api/bracket/"))
    ```
- **Cache eligibility:** `Eligible for cache`
- **Edge TTL:**
  - Status: `Override origin`
  - TTL: `15 minutes` (or `1 hour`)
- **Browser TTL:**
  - Status: `Respect origin`

---

### Rule 3: SSE Streaming Bypass (`/api/live-scores/stream`)
Real-time Server-Sent Events (SSE) must stream continuously and should never be buffered or cached.

- **Rule Name:** `Bypass SSE Stream`
- **When incoming requests match:**
  - Field: `URI Path`
  - Operator: `equals`
  - Value: `/api/live-scores/stream`
- **Cache eligibility:** `Bypass cache`

*(Also ensure **Network > WebSockets** is enabled in the Cloudflare dashboard).*

---

### Rule 4: Mutation Endpoints (Never Cache)
Ensure push token subscriptions and user actions always reach the backend.

- **Rule Name:** `Bypass API Mutations`
- **When incoming requests match:**
  - Field: `URI Path`
  - Operator: `starts with`
  - Value: `/api/notifications/`
- **Cache eligibility:** `Bypass cache`

---

## 4. Backend-Side Cache Control Headers (Optional Best Practice)

If you prefer to control caching via HTTP headers instead of setting TTLs manually in the Cloudflare UI, the backend can return standard `Cache-Control` headers:

- **For live scores:**
  ```python
  response.headers["Cache-Control"] = "public, max-age=5, s-maxage=5, stale-while-revalidate=10"
  ```
- **For upcoming / past predictions:**
  ```python
  response.headers["Cache-Control"] = "public, max-age=300, s-maxage=900"
  ```
- **For SSE stream:**
  ```python
  response.headers["Cache-Control"] = "no-cache, no-transform"
  response.headers["X-Accel-Buffering"] = "no"
  ```

With `s-maxage`, Cloudflare respects the edge cache duration automatically without needing custom Cache Rules.

---

## 5. Purging the Cache After Pipeline Runs

When `Daily_Pipeline.py` finishes retraining models and generating new predictions, you can automatically purge the Cloudflare cache for prediction endpoints via Cloudflare's API:

```python
import os
import requests

def purge_cloudflare_predictions():
    zone_id = os.getenv("CLOUDFLARE_ZONE_ID")
    api_token = os.getenv("CLOUDFLARE_API_TOKEN")
    if not zone_id or not api_token:
        return

    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "files": [
            "https://api.beyondthestats.com/api/upcoming",
            "https://api.beyondthestats.com/api/past-games",
            "https://api.beyondthestats.com/api/home/upcoming",
        ]
    }
    requests.post(url, json=payload, headers=headers, timeout=5)
```
Add this call to the very end of `pipelines/europe/Daily_Pipeline.py` or `Run_All_Pipeline.py` so your users instantly receive updated matchweek predictions as soon as model training completes.

