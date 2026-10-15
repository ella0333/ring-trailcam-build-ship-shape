"""Send random clips from simulation/motion-clips to the running app:  python send_clips.py 3"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def main() -> None:
    try:
        count = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    except ValueError:
        sys.exit("Usage: python send_clips.py 3")
    body = urllib.parse.urlencode({"count": count}).encode()
    try:
        r = urllib.request.urlopen("http://localhost:8000/api/simulate", data=body, timeout=10)
    except urllib.error.HTTPError as e:
        sys.exit(f"Ring Trail Cam refused: {e.read().decode(errors='replace')}")
    except OSError:
        sys.exit("Ring Trail Cam is not running. Start it with:  python start.py")
    queued = json.load(r)["queued"]
    print(f"Sent {queued} clip{'s' if queued != 1 else ''}. Watch the dashboard.")


if __name__ == "__main__":
    main()
