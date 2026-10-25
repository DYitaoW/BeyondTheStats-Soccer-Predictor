#!/usr/bin/env python3
"""
startbts - Quick reload and manager for Beyond the Stats backend.

Usage:
    python startbts              # Restart/reload background backend service with new changes
    python startbts.py           # Same as above
    ./startbts                   # Same as above
    python startbts status       # Check service status & API health
    python startbts logs         # Tail live backend logs
    python startbts stop         # Stop the background service
    python startbts --foreground # Run in foreground (dev/debug mode)
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request
import urllib.error
import subprocess
from pathlib import Path


def find_directories() -> tuple[Path, Path]:
    """Return (repo_root, app_dir)."""
    current = Path.cwd().resolve()
    script_dir = Path(__file__).resolve().parent

    for candidate in [current, script_dir]:
        if (candidate / "Beyond-the-Stats").is_dir():
            return candidate, candidate / "Beyond-the-Stats"
        if (candidate / "Website").is_dir() and (candidate / "main").is_dir():
            return candidate.parent, candidate

    return current, current


def find_python_bin(app_dir: Path) -> Path:
    """Find the virtualenv Python binary."""
    for venv_name in [".venv2", ".venv", "venv"]:
        candidate = app_dir / venv_name / "bin" / "python"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return Path(sys.executable)


def get_uid() -> int:
    return os.getuid()


def check_health(port: int = 8000, timeout: float = 2.0) -> tuple[bool, str]:
    """Query /api/health and return (is_healthy, message)."""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "startbts/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8", errors="replace")
                return True, body
            return False, f"HTTP {resp.status}"
    except urllib.error.URLError as e:
        return False, str(e.reason if hasattr(e, "reason") else e)
    except Exception as e:
        return False, str(e)


def restart_launchd_service(app_dir: Path) -> bool:
    """Kickstart or bootstrap the launchd service."""
    uid = get_uid()
    service_label = "com.beyondthestats.backend"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{service_label}.plist"

    print(f"[+] Reloading Beyond the Stats backend (gui/{uid}/{service_label})...")

    # 1. Try kickstart first (fastest restart if already registered)
    res = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{service_label}"],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        print("[+] Sent reload signal to background service.")
    else:
        # If not active, bootstrap it
        print(f"[-] Kickstart: {res.stderr.strip() or 'service not active'}. Bootstrapping plist...")
        if not plist_path.is_file():
            setup_script = app_dir / "deploy" / "macos" / "setup_mac_mini.sh"
            if setup_script.is_file():
                print(f"[+] Initializing plist via {setup_script.name}...")
                subprocess.run(["bash", str(setup_script)], check=False)

        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{service_label}"], capture_output=True)
        time.sleep(0.5)

        if plist_path.is_file():
            boot_res = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                capture_output=True,
                text=True,
            )
            if boot_res.returncode == 0:
                print("[+] Successfully bootstrapped service.")
            else:
                subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True)

    # Verify health
    print("[+] Waiting for backend to become ready...")
    time.sleep(1.5)
    healthy = False
    msg = ""
    for _ in range(5):
        healthy, msg = check_health(8000, timeout=1.5)
        if healthy:
            break
        time.sleep(1.0)

    if healthy:
        print("\n========================================================")
        print("  Backend is UP and healthy on port 8000!")
        print("  API Health: http://localhost:8000/api/health")
        print("========================================================")
    else:
        print(f"\n[!] Notice: Service kicked, but port 8000 health check not ready yet ({msg}).")
        print("    Check live output: python startbts logs")

    return healthy


def stop_launchd_service() -> None:
    uid = get_uid()
    service_label = "com.beyondthestats.backend"
    print(f"[+] Stopping service {service_label}...")
    res = subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{service_label}"],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        print("[+] Service stopped successfully.")
    else:
        print(f"[!] Result: {res.stderr.strip() or res.stdout.strip() or 'already stopped'}")


def print_status(app_dir: Path) -> None:
    print("=" * 60)
    print(" Beyond the Stats Backend Status")
    print("=" * 60)
    service_label = "com.beyondthestats.backend"

    # Check launchctl list
    res = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    matching = [line for line in res.stdout.splitlines() if service_label in line]
    if matching:
        print(f"[+] Launchd Service : RUNNING ({matching[0].strip()})")
    else:
        print("[-] Launchd Service : NOT running in launchctl")

    healthy, msg = check_health(8000, timeout=1.5)
    if healthy:
        print(f"[+] API Health (8000): ONLINE -> {msg[:80]}")
    else:
        print(f"[-] API Health (8000): OFFLINE ({msg})")

    stderr_log = app_dir / "Output" / "Logs" / "launchd_stderr.log"
    stdout_log = app_dir / "Output" / "Logs" / "launchd_stdout.log"
    print(f"\nLog files:\n  stderr: {stderr_log}\n  stdout: {stdout_log}")

    if stderr_log.is_file():
        print("\nRecent Activity (last 6 lines of launchd_stderr.log):")
        try:
            lines = stderr_log.read_text(errors="replace").splitlines()
            for l in lines[-6:]:
                print(f"  {l}")
        except Exception:
            pass
    print("=" * 60)


def tail_logs(app_dir: Path) -> None:
    stderr_log = app_dir / "Output" / "Logs" / "launchd_stderr.log"
    if not stderr_log.is_file():
        print(f"[!] Log file not found at {stderr_log}")
        return
    print(f"[+] Streaming live backend logs from {stderr_log} (Ctrl+C to stop)...")
    try:
        subprocess.run(["tail", "-n", "20", "-f", str(stderr_log)])
    except KeyboardInterrupt:
        print("\n[+] Exited log stream.")


def run_foreground(app_dir: Path) -> None:
    py_bin = find_python_bin(app_dir)
    entry = app_dir / "main" / "run_backend.py"
    print(f"[+] Running in foreground mode: {py_bin} {entry}...")
    try:
        subprocess.run([str(py_bin), str(entry)], cwd=str(app_dir))
    except KeyboardInterrupt:
        print("\n[+] Backend stopped.")


def main() -> int:
    _, app_dir = find_directories()
    args = sys.argv[1:]
    cmd = (args[0] if args else "").strip().lower()

    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    elif cmd in ("status", "--status", "-s"):
        print_status(app_dir)
        return 0
    elif cmd in ("stop", "--stop"):
        stop_launchd_service()
        return 0
    elif cmd in ("logs", "--logs", "log", "-l"):
        tail_logs(app_dir)
        return 0
    elif cmd in ("--foreground", "-f", "dev", "foreground"):
        run_foreground(app_dir)
        return 0
    else:
        # Default action: reload/restart the backend
        restart_launchd_service(app_dir)
        return 0


if __name__ == "__main__":
    sys.exit(main())
