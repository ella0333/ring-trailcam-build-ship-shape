"""Cloudflare quick tunnel, so Ring can reach this computer over HTTPS for camera mode.

Ring sends webhooks, the token exchange and the account link to HTTPS addresses registered
in the Ring developer console. A quick tunnel gives a new trycloudflare.com address each run.
"""

from __future__ import annotations

import atexit
import logging
import platform
import re
import shutil
import subprocess
import threading
import urllib.request

from .config import ROOT, settings

log = logging.getLogger(__name__)

TOOLS = ROOT / "tools"
DOWNLOADS = {
    ("Windows", "AMD64"): "cloudflared-windows-amd64.exe",
    ("Linux", "x86_64"): "cloudflared-linux-amd64",
    ("Linux", "aarch64"): "cloudflared-linux-arm64",
}
INSTALL_URL = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"

state = {"status": "off", "error": ""}  # off, starting, on, error
_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def _cloudflared() -> str:
    found = shutil.which("cloudflared")
    if found:
        return found
    name = DOWNLOADS.get((platform.system(), platform.machine()))
    if not name:
        raise RuntimeError(f"Install cloudflared from {INSTALL_URL} and restart Ring Trail Cam.")
    exe = TOOLS / ("cloudflared.exe" if name.endswith(".exe") else "cloudflared")
    if not exe.exists():
        TOOLS.mkdir(exist_ok=True)
        urllib.request.urlretrieve(f"https://github.com/cloudflare/cloudflared/releases/latest/download/{name}", exe)
        exe.chmod(0o755)
    return str(exe)


def start() -> None:
    """Start the tunnel in the background. Safe to call more than once."""
    with _lock:
        if state["status"] in ("starting", "on"):
            return
        state.update(status="starting", error="")
    settings.save(public_url="")
    threading.Thread(target=_run, name="tunnel", daemon=True).start()


def _run() -> None:
    global _proc
    try:
        _proc = subprocess.Popen([_cloudflared(), "tunnel", "--no-autoupdate", "--url", "http://localhost:8000"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except (OSError, RuntimeError) as e:
        state.update(status="error", error=str(e))
        return
    for line in _proc.stdout:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m and state["status"] != "on":
            settings.save(public_url=m.group(0))
            state["status"] = "on"
            log.info("tunnel: %s", m.group(0))
    state.update(status="error", error="The tunnel stopped. Restart Ring Trail Cam.")


@atexit.register
def stop() -> None:
    if _proc and _proc.poll() is None:
        _proc.terminate()
