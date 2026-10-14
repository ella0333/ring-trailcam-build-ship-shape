"""Find and name animals in a clip with Google SpeciesNet.

A clip is sampled into a handful of frames. SpeciesNet runs MegaDetector on each
frame, crops every animal it finds and classifies the crop, so the Ring watermark
in the corner never reaches the species classifier. The best animal frame wins.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2

log = logging.getLogger(__name__)

MODEL = "kaggle:google/speciesnet/pyTorch/v4.0.3a/1"
FRAMES_PER_CLIP = 8

# SpeciesNet's geofence wants ISO 3166-1 alpha-3, Ring's location endpoint gives alpha-2.
ISO2_TO_ISO3 = {
    "US": "USA", "CA": "CAN", "MX": "MEX", "GB": "GBR", "IE": "IRL", "FR": "FRA",
    "DE": "DEU", "ES": "ESP", "IT": "ITA", "NL": "NLD", "BE": "BEL", "AT": "AUT",
    "CH": "CHE", "SE": "SWE", "NO": "NOR", "DK": "DNK", "FI": "FIN", "PL": "POL",
    "PT": "PRT", "AU": "AUS", "NZ": "NZL", "JP": "JPN", "IN": "IND", "BR": "BRA",
    "ZA": "ZAF", "PR": "PRI",
}

NOT_ANIMALS = {"blank", "human", "vehicle", "no cv result"}


@dataclass
class Result:
    animal: bool
    label: str = ""            # common name, e.g. "white-tailed deer"
    scientific: str = ""       # e.g. "odocoileus virginianus"
    taxon: str = ""            # SpeciesNet's full label string
    score: float = 0.0
    best_frame: Path | None = None
    bbox: list[float] | None = None   # normalized [x, y, w, h] of the animal in best_frame
    second: str = ""           # the classifier's next answer for the best frame, e.g. "great blue heron"
    second_score: float = 0.0
    saw_human: bool = False
    frames: list[dict] = field(default_factory=list)


def parse_label(label: str) -> tuple[str, str]:
    """SpeciesNet labels look like 'uuid;class;order;family;genus;species;common name'."""
    parts = label.split(";")
    common = parts[-1] if parts else label
    genus, species = (parts[4], parts[5]) if len(parts) >= 7 else ("", "")
    scientific = f"{genus} {species}".strip()
    return common, scientific


def runner_up(pred: dict, chosen: str) -> tuple[str, float]:
    """The classifier's best answer other than the one SpeciesNet chose, skipping blank,
    human and vehicle."""
    c = pred.get("classifications") or {}
    for label, score in zip(c.get("classes", []), c.get("scores", [])):
        name = parse_label(label)[0]
        if name != chosen and name not in NOT_ANIMALS:
            return name, float(score)
    return "", 0.0


def sample_frames(clip: Path, out_dir: Path, n: int = FRAMES_PER_CLIP) -> list[Path]:
    cap = cv2.VideoCapture(str(clip))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    paths: list[Path] = []
    if total <= 0:
        cap.release()
        return paths
    picks = sorted({int(total * (i + 0.5) / n) for i in range(n)})
    for idx in picks:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        p = out_dir / f"{clip.stem}_f{idx:05d}.jpg"
        # yolov5 (a SpeciesNet dependency) replaces cv2.imwrite, so encode and write directly.
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            continue
        p.write_bytes(buf.tobytes())
        paths.append(p)
    cap.release()
    return paths


class Classifier:
    """Loads SpeciesNet once and serializes calls to it."""

    def __init__(self, model: str = MODEL):
        self.model_name = model
        self._model = None
        self._lock = threading.RLock()

    def load(self):
        # Loading isn't thread safe, so it shares the inference lock.
        with self._lock:
            if self._model is None:
                from speciesnet import SpeciesNet

                log.info("loading SpeciesNet %s", self.model_name)
                self._model = SpeciesNet(self.model_name)
            return self._model

    def classify_images(self, images: list[Path], country: str | None, state: str | None) -> Result:
        iso3 = ISO2_TO_ISO3.get((country or "").upper(), country)
        admin1 = state if iso3 == "USA" else None
        with self._lock:
            model = self.load()
            out = model.predict(
                filepaths=[str(p) for p in images],
                country=iso3,
                admin1_region=admin1,
                run_mode="single_thread",
                progress_bars=False,
            )
        result = Result(animal=False)
        best = None
        for pred in (out or {}).get("predictions", []):
            label = pred.get("prediction", "")
            score = float(pred.get("prediction_score") or 0.0)
            common, scientific = parse_label(label)
            result.frames.append({"file": Path(pred["filepath"]).name, "label": common, "score": score})
            if common == "human":
                result.saw_human = True
            if common in NOT_ANIMALS or not label:
                continue
            # Prefer species-level answers over genus or family rollups.
            rank = (len(scientific.split()) == 2, score)
            if best is None or rank > best[0]:
                animals = [d for d in pred.get("detections", []) if d.get("label") == "animal"]
                bbox = max(animals, key=lambda d: d.get("conf", 0))["bbox"] if animals else None
                best = (rank, score, label, common, scientific, Path(pred["filepath"]), bbox, runner_up(pred, common))
        if best:
            result.animal = True
            (_, result.score, result.taxon, result.label, result.scientific,
             result.best_frame, result.bbox, (result.second, result.second_score)) = best
        return result

    def classify_clip(self, clip: Path, country: str | None = None, state: str | None = None,
                      keep_dir: Path | None = None) -> Result:
        work = Path(tempfile.mkdtemp(prefix="trailcam_")) if keep_dir is None else keep_dir
        work.mkdir(parents=True, exist_ok=True)
        frames = sample_frames(clip, work)
        if not frames:
            return Result(animal=False)
        return self.classify_images(frames, country, state)
