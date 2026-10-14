"""Stop Ring Trail Cam:  python stop.py"""

import os
import signal
import subprocess
from pathlib import Path

PID = Path(__file__).resolve().parent / "data" / "trailcam.pid"


def main() -> None:
    if not PID.exists():
        print("Ring Trail Cam is not running.")
        return
    pid = int(PID.read_text())
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    PID.unlink()
    print("Ring Trail Cam stopped.")


if __name__ == "__main__":
    main()
