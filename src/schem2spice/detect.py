"""Stage 3: symbol detection.

Local detector by template matching. A trained detector (YOLO etc.) needs
hundreds of labelled symbols and we have one hand-labelled module; this manual
is drawn by one draftsman at one scale with stencilled symbols, so normalized
cross-correlation against templates cut from the same sheet is accurate and
needs no training. Detections can later seed labels for a trained model.

Templates live in symbols/templates/<type>.png (a symbol drawn two ways has a second
file <type>@2.png, whose pins are under `drawings:` in the library), cut from PM21/JM21 (page 4) so
the PM11 golden module stays unseen. Each is tried in the 8 rotations/mirrors.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .ir import Component

from . import project

DETECTOR = "template-match/v1"

# Score thresholds, set from the PM11 golden scope (page 4): true symbols scored
# >= 0.79, false hits (wire crossings/junctions) <= 0.66. Ground has no true
# example in that scope yet and its false hits reach 0.74, hence stricter.
#
# opamp: the template (Q401-2) has no supply circle; on Q401-1 the V+ circle sits on
# the top edge and costs score: Q401-1 0.70, Q401-2 0.79, best false hit on all of
# page 4 < 0.55.
#
# capacitor: P601 C606 (page 6, plates drawn slightly wider apart) scores 0.73; at 0.70
# no new hit appears on p501l, p401 or p601 besides it.
#
# connector_pin: on P501 J505 (page 4) the dashed board outline runs through the pin
# circles; they score 0.72-0.76 (J504 next to it 0.85-0.89). At 0.70 the two missing
# J505 pins are the only new hits on p501, p401 and p601.
#
# resistor: P701 R736 (page 7) has the printed "-52.9V" against its zigzag and scores
# 0.746 (twin R735 0.92). At 0.70 it is the only new hit on p501, p401, p601 and p701.
#
# capacitor_polarized: P701 CN05 (0.47/50V, drawn smaller) scores 0.722; at 0.68 it is
# the only new hit on p501, p401, p601 and p701.
THRESHOLDS = {"ground": 0.85, "opamp": 0.6, "capacitor": 0.70, "connector_pin": 0.70, "resistor": 0.72,
              "capacitor_polarized": 0.70}
DEFAULT_THRESHOLD = 0.75

# A potentiometer is a resistor plus a wiper, so the resistor template also matches
# pots, sometimes a hair better (P601 R625-1: resistor 0.855, pot 0.852). Where they
# overlap, a pot that clears its own threshold wins: NMS ranks it with this bonus.
# The reported score is unchanged.
NMS_BONUS = {"potentiometer": 0.1}

# Symbols drawn around smaller ones (the zigzag of a centre-tapped resistor, the diode
# of an LED or photocoupler, the transistors of a Darlington): a detection lying mostly
# inside one of these is part of it, whatever its score.
CONTAINERS = {"led", "darlington", "darlington_pnp", "photocoupler", "resistor_ct", "rc_snubber", "regulator"}
CONTAINED = 0.6  # fraction of a detection's area inside a container that makes it a part of it

# Symbols that mark nets or connectors rather than parts; `trace` consumes them.
NON_COMPONENTS = {"ground", "connector_pin", "rca_jack", "warning"}


@dataclass
class Detection:
    type: str
    x: int
    y: int
    w: int
    h: int
    score: float
    variant: str  # e.g. "r90", "r0f" (rotation, f = mirrored)
    template: str = ""  # template file stem when a type has several ("darlington@2")

    @property
    def stem(self) -> str:
        return self.template or self.type

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2


def _variants(t: np.ndarray) -> list[tuple[str, np.ndarray]]:
    out, seen = [], set()
    for flip in (False, True):
        base = cv2.flip(t, 1) if flip else t
        for k in range(4):
            v = np.ascontiguousarray(np.rot90(base, k))
            key = v.tobytes() + str(v.shape).encode()
            if key not in seen:  # symmetric symbols produce duplicate variants
                seen.add(key)
                out.append((f"r{90 * k}{'f' if flip else ''}", v))
    return out


def load_templates(directory: Path | None = None) -> dict[str, np.ndarray]:
    """Templates by stem from `directory`, else from the project's symbols/templates
    overlay and the shared library (a project template replaces a shared one)."""
    dirs = [directory] if directory else [d / "templates" for d in reversed(project.symbol_dirs())]
    out: dict[str, np.ndarray] = {}
    for d in dirs:
        out.update({p.stem: cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) for p in sorted(d.glob("*.png"))})
    return out


def _nms(dets: list[Detection], overlap: float) -> list[Detection]:
    """Greedy non-maximum suppression across all classes (one symbol per place)."""
    kept: list[Detection] = []
    for d in sorted(dets, key=lambda d: -(d.score + NMS_BONUS.get(d.type, 0))):
        if all(_iou(d, k) < overlap for k in kept):
            kept.append(d)
    return kept


def _iou(a: Detection, b: Detection) -> float:
    ix = max(0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    return inter / (a.w * a.h + b.w * b.h - inter)


def detect_symbols(
    page: np.ndarray,
    templates: dict[str, np.ndarray] | None = None,
    thresholds: dict[str, float] | None = None,
    overlap: float = 0.3,
) -> list[Detection]:
    """Find symbols in a grayscale page (or crop). Coordinates are relative to `page`."""
    templates = templates or load_templates()
    thresholds = {**THRESHOLDS, **(thresholds or {})}
    raw: list[Detection] = []
    for stem, tmpl in templates.items():
        typ = stem.split("@")[0]  # "darlington@2": a second drawing of the same symbol
        threshold = thresholds.get(typ, DEFAULT_THRESHOLD)
        for name, v in _variants(tmpl):
            h, w = v.shape
            if h > page.shape[0] or w > page.shape[1]:
                continue  # this rotation does not fit in the crop
            res = cv2.matchTemplate(page, v, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= threshold)
            cand = [Detection(typ, int(x), int(y), w, h, float(res[y, x]), name, stem if stem != typ else "")
                    for y, x in zip(ys, xs)]
            raw.extend(_nms(cand, overlap))  # thin each variant's plateau first
    kept = _nms(raw, overlap)
    boxes = [d for d in kept if d.type in CONTAINERS]
    return [d for d in kept if d.type in CONTAINERS
            or all(_inside(d, b) < CONTAINED for b in boxes)]


def _inside(a: Detection, b: Detection) -> float:
    """Fraction of a's area inside b."""
    ix = max(0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    return ix * iy / (a.w * a.h)


def to_components(dets: list[Detection], offset: tuple[int, int] = (0, 0)) -> list[Component]:
    ox, oy = offset
    return [
        Component(
            ref=f"?{i}",
            type=d.type,
            value="",
            pins={},
            bbox=(d.x + ox, d.y + oy, d.w, d.h),
            confidence=round(d.score, 3),
            note=f"{DETECTOR} {d.variant}",
        )
        for i, d in enumerate(dets, 1)
        if d.type not in NON_COMPONENTS
    ]
