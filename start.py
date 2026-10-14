"""Start Ring Trail Cam:  python start.py
Start with an empty field journal:  python start.py --fresh

The first run creates a .venv folder and installs the packages. The app then runs in the
background and the dashboard opens in your browser. Stop it with:  python stop.py
"""

import os
import shutil
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
DATA = ROOT / "data"
PID = DATA / "trailcam.pid"
URL = "http://localhost:8000"


def running() -> bool:
    try:
        urllib.request.urlopen(f"{URL}/api/state", timeout=2)
        return True
    except OSError:
        return False


def install() -> None:
    if sys.version_info < (3, 11):
        sys.exit("Ring Trail Cam needs Python 3.11 or newer.")
    if not PY.exists():
        print("Setting up for the first time. This takes a few minutes.")
        venv.create(VENV, with_pip=True)
    stamp = VENV / ".installed"
    reqs = ROOT / "requirements.txt"
    if not stamp.exists() or stamp.stat().st_mtime < reqs.stat().st_mtime:
        print("Installing packages...")
        subprocess.run([str(PY), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
        subprocess.run([str(PY), "-m", "pip", "install", "-q", "-r", str(reqs)], check=True)
        stamp.touch()


def launch() -> None:
    DATA.mkdir(exist_ok=True)
    log = open(DATA / "app.log", "a")
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        flags["start_new_session"] = True
    proc = subprocess.Popen([str(PY), "-m", "uvicorn", "trailcam.app:app", "--port", "8000"],
                            cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, **flags)
    PID.write_text(str(proc.pid))
    print("Starting...")
    deadline = time.time() + 180
    while not running():
        if proc.poll() is not None or time.time() > deadline:
            sys.exit(f"Ring Trail Cam did not start. See {DATA / 'app.log'}")
        time.sleep(1)


def clear_journal() -> None:
    """Remove sightings, events and saved clips. Mode, keys and tokens are kept."""
    (DATA / "trailcam.db").unlink(missing_ok=True)
    for folder in ("clips", "thumbs"):
        shutil.rmtree(DATA / folder, ignore_errors=True)
    print("Cleared the field journal.")


def main() -> None:
    if "--fresh" in sys.argv:
        if running():
            sys.exit("Stop Ring Trail Cam first with:  python stop.py")
        clear_journal()
    if running():
        print(f"Ring Trail Cam is already running at {URL}")
    else:
        install()
        launch()
        print(f"Ring Trail Cam is running at {URL}")
    print("The dashboard is opening in your browser and walks you through setup.")
    webbrowser.open(URL)
    print("Stop it with:  python stop.py")


if __name__ == "__main__":
    main()
