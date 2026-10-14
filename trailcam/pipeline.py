"""What happens to a motion event after the webhook has answered Ring.

queue -> wait for Ring's recording -> download the clip -> SpeciesNet -> sighting or delete.
"""

from __future__ import annotations

import logging
import queue
import random
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2

from . import store
from .classify import Classifier
from .config import settings
from .ring import RingClient, RingError

log = logging.getLogger(__name__)

CLIPS = settings.data_dir / "clips"
THUMBS = settings.data_dir / "thumbs"
CLIPS.mkdir(exist_ok=True)
THUMBS.mkdir(exist_ok=True)

# Ring's CV guidelines for a wildlife app: drop what Ring tagged as a person or a vehicle,
# and send every other value, including ones Ring adds later, on to classification.
SKIP_SUB_TYPES = {"human", "vehicle"}
# Playground simulation: every Playground Motion event gets a random clip from the motion pool
# instead of the Playground's own video. See poller.py for how Motion events are recognized.


@dataclass
class Job:
    request_id: str
    device_id: str
    ts_ms: int
    sub_type: str
    component_id: str | None
    source: str        # "ring" (webhook) or "playground" (Event History polling)
    ring_subtype: str | None = None  # Ring's own detection, read from Event History


class Pipeline:
    def __init__(self, workers: int = 3) -> None:
        self.ring = RingClient()
        self.classifier = Classifier()
        self.q: queue.Queue[Job] = queue.Queue()
        self._device_names: dict[str, str] = {}
        # Several downloaders, because each one mostly waits on Ring; SpeciesNet itself
        # runs one clip at a time behind the classifier's lock.
        for i in range(workers):
            threading.Thread(target=self._loop, name=f"pipeline-{i}", daemon=True).start()

    def warm_up(self) -> None:
        threading.Thread(target=self.classifier.load, daemon=True).start()

    def intake(self, request_id: str, event_id: str, device_id: str, account_id: str, ts_ms: int,
               sub_type: str, component_id: str | None, source: str, ring_subtype: str | None = None) -> str:
        """One motion event, from a webhook or from Event History. Returns what happened."""
        skip = sub_type in SKIP_SUB_TYPES
        is_new = store.add_event(request_id, event_id, device_id, account_id, ts_ms, sub_type,
                                 "skipped" if skip else "queued", f"Ring tagged it {sub_type}" if skip else "",
                                 source)
        if not is_new:
            return "duplicate"
        if skip:
            return "skipped"
        self.q.put(Job(request_id, device_id, ts_ms, sub_type, component_id, source, ring_subtype))
        return "queued"

    def swap_clip(self, job: Job) -> Path | None:
        """Playground simulation: a Motion event gets a random clip from the motion pool,
        because the Playground's own video is Ring's synthetic doorbell."""
        if job.source != "playground" or job.sub_type != "motion":
            return None
        pool = self.pool()
        return random.choice(pool) if pool else None

    @staticmethod
    def pool() -> list[Path]:
        return sorted(settings.motion_clips.glob("*.mp4")) if settings.motion_clips.is_dir() else []

    def ring_subtype(self, job: Job) -> str | None:
        """Ring's own detection for this event. History ids are opaque, so match on start time."""
        if job.ring_subtype:
            return job.ring_subtype
        try:
            for e in self.ring.history(job.device_id, "motion"):
                if abs(int((e.get("attributes") or {}).get("start", 0)) - job.ts_ms) <= 5000:
                    return self.ring.motion_subtype(job.device_id, e["id"])
        except RingError as e:
            log.warning("event history failed: %s", e)
        return None

    def _loop(self) -> None:
        while True:
            job = self.q.get()
            try:
                self.process(job)
            except Exception as e:  # keep the worker alive, record what went wrong
                log.exception("job %s failed", job.request_id)
                store.set_event(job.request_id, "error", str(e)[:300])

    def device_name(self, device_id: str) -> str:
        if device_id not in self._device_names:
            try:
                for d in self.ring.devices():
                    self._device_names[d["id"]] = (d.get("attributes") or {}).get("name") or d["id"]
            except RingError as e:
                log.warning("device list failed: %s", e)
        return self._device_names.get(device_id, device_id[-12:])

    def process(self, job: Job) -> None:
        if self.ring.is_encrypted(job.device_id):
            store.set_event(job.request_id, "encrypted", "video is encrypted on this camera, Ring cannot share it")
            return
        loc = self.ring.location(job.device_id)
        country = loc.get("country") or settings.default_country
        state = loc.get("state") or settings.default_state or None

        work = Path(tempfile.mkdtemp(prefix="trailcam_job_"))
        try:
            media = "clip"
            clip_path = work / "clip.mp4"
            swap = self.swap_clip(job)
            if swap:
                media = "simulated"
                shutil.copy(swap, clip_path)
                have_clip = True
            else:
                store.set_event(job.request_id, "working", "waiting for Ring's recording")
                try:
                    clip = self.ring.download_clip_after_event(job.device_id, job.ts_ms, settings.clip_ms,
                                                               job.component_id)
                except RingError as e:
                    log.warning("clip download failed: %s", e)
                    clip = None
                have_clip = clip is not None
                if have_clip:
                    clip_path.write_bytes(clip.data)
            if have_clip:
                store.set_event(job.request_id, "working", "classifying clip")
                result = self.classifier.classify_clip(clip_path, country, state, keep_dir=work)
            else:
                # No recording to download. A snapshot at the event time is the next best thing.
                media = "snapshot"
                try:
                    snap = self.ring.download_snapshot(job.device_id, job.ts_ms, job.component_id)
                except RingError as e:
                    store.set_event(job.request_id, "unavailable", f"no clip or snapshot from Ring ({e.status})")
                    return
                snap_path = work / "snapshot.jpg"
                snap_path.write_bytes(snap)
                store.set_event(job.request_id, "working", "classifying snapshot")
                result = self.classifier.classify_images([snap_path], country, state)

            if not result.animal:
                why = "a person was in frame" if result.saw_human else "no animal in the clip"
                kind = "snapshot" if media == "snapshot" else "clip"
                store.set_event(job.request_id, "no_animal", f"{why}, {kind} deleted")
                return

            stem = f"{job.ts_ms}_{job.request_id[:8]}"
            clip_rel = ""
            if media != "snapshot":
                shutil.move(str(clip_path), CLIPS / f"{stem}.mp4")
                clip_rel = f"clips/{stem}.mp4"
            thumb_rel = f"thumbs/{stem}.jpg"
            make_thumb(result.best_frame, result.bbox, THUMBS / f"{stem}.jpg")

            first = not store.species_seen(result.label)
            store.add_sighting(
                request_id=job.request_id, device_id=job.device_id, device_name=self.device_name(job.device_id),
                ts_ms=job.ts_ms, label=result.label, scientific=result.scientific, score=result.score,
                clip=clip_rel, thumb=thumb_rel, first_visit=int(first), country=country, state=state or "",
                media=media, source=job.source, ring_subtype=self.ring_subtype(job),
            )
            store.set_event(job.request_id, "sighting", f"{result.label} ({result.score:.0%})")
            log.info("sighting: %s %.2f%s", result.label, result.score, " (first visit)" if first else "")
        finally:
            shutil.rmtree(work, ignore_errors=True)


def make_thumb(frame: Path, bbox: list[float] | None, out: Path, width: int = 640) -> None:
    img = cv2.imread(str(frame))
    if img is None:
        return
    h, w = img.shape[:2]
    if bbox:
        x, y, bw, bh = bbox
        p1 = (int(x * w), int(y * h))
        p2 = (int((x + bw) * w), int((y + bh) * h))
        cv2.rectangle(img, p1, p2, (80, 220, 120), max(2, w // 320))
    scale = width / w
    img = cv2.resize(img, (width, int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if ok:
        out.write_bytes(buf.tobytes())


def now_ms() -> int:
    return int(time.time() * 1000)
