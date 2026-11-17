"""Stage 4: read text (refs, values, printed voltages) with a local OCR engine.

RapidOCR (PaddleOCR models on onnxruntime) runs offline. Schematic text is either
horizontal or rotated 90 deg (reading bottom-to-top), so the crop is read twice,
upright and rotated clockwise, and each text is kept from the pass in which its
box is a horizontal line. Results are cached on disk by input hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import cv2
import numpy as np

from . import project

CACHE: Path | None = None  # override (tests); default project.cache_dir("ocr")
ENGINE = "rapidocr-3/ppocr-onnx"
PROMPT_VERSION = "ocr/v4"  # bump when pre/post-processing changes, to invalidate cache
# RapidOCR shrinks any image whose long side exceeds Global.max_side_len (2000 px)
# before detection, so a wide crop was read at a smaller scale than a narrow one
# (p401l at ~0.41 instead of 0.75) and lost most small labels. Read in tiles below
# that limit instead; the overlap holds a whole label (refs/values are < 200 px).
TILE = 1800
TILE_OVERLAP = 240

_engine = None


@dataclass
class Text:
    text: str
    x: int
    y: int
    w: int
    h: int
    score: float
    vertical: bool  # True if printed rotated 90 deg on the page
    kind: str = "other"  # ref | value | voltage | other
    norm: str = ""  # normalized text for kind (e.g. "QM16", "-13.1")

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR

        _engine = RapidOCR()
    return _engine


def _run(img: np.ndarray) -> list[tuple[np.ndarray, str, float]]:
    r = _get_engine()(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
    if r.boxes is None:
        return []
    return list(zip(r.boxes, r.txts, r.scores))


def _tiles(n: int) -> list[int]:
    if n <= TILE:
        return [0]
    step = TILE - TILE_OVERLAP
    starts = list(range(0, n - TILE, step)) + [n - TILE]
    return sorted(set(starts))


def _read_tiled(img: np.ndarray) -> list[Text]:
    """`_read_both` over tiles small enough to be read at full scale, merged."""
    h, w = img.shape
    out: list[tuple[int, Text]] = []
    origins = [(tx, ty) for ty in _tiles(h) for tx in _tiles(w)]
    for tile, (tx, ty) in enumerate(origins):
        for t in _read_both(img[ty:ty + TILE, tx:tx + TILE]):
            t.x, t.y = t.x + tx, t.y + ty
            out.append((tile, t))
    return _dedupe_tiles(out)


def _dedupe_tiles(texts: list[tuple[int, Text]]) -> list[Text]:
    """Overlapping tiles read the same label twice (or once whole, once cut at the
    tile edge): keep the longest reading of each place. Only readings from different
    tiles are merged; within a tile, stacked labels legitimately touch."""
    kept: list[tuple[int, Text]] = []
    for tile, t in sorted(texts, key=lambda it: (-len(it[1].text), -it[1].score)):
        if all(tile == kt or t.vertical != k.vertical or _overlap(t, k) < 0.5 for kt, k in kept):
            kept.append((tile, t))
    return [t for _, t in kept]


def _read_both(crop: np.ndarray) -> list[Text]:
    h, w = crop.shape
    out: list[Text] = []
    for box, txt, sc in _run(crop):
        xs, ys = box[:, 0], box[:, 1]
        bw, bh = xs.max() - xs.min(), ys.max() - ys.min()
        if bw >= bh * 0.9 or len(txt) == 1:
            out.append(Text(txt, int(xs.min()), int(ys.min()), int(bw), int(bh), float(sc), False))
    # Rotated clockwise: rotated (u, v) -> original (x, y) = (v, h - 1 - u)
    for box, txt, sc in _run(cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)):
        us, vs = box[:, 0], box[:, 1]
        if us.max() - us.min() < (vs.max() - vs.min()) * 0.9 or len(txt) == 1:
            continue  # single characters are taken from the upright pass only
        x0, x1 = vs.min(), vs.max()
        y0, y1 = h - 1 - us.max(), h - 1 - us.min()
        out.append(Text(txt, int(x0), int(y0), int(x1 - x0), int(y1 - y0), float(sc), True))
    return _dedupe(out)


def _overlap(a: Text, b: Text) -> float:
    ix = max(0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    return ix * iy / max(1, min(a.w * a.h, b.w * b.h))


def _dedupe(texts: list[Text]) -> list[Text]:
    """Drop texts read by both passes. Only cross-orientation overlaps are duplicates:
    within one pass, stacked labels (e.g. "QM15" above "2SA970") legitimately touch."""
    kept: list[Text] = []
    for t in sorted(texts, key=lambda t: (-len(t.text), -t.score)):
        if all(t.vertical == k.vertical or _overlap(t, k) < 0.5 for k in kept):
            kept.append(t)
    return kept


# --- classification -------------------------------------------------------

# Character confusions seen on this scan, applied only inside a pattern that
# pins down what the character must be (e.g. the letter after a ref prefix).
_REF = re.compile(r"^([RCDQJL])([MN]?)([0-9OIl]{2,3})(-\d)?$")
_VOLT = re.compile(r"^([-+]?)(\d{1,2})[.,](\d{1,2})V$")
_ZERO_VOLT = re.compile(r"^[0O]V$")  # "0V"; other integers + V are capacitor ratings
_PART = re.compile(r"^(2S[ABCJK]\d{3,4}|1SS\d{3}|NJM\d{4}\w*)$")
_VALUE = re.compile(r"^(\d+(?:\.\d+)?)([kKM]?)$")


def _fix_digits(s: str) -> str:
    return s.replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1")


def _cache() -> Path:
    return CACHE or project.cache_dir("ocr")


def classify(t: Text) -> Text:
    s = t.text.strip().replace(" ", "")
    s = re.sub(r"^0(?=M\d)", "Q", s)  # "0M16" -> "QM16": refs never start with a digit
    s = re.sub(r"^0(?=\d{3}$)", "Q", s)  # "0501" -> "Q501": integer values never have a leading 0
    # A wire end touching the minus sign ("|-") reads as "F": P501 R522's "-0.04V" -> "F0.04V".
    s = re.sub(r"^F(?=\d{1,2}[.,]\d{1,2}V$)", "-", s)
    if _ZERO_VOLT.match(s):
        t.kind, t.norm = "voltage", "0.0"
        return t
    if m := _VOLT.match(s):
        sign, a, b = m.groups()
        t.kind, t.norm = "voltage", f"{'-' if sign == '-' else ''}{int(a)}.{b}"
        return t
    if m := _REF.match(s):
        letter, mid, num, suffix = m.groups()
        # Module refs (Marantz "QM13"): the project names the letter, and an OCR'd
        # "QN13" is read with it. Without one the letter stays as read.
        mod = (p := project.optional()) and p.module_ref_letter
        mid = mod if (mid and mod) else mid
        t.kind, t.norm = "ref", f"{letter}{mid}{_fix_digits(num)}{suffix or ''}"
        return t
    if _PART.match(s):
        t.kind, t.norm = "value", s
        return t
    if m := _VALUE.match(s):
        num, mult = m.groups()
        t.kind, t.norm = "value", num + {"K": "k"}.get(mult, mult)
        return t
    return t


# The PP-OCR detector is tuned for smaller text than this 600 ppi scan; on PM11,
# scale 0.75 found 16/18 refs vs 14 (1.0), 13 (0.5), 13 (1.5); voltages 11/11.
DEFAULT_SCALE = 0.75


def read_text(crop: np.ndarray, scale: float = DEFAULT_SCALE, n_refs: bool = False) -> list[Text]:
    """OCR a grayscale crop. Coordinates are relative to the crop. Cached by input hash.
    `classify` reads "QN13" as "QM13" (page 4: module refs, M misread as N). A sheet with
    real N refs (P701 QN01, RN25: `n_refs: true` in crops.yaml) keeps the N as read."""
    key = hashlib.sha256(
        crop.tobytes() + str((crop.shape, scale)).encode() + PROMPT_VERSION.encode()
    ).hexdigest()[:24]
    path = _cache() / f"{key}.json"
    if path.exists():
        texts = [Text(**d) for d in json.loads(path.read_text())]
    else:
        img = crop if scale == 1 else cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        texts = _read_tiled(img)
        for t in texts:
            t.x, t.y, t.w, t.h = (round(v / scale) for v in (t.x, t.y, t.w, t.h))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([asdict(t) for t in texts]))
    out = [w for t in texts for w in _split_words(classify(t))]
    if n_refs:
        for t in out:
            if t.kind == "ref" and re.match(r"^[RCDQJL]N", t.text.strip()):
                t.norm = t.norm[0] + "N" + t.norm[2:]
    return out


def _split_words(t: Text) -> list[Text]:
    """Adjacent labels on one line come back fused ("-0.04V 10/50V": an output voltage
    and a capacitor rating). If the whole text means nothing, classify each word on
    its own, with a box cut from the line in proportion to its characters (vertical
    text reads bottom to top)."""
    words = t.text.split()
    if t.kind != "other" or len(words) < 2:
        return [t]
    out, n, pos = [], len(t.text), 0
    for w in words:
        start = t.text.index(w, pos)
        pos = start + len(w)
        a, b = start / n, pos / n
        if t.vertical:
            box = dict(x=t.x, w=t.w, y=t.y + round(t.h * (1 - b)), h=max(1, round(t.h * (b - a))))
        else:
            box = dict(y=t.y, h=t.h, x=t.x + round(t.w * a), w=max(1, round(t.w * (b - a))))
        out.append(classify(Text(w, score=t.score, vertical=t.vertical, **box)))
    return out


def read_page(img: np.ndarray) -> list[Text]:
    """OCR an upright text page (parts list) at native scale, one pass. Cached by input hash.
    Texts are returned unclassified: table parsing has its own rules."""
    key = hashlib.sha256(img.tobytes() + str(img.shape).encode() + b"page/v1").hexdigest()[:24]
    path = _cache() / f"page-{key}.json"
    if path.exists():
        return [Text(**d) for d in json.loads(path.read_text())]
    texts = []
    for box, txt, sc in _run(img):
        xs, ys = box[:, 0], box[:, 1]
        texts.append(Text(txt, int(xs.min()), int(ys.min()), int(xs.max() - xs.min()),
                          int(ys.max() - ys.min()), float(sc), False))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(t) for t in texts]))
    return texts


# --- association -----------------------------------------------------------

# Ref prefix -> symbol types it can name (connectors are "J").
_PREFIX_TYPES = {
    "R": {"resistor", "potentiometer", "resistor_ct"},
    "C": {"capacitor", "capacitor_polarized"},
    "D": {"diode", "led"},
    "Q": {"npn", "pnp", "njfet", "darlington", "photocoupler"},
    "J": {"connector"},
}
_SEMIS = {"diode", "npn", "pnp", "njfet", "led", "darlington", "photocoupler"}


def _ref_letters(t: Text) -> set[str]:
    """Letters a ref text may stand for. `classify` turns a leading "0" into Q ("0501"
    -> Q501), but OCR reads D that way too (P701 "0705" for D705, where Q705 also
    exists), so such a text may label a diode as well; the symbol decides."""
    letters = {t.norm[0]}
    if t.norm[0] == "Q" and t.text.strip().startswith("0"):
        letters.add("D")
    return letters
ASSOC_DIST = 150  # px (page scale): max text-center to symbol-edge distance


def _edge_dist(t: Text, box: tuple[float, float, float, float]) -> float:
    tx, ty = t.center
    x, y, w, h = box
    dx = max(x - tx, 0, tx - (x + w))
    dy = max(y - ty, 0, ty - (y + h))
    return (dx * dx + dy * dy) ** 0.5


def _value_fits(t: Text, typ: str) -> bool:
    is_part = bool(_PART.match(t.norm))
    return is_part if typ in _SEMIS else (not is_part and typ != "connector")


def associate(items: list[tuple[str, tuple[float, float, float, float]]], texts: list[Text],
              max_dist: float = ASSOC_DIST) -> list[tuple[Text | None, Text | None]]:
    """Give each item (type, bbox) its nearest compatible ref and value text.

    Greedy by distance over all pairs, so each text labels at most one item.
    Coordinates of bboxes and texts must be in the same frame.
    """
    out: list[list[Text | None]] = [[None, None] for _ in items]
    for slot, kind in ((0, "ref"), (1, "value")):
        pairs = []
        for i, (typ, box) in enumerate(items):
            for j, t in enumerate(texts):
                if t.kind != kind:
                    continue
                if kind == "ref" and not any(typ in _PREFIX_TYPES.get(L, ()) for L in _ref_letters(t)):
                    continue
                if kind == "value" and not _value_fits(t, typ):
                    continue
                d = _edge_dist(t, box)
                if d <= max_dist:
                    pairs.append((d, i, j))
        used_i, used_j = set(), set()
        for _, i, j in sorted(pairs):
            if i not in used_i and j not in used_j:
                used_i.add(i)
                used_j.add(j)
                t = texts[j]
                if kind == "ref" and items[i][0] not in _PREFIX_TYPES.get(t.norm[0], ()):
                    t = replace(t, norm="D" + t.norm[1:])  # a "0..." ref on a diode
                out[i][slot] = t
    return [tuple(o) for o in out]
