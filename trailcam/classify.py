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
# SpeciesNet's fallback when it can't name the animal. Never shown; the next answer is used.
GENERIC = "animal"
MIN_BOX = 0.5        # detector confidence for counting a box as a separate animal
MAX_ANIMALS = 5


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
    saw_vehicle: bool = False
    # Every animal in best_frame: {"label", "scientific", "score", "bbox"}, highest score first.
    animals: list[dict] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)


def parse_label(label: str) -> tuple[str, str]:
    """SpeciesNet labels look like 'uuid;class;order;family;genus;species;common name'."""
    parts = label.split(";")
    common = parts[-1] if parts else label
    genus, species = (parts[4], parts[5]) if len(parts) >= 7 else ("", "")
    scientific = f"{genus} {species}".strip()
    return common, scientific


def animal_answers(pred: dict) -> list[tuple[str, str, float]]:
    """The classifier's animal answers in order as (common, scientific, score), without
    blank, human, vehicle or the generic "animal"."""
    c = pred.get("classifications") or {}
    out = []
    for label, score in zip(c.get("classes", []), c.get("scores", [])):
        common, scientific = parse_label(label)
        if common not in NOT_ANIMALS and common != GENERIC:
            out.append((common, scientific, float(score)))
    return out


def named(pred: dict) -> tuple[str, str, float, str, float]:
    """SpeciesNet's answer with the generic "animal" replaced by the classifier's next answer.
    Returns (common, scientific, score, second, second_score)."""
    common, scientific = parse_label(pred.get("prediction", ""))
    score = float(pred.get("prediction_score") or 0.0)
    answers = animal_answers(pred)
    if common == GENERIC and answers:
        common, scientific, score = answers[0]
    rest = [a for a in answers if a[0] != common]
    second, second_score = (rest[0][0], rest[0][2]) if rest else ("", 0.0)
    return common, scientific, score, second, second_score


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

    def _predict(self, images: list[Path], iso3: str | None, admin1: str | None) -> list[dict]:
        with self._lock:
            out = self.load().predict(
                filepaths=[str(p) for p in images],
                country=iso3,
                admin1_region=admin1,
                run_mode="single_thread",
                progress_bars=False,
            )
        return (out or {}).get("predictions", [])

    def classify_images(self, images: list[Path], country: str | None, state: str | None) -> Result:
        iso3 = ISO2_TO_ISO3.get((country or "").upper(), country)
        admin1 = state if iso3 == "USA" else None
        result = Result(animal=False)
        best = None
        for pred in self._predict(images, iso3, admin1):
            common, scientific, score, second, second_score = named(pred)
            result.frames.append({"file": Path(pred["filepath"]).name, "label": common, "score": score})
            if common == "human":
                result.saw_human = True
            if common == "vehicle":
                result.saw_vehicle = True
            if common in NOT_ANIMALS or not pred.get("prediction"):
                continue
            boxes = sorted((d for d in pred.get("detections", []) if d.get("label") == "animal"),
                           key=lambda d: d.get("conf", 0), reverse=True)
            count = sum(1 for d in boxes if d.get("conf", 0) >= MIN_BOX)
            # The frame with the most animals wins, then species-level answers, then score.
            rank = (count, len(scientific.split()) == 2, score)
            if best is None or rank > best[0]:
                best = (rank, common, scientific, score, second, second_score, Path(pred["filepath"]), boxes)
        if not best:
            return result
        _, common, scientific, score, second, second_score, frame, boxes = best
        result.animal = True
        result.label, result.scientific, result.score = common, scientific, score
        result.second, result.second_score, result.best_frame = second, second_score, frame
        result.bbox = boxes[0]["bbox"] if boxes else None
        result.animals = [{"label": common, "scientific": scientific, "score": score, "bbox": result.bbox}]

        strong = [d for d in boxes if d.get("conf", 0) >= MIN_BOX][:MAX_ANIMALS]
        if len(strong) > 1:
            result.animals = self._name_each(frame, strong, iso3, admin1) or result.animals
            top = result.animals[0]
            result.label, result.scientific, result.score = top["label"], top["scientific"], top["score"]
            result.bbox = top["bbox"]
            result.second, result.second_score = "", 0.0
        return result

    def _name_each(self, frame: Path, boxes: list[dict], iso3: str | None, admin1: str | None) -> list[dict]:
        """Classify every animal box in a frame separately by cropping it out."""
        img = cv2.imread(str(frame))
        if img is None:
            return []
        h, w = img.shape[:2]
        crops = []
        for i, d in enumerate(boxes):
            x, y, bw, bh = d["bbox"]
            pad_x, pad_y = bw * 0.15, bh * 0.15
            x1, y1 = max(0, int((x - pad_x) * w)), max(0, int((y - pad_y) * h))
            x2, y2 = min(w, int((x + bw + pad_x) * w)), min(h, int((y + bh + pad_y) * h))
            ok, buf = cv2.imencode(".jpg", img[y1:y2, x1:x2], [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                continue
            p = frame.with_name(f"{frame.stem}_animal{i}.jpg")
            p.write_bytes(buf.tobytes())
            crops.append((p, d["bbox"]))
        named_boxes = {Path(p.get("filepath", "")).name: p for p in self._predict([c[0] for c in crops], iso3, admin1)}
        animals = []
        for path, bbox in crops:
            pred = named_boxes.get(path.name)
            if not pred:
                continue
            common, scientific, score, _, _ = named(pred)
            if common in NOT_ANIMALS or not common:
                answers = animal_answers(pred)
                if not answers:
                    continue
                common, scientific, score = answers[0]
            animals.append({"label": common, "scientific": scientific, "score": score, "bbox": bbox})
        return sorted(animals, key=lambda a: a["score"], reverse=True)

    def classify_clip(self, clip: Path, country: str | None = None, state: str | None = None,
                      keep_dir: Path | None = None) -> Result:
        work = Path(tempfile.mkdtemp(prefix="trailcam_")) if keep_dir is None else keep_dir
        work.mkdir(parents=True, exist_ok=True)
        frames = sample_frames(clip, work)
        if not frames:
            return Result(animal=False)
        return self.classify_images(frames, country, state)
