"""Event History polling for Ring Developer Playground tokens.

A Playground token is not tied to an app or an account link, so Ring has no webhook to
deliver its events to. Ring documents polling Event History as the alternative
("API polling alternative"). Events found this way go through the same pipeline as webhooks.
"""

from __future__ import annotations

import logging
import threading
import time

from .config import settings
from .pipeline import Pipeline
from .ring import RingError, tokens

log = logging.getLogger(__name__)


class PlaygroundPoller:
    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        self.ring = pipeline.ring
        self._seen: dict[str, set[str]] = {}  # device id -> history event ids already handled

    def start(self) -> None:
        threading.Thread(target=self._loop, name="playground-poller", daemon=True).start()

    def _loop(self) -> None:
        while True:
            if settings.mode == "simulation" and tokens.status()["connected"]:
                try:
                    self.poll()
                except RingError as e:
                    log.warning("Event History poll failed: %s", e)
            time.sleep(settings.poll_s)

    def poll(self) -> None:
        for device in self.ring.devices():
            device_id = device["id"]
            events = self.ring.history(device_id, "motion")
            ids = {e["id"] for e in events}
            if device_id not in self._seen:
                # Events from before the app started are history, not new motion.
                self._seen[device_id] = ids
                continue
            for e in reversed(events):  # oldest first
                if e["id"] in self._seen[device_id]:
                    continue
                self._seen[device_id].add(e["id"])
                start = int((e.get("attributes") or {}).get("start", 0))
                tags = self.ring.motion_tags(device_id, e["id"])
                button = playground_button(tags)
                status = self.pipeline.intake(
                    request_id=e["id"], event_id=e["id"], device_id=device_id, account_id="",
                    ts_ms=start, sub_type=button, component_id=None, source="playground",
                    note=f"Ring tags: {', '.join(tags) or 'none'}",
                )
                log.info("Playground %s event %s (Ring tags: %s): %s", button, e["id"], tags, status)


def playground_button(tags: list[str]) -> str:
    """Which Playground button made the event, from every tag Ring gave it.

    Package: Ring's package tag. It keeps package_delivery, so the pipeline uses the
    Playground's own video. Vehicle: Ring's vehicle tag, so the event is skipped. Anything
    else is the Motion button, whatever Ring tagged its synthetic video as, and becomes plain
    motion so the pipeline swaps in a clip from the motion pool."""
    if "package_delivery" in tags:
        return "package_delivery"
    if "vehicle" in tags:
        return "vehicle"
    return "motion"
