# Beyond the Stats - Production Deployment Guide for M6 Mac Mini

This guide outlines setup, service management, and Apple Silicon ARM64 optimizations for running **Beyond the Stats** on an **Apple Silicon (M6) Mac Mini**.

---

## 1. System Architecture & Prerequisites

- **OS**: macOS Sonoma / Sequoia / Tahoe (Darwin ARM64).
- **Architecture**: Apple Silicon Unified Memory Architecture (UMA).
- **Service Supervisor**: Native macOS `launchd` via User LaunchAgent (`~/Library/LaunchAgents/com.beyondthestats.backend.plist`).
- **Python Runtime**: Python 3.11+ (Homebrew: `/opt/homebrew/bin/python3.11`).
- **Web Server**: Gunicorn with Uvicorn ASGI workers (`uvicorn.workers.UvicornWorker`) hosting FastAPI.
- **Database**: SQLite with WAL mode (`bts_store.db`) on native APFS.

---

## 2. Quick Setup (Automated)

Run the automated setup script from the root of the repository:

```bash
chmod +x deploy/macos/setup_mac_mini.sh
./deploy/macos/setup_mac_mini.sh
```

The script will automatically:
1. Verify Apple Silicon architecture and locate/install Python 3.11+.
2. Create and configure a local virtual environment (`venv/`).
3. Install production dependencies including `uvicorn`, `gunicorn`, `httpx`, and `cryptography`.
4. Render and register `~/Library/LaunchAgents/com.beyondthestats.backend.plist`.
5. Bootstrap and start the backend service under `launchd`.

---

## 3. Launchd Service Commands

### Check Service Status
```bash
launchctl list | grep com.beyondthestats
```

### Stop the Service
```bash
launchctl bootout gui/$(id -u)/com.beyondthestats.backend
# Or legacy:
# launchctl unload ~/Library/LaunchAgents/com.beyondthestats.backend.plist
```

### Start the Service
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.beyondthestats.backend.plist
# Or legacy:
# launchctl load -w ~/Library/LaunchAgents/com.beyondthestats.backend.plist
```

### Restart the Service
```bash
launchctl kickstart -k gui/$(id -u)/com.beyondthestats.backend
```

---

## 4. Log Inspection & Monitoring

All outputs and errors are logged under `Output/Logs/`:

```bash
# Real-time server standard out:
tail -f Output/Logs/launchd_stdout.log

# Real-time error log:
tail -f Output/Logs/launchd_stderr.log

# Pipeline run logs:
tail -f Output/Logs/pipeline_latest.log
```

---

## 5. Apple Silicon / Mac Mini Specific Notes

1. **Port Selection & AirPlay Receiver Coexistence (Port 8000 vs 5000)**:
   - macOS Monterey, Ventura, Sonoma, and Sequoia bind port `5000` to `ControlCenter` (AirPlay Receiver) by default.
   - To prevent `OSError: [Errno 48] Address already in use` crashes, the backend automatically defaults to port **`8000`** on macOS (`PORT=8000`). If port 5000 is explicitly requested and blocked by AirPlay, the server will output a detailed diagnostic message.

2. **Multiprocessing Safety (`OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`)**:
   - macOS requires `spawn` or `forkserver` when multiprocessing intersects with system frameworks (CoreFoundation).
   - The LaunchAgent and all Python entry points set `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` to prevent crashes during Monte Carlo simulations or sub-pipelines.

3. **Unified Memory Management & Auto-Scaling**:
   - Memory limits dynamically scale to ~60% of host physical RAM (detected via native `sysctl -n hw.memsize`), capped between 4 GB and 24 GB.
   - You can also explicitly adjust this limit via `--memory-limit-gb <GB>` or the `SOCCER_MEMORY_LIMIT_GB` environment variable.

4. **Shared Machine CPU Governance (Niceness & Reserved Cores)**:
   - On a multi-purpose Mac Mini used as an interactive workstation or running Docker/databases, full-load tasks (Monte Carlo simulations, XGBoost) reserve **at least 2 physical CPU cores** (`SOCCER_RESERVED_CORES=2`) so interactive desktop apps (browser, IDE) never experience UI stutter.
   - Background training pipelines execute with lower scheduler priority (`os.nice(5)` / LaunchAgent `<key>Nice</key><integer>5</integer>`), letting interactive apps and co-hosted services retain priority.

5. **Socket & File Descriptor Limits**:
   - macOS defaults `ulimit -n` to a low 256. The LaunchAgent plist sets a soft limit of `8192` (`SoftResourceLimits -> NumberOfFiles`) to support high-throughput SSE streams and live polling without descriptor exhaustion.

6. **APNs Certificate Permissions**:
   - Running as a user LaunchAgent ensures access to your Apple Push Notification `.p8` key without requiring root/daemon file ownership alterations.

7. **Energy Saver Settings**:
   - In macOS System Settings -> Energy Saver, ensure **"Prevent automatic sleeping when the display is off"** is enabled so the Mac Mini continues running 24/7.

