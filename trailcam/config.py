"""Settings.

Fixed settings live here. What the dashboard sets up (the mode, the Ring app keys and the
tunnel address) is saved in data/settings.json and read fresh each time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Settings:
    # Ring Partner API.
    api_base = _env("RING_API_BASE", "https://api.amazonvision.com").rstrip("/")
    oauth_url = _env("RING_OAUTH_URL", "https://oauth.ring.com/oauth/token")

    # Clip handling. Ring's guidance: wait until the range has ended plus 5 s, then retry
    # 416/425 with backoff until 2 minutes after the range ends.
    clip_ms = int(_env("CLIP_MS", "15000"))
    media_wait_s = float(_env("MEDIA_WAIT_S", "5"))
    media_deadline_s = float(_env("MEDIA_DEADLINE_S", "120"))

    # Fallback location for the species geofence when Ring has none for a device.
    default_country = _env("DEFAULT_COUNTRY", "US")
    default_state = _env("DEFAULT_STATE", "")

    # Playground events are read by polling Event History (Ring's "API polling alternative").
    poll_s = float(_env("POLL_S", "5"))

    # Clip pool for Playground Motion events. Not present in the open source version.
    motion_clips = ROOT / "simulation" / "motion-clips"

    data_dir = Path(_env("DATA_DIR", str(ROOT / "data")))

    @property
    def file(self) -> Path:
        return self.data_dir / "settings.json"

    def saved(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, **values) -> None:
        data = self.saved()
        data.update(values)
        self.file.write_text(json.dumps(data, indent=1))

    @property
    def simulation_available(self) -> bool:
        return self.motion_clips.is_dir()

    @property
    def mode(self) -> str | None:
        """"simulation" or "camera", or None until chosen on the dashboard."""
        if not self.simulation_available:
            return "camera"
        return self.saved().get("mode")

    @property
    def client_id(self) -> str:
        return self.saved().get("client_id", "")

    @property
    def client_secret(self) -> str:
        return self.saved().get("client_secret", "")

    @property
    def hmac_key(self) -> str:
        return self.saved().get("hmac_key", "")

    @property
    def public_url(self) -> str:
        return self.saved().get("public_url", "")


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
