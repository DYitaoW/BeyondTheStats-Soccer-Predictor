#!/usr/bin/env bash
# ==============================================================================
# Beyond the Stats - M6 Mac Mini (Apple Silicon macOS) Production Setup Script
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PLIST_NAME="com.beyondthestats.backend.plist"
PLIST_SRC="${SCRIPT_DIR}/${PLIST_NAME}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
TARGET_PLIST="${LAUNCH_AGENTS_DIR}/${PLIST_NAME}"

echo "=========================================================="
echo " Beyond the Stats - Apple Silicon Mac Mini Setup"
echo " Target Directory: ${APP_DIR}"
echo "=========================================================="

# 1. Architecture & OS Verification
ARCH="$(uname -m)"
OS="$(uname -s)"
if [[ "${OS}" != "Darwin" ]]; then
    echo "[!] Warning: This script is configured for macOS (Darwin). Detected OS: ${OS}"
fi
echo "[+] Detected OS: ${OS}, Architecture: ${ARCH}"

# 2. Check Homebrew & Python
if ! command -v brew &>/dev/null; then
    if [[ -f "/opt/homebrew/bin/brew" ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi

PYTHON_BIN=""
for cmd in python3.11 python3.12 python3.13 python3; do
    if command -v "${cmd}" &>/dev/null; then
        VER="$("${cmd}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        MAJOR="$("${cmd}" -c 'import sys; print(sys.version_info.major)')"
        MINOR="$("${cmd}" -c 'import sys; print(sys.version_info.minor)')"
        if [[ "${MAJOR}" -eq 3 && "${MINOR}" -ge 10 ]]; then
            PYTHON_BIN="$(command -v "${cmd}")"
            echo "[+] Found compatible Python: ${PYTHON_BIN} (v${VER})"
            break
        fi
    fi
done

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[-] Python 3.10+ not found. Installing via Homebrew..."
    brew install python@3.11
    PYTHON_BIN="$(brew --prefix python@3.11)/bin/python3.11"
fi

# 3. Create or Locate Virtual Environment
VENV_DIR="${APP_DIR}/venv"
for candidate in "${APP_DIR}/.venv2" "${APP_DIR}/.venv" "${APP_DIR}/venv"; do
    if [[ -d "${candidate}" && -f "${candidate}/bin/python" ]]; then
        VENV_DIR="${candidate}"
        echo "[+] Found existing virtual environment at ${VENV_DIR}"
        break
    fi
done

if [[ ! -d "${VENV_DIR}" ]]; then
    echo "[+] Creating virtual environment at ${VENV_DIR}..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"

echo "[+] Upgrading pip & wheel..."
"${VENV_PIP}" install --upgrade pip setuptools wheel

# 4. Install Dependencies
if [[ -f "${APP_DIR}/requirements.txt" ]]; then
    echo "[+] Installing project dependencies from requirements.txt..."
    "${VENV_PIP}" install -r "${APP_DIR}/requirements.txt"
fi

# Ensure Uvicorn and Gunicorn are installed for high-performance macOS production serving
"${VENV_PIP}" install uvicorn gunicorn httpx pyjwt cryptography

# 5. Prepare Output & Log Directories
echo "[+] Initializing output directories..."
mkdir -p "${APP_DIR}/Output/Logs"
mkdir -p "${APP_DIR}/Output/Status"
mkdir -p "${APP_DIR}/Output/Predictions"
mkdir -p "${APP_DIR}/Output/CupData"
mkdir -p "${APP_DIR}/Output/LeagueData"
mkdir -p "${LAUNCH_AGENTS_DIR}"

# 6. Generate and Install LaunchAgent Plist with Actual Paths
echo "[+] Configuring LaunchAgent plist..."
sed \
    -e "s|/Users/Shared/BeyondTheStats/Soccer-Result-Predictor/Beyond-the-Stats/venv/bin/python|${VENV_PYTHON}|g" \
    -e "s|/Users/Shared/BeyondTheStats/Soccer-Result-Predictor/Beyond-the-Stats|${APP_DIR}|g" \
    "${PLIST_SRC}" > "${TARGET_PLIST}"

chmod 644 "${TARGET_PLIST}"
echo "[+] Installed LaunchAgent to ${TARGET_PLIST}"

# 7. Load / Restart the Launchd Service
echo "[+] Loading service via launchctl..."
# Unload previous instance if present
launchctl bootout "gui/$(id -u)" "${TARGET_PLIST}" 2>/dev/null || launchctl unload "${TARGET_PLIST}" 2>/dev/null || true

# Bootstrap the new service
if launchctl bootstrap "gui/$(id -u)" "${TARGET_PLIST}" 2>/dev/null; then
    echo "[+] Successfully bootstrapped com.beyondthestats.backend"
else
    # Fallback for older macOS versions
    launchctl load -w "${TARGET_PLIST}"
    echo "[+] Loaded com.beyondthestats.backend via legacy launchctl load"
fi

# 8. Service Status and Verification
sleep 2
echo "=========================================================="
echo " Setup complete! Service status:"
launchctl list | grep "com.beyondthestats" || true
echo ""
echo " To check live logs:"
echo "   tail -f \"${APP_DIR}/Output/Logs/launchd_stdout.log\""
echo "   tail -f \"${APP_DIR}/Output/Logs/launchd_stderr.log\""
echo ""
echo " To test local API health:"
echo "   curl http://localhost:8000/api/health"
echo "=========================================================="

