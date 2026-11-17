"""Stage 5: trace wires into nets (classical, no learned model).

1. Erase what is not wire: each detected symbol (its matched template ink,
   dilated), each connector bracket, and each OCR text box except strokes that
   leave the box or touch a pin (wires and arrows under a label).
2. Junction dots: blobs that survive an opening with a disk wider than any
   wire (wires <= ~12 px, dots ~28-32 px at 600 ppi).
3. Arrows: thin diagonal strokes (label -> wire pointers), rejoined where a
   wire they cross cut them; then erased so their heads are not taken for wire.
4. Wires: openings with long horizontal / vertical kernels give H and V segments.
5. Connectivity: an H and a V segment that meet are joined only if the meeting
   point is at the end of one of them (corner or T) or on a dot. A meeting in
   the middle of both is a crossing and stays unconnected.
6. Pins: each symbol pin (from `template_pins` in symbols/library.yaml,
   transformed by the matched rotation/mirror) joins every segment or dot
   within a few pixels. Connector pins are stacked into connectors; facing
   header pairs (board | module) are mated pin for pin.

`to_ir` then names nets, attaches refs/values (ocr.associate) and printed
voltages (via their arrow, else the nearest wire). Coordinates here are
relative to the crop; `to_ir` adds the page offset.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .detect import DETECTOR, NON_COMPONENTS, Detection, _variants, load_templates
from .ir import IR, Annotation, Component, Connector, derive_nets
from .netlist import load_library
from .ocr import ENGINE, PROMPT_VERSION, Text, associate

TRACER = "classical-hv/v1"

INK = 128  # gray < INK is ink
WIRE_MIN = 25  # px: shortest wire segment kept (longer than any glyph stroke after text erase)
DOT_DISK = 17  # px: opening disk; wider than wires, narrower than dots
DOT_SIZE = (20, 45)  # px: accepted dot bbox side
END_TOL = 8  # px: a meeting within this of a segment end counts as "at the end"
PIN_TOL = 10  # px: pin-to-wire attach distance
ERASE_PAD = 3  # px: dilation of erased symbol ink
MARKS = {"warning"}  # symbols that mark a part (not wired): erased, wires through them kept
ARROW_MIN = 40  # px: shortest arrow kept
ARROW_PIECE = 25  # px: shortest shaft piece (arrows are cut where they cross wires)
ARROW_WIRE = 61  # px: wire kernel when looking for arrows (longer than a shallow arrow's head)
ARROW_GAP = 35  # px: max gap between pieces of one arrow
ARROW_ANGLE = 10  # deg: arrow shafts are at least this far from horizontal/vertical
HV_ANGLE = 3  # deg: a thin piece this close to horizontal/vertical is a wire stub
ARROW_THIN = 0.35  # max ratio of the 2nd to 1st principal axis spread
ARROW_HEAD = 1.3  # min ink ratio between the head end and the tail end
MATE_GAP = 80  # px: max gap between the brackets of a mated header pair
TEXT_MARGIN = 3  # px: a stroke reaching this far outside a text box is a wire or arrow, not a glyph
LEAVE = 40  # px: window around a text box in which strokes are followed


@dataclass
class Segment:
    x0: int
    y0: int
    x1: int  # inclusive
    y1: int
    horizontal: bool

    def near(self, x: float, y: float, tol: float) -> bool:
        return self.x0 - tol <= x <= self.x1 + tol and self.y0 - tol <= y <= self.y1 + tol


@dataclass
class Pin:
    owner: int  # index into detections
    name: str
    x: float
    y: float


@dataclass
class TraceResult:
    segments: list[Segment]
    dots: list[tuple[int, int, int, int]]  # x, y, w, h
    pins: list[Pin]
    net_of_pin: dict[tuple[int, str], int]  # (detection index, pin name) -> net id
    floating: list[Pin] = field(default_factory=list)  # pins that touch no wire
    nets: dict[int, list[int]] = field(default_factory=dict)  # net id -> segment indices
    connectors: list[list[int]] = field(default_factory=list)  # connector_pin detection indices, pin 1 first
    arrows: list[tuple[tuple[float, float], tuple[float, float]]] = field(default_factory=list)  # (tip, tail)
    mated: list[tuple[int, int]] = field(default_factory=list)  # pairs of indices into `connectors`


# --- geometry -------------------------------------------------------------

def _transform(x: float, y: float, w: int, h: int, variant: str) -> tuple[float, float]:
    """Map a point of the unrotated template (w x h) into the variant used by detect._variants:
    optional horizontal flip, then np.rot90 k times (counterclockwise)."""
    k = int(variant[1:].rstrip("f")) // 90
    if variant.endswith("f"):
        x = w - 1 - x
    for _ in range(k):
        x, y, w, h = y, w - 1 - x, h, w
    return x, y


def symbol_pins(dets: list[Detection], library: dict | None = None) -> list[Pin]:
    library = library or load_library()
    templates = load_templates()
    pins = []
    for i, d in enumerate(dets):
        th, tw = templates[d.stem].shape
        spec = library[d.type].get("drawings", {}).get(d.stem, library[d.type])
        for name, (px, py) in spec["template_pins"].items():
            x, y = _transform(px, py, tw, th, d.variant)
            pins.append(Pin(i, name, d.x + x, d.y + y))
    return _canonical_swaps(pins, dets, library)


def _canonical_swaps(pins: list[Pin], dets: list[Detection], library: dict) -> list[Pin]:
    """Pins drawn identically (R/C ends, JFET D/S) get names by page position: first = top/left."""
    by_owner: dict[int, list[Pin]] = {}
    for p in pins:
        by_owner.setdefault(p.owner, []).append(p)
    for i, ps in by_owner.items():
        spec = library[dets[i].type]
        swap = spec.get("drawings", {}).get(dets[i].stem, spec).get("swappable", spec.get("swappable"))
        if not swap:
            continue
        a, b = (next(p for p in ps if p.name == n) for n in swap)
        # order along the part's axis: sub-pixel y noise must not decide a horizontal part
        first_is_b = b.y < a.y if abs(b.y - a.y) > abs(b.x - a.x) else b.x < a.x
        if first_is_b:
            a.name, b.name = b.name, a.name
    return pins


# --- connectors -----------------------------------------------------------

Rect = tuple[int, int, int, int]  # x0, y0, x1, y1 (exclusive)


def group_connectors(dets: list[Detection], pins: list[Pin]) -> tuple[list[list[int]], list[Rect]]:
    """Stack connector_pin detections into connectors, pin 1 at the top (or left).
    Returns the stacks and, per stack, the bracket area to erase: everything
    between the far edge of the circles and the lead exit."""
    lead = {p.owner: p for p in pins if dets[p.owner].type == "connector_pin"}
    stacks: list[list[int]] = []
    for i in sorted(lead, key=lambda i: (dets[i].y, dets[i].x)):
        d, p = dets[i], lead[i]
        cx, cy = d.center
        vertical = abs(p.x - cx) > abs(p.y - cy)  # leads sideways -> pins stacked vertically
        # A pin joins every stack it sits next to, merging them; visiting order alone
        # is not enough, as a pin drawn 2 px lower in a horizontal header comes last
        # (P851 J855 pin 1), and one drawn 1 px higher comes first (P651 J651).
        def beside(o: Detection) -> bool:
            if vertical:
                return abs(o.x - d.x) <= 8 and 0 < abs(d.y - o.y) <= 1.5 * d.h
            return abs(o.y - d.y) <= 8 and 0 < abs(d.x - o.x) <= 1.5 * d.w
        touching = [st for st in stacks if any(beside(dets[j]) for j in st)]
        merged = [i] + [j for st in touching for j in st]
        stacks = [st for st in stacks if st not in touching] + [merged]
    for st in stacks:  # pin 1 at the top (or left)
        p, (cx, cy) = lead[st[0]], dets[st[0]].center
        st.sort(key=(lambda j: dets[j].y) if abs(p.x - cx) > abs(p.y - cy) else (lambda j: dets[j].x))
    rects = []
    for st in stacks:
        ds = [dets[i] for i in st]
        x0, y0 = min(d.x for d in ds), min(d.y for d in ds)
        x1, y1 = max(d.x + d.w for d in ds), max(d.y + d.h for d in ds)
        p, (cx, cy) = lead[st[0]], ds[0].center
        if abs(p.x - cx) > abs(p.y - cy):
            ext = ds[0].h // 2  # bracket ends above the first / below the last pin
            y0, y1 = y0 - ext, y1 + ext
            x0, x1 = (x0, int(p.x) - 2) if p.x > cx else (int(p.x) + 3, x1)
        else:
            ext = ds[0].w // 2
            x0, x1 = x0 - ext, x1 + ext
            y0, y1 = (y0, int(p.y) - 2) if p.y > cy else (int(p.y) + 3, y1)
        rects.append((x0, y0, x1, y1))
    return stacks, rects


def mate_connectors(dets: list[Detection], pins: list[Pin], stacks: list[list[int]]) -> list[tuple[int, int]]:
    """Board/module header pairs (e.g. J504 | JM11) are drawn face to face with no wire
    between them: same pin count, pins aligned, leads pointing away from each other,
    a small gap. Pin k of one is pin k of the other."""
    lead = {p.owner: p for p in pins if dets[p.owner].type == "connector_pin"}
    out = []
    for a in range(len(stacks)):
        for b in range(a + 1, len(stacks)):
            sa, sb = stacks[a], stacks[b]
            if len(sa) != len(sb):
                continue
            da, db = dets[sa[0]], dets[sb[0]]
            (pa, ca), (pb, cb) = (lead[sa[0]], da.center), (lead[sb[0]], db.center)
            if abs(pa.x - ca[0]) > abs(pa.y - ca[1]):  # vertical stacks, leads sideways
                aligned = all(abs(dets[i].y - dets[j].y) <= 10 for i, j in zip(sa, sb))
                left, right = (pa, pb) if ca[0] < cb[0] else (pb, pa)
                lc, rc = (ca, cb) if ca[0] < cb[0] else (cb, ca)
                facing = left.x < lc[0] and right.x > rc[0]
                gap = abs(cb[0] - ca[0]) - da.w
            else:
                aligned = all(abs(dets[i].x - dets[j].x) <= 10 for i, j in zip(sa, sb))
                top, bot = (pa, pb) if ca[1] < cb[1] else (pb, pa)
                tc, bc = (ca, cb) if ca[1] < cb[1] else (cb, ca)
                facing = top.y < tc[1] and bot.y > bc[1]
                gap = abs(cb[1] - ca[1]) - da.h
            if aligned and facing and gap <= MATE_GAP:
                out.append((a, b))
    return out


# --- image ----------------------------------------------------------------

BOX_SEARCH = 30  # px: how far outside a jack block its box outline may lie
BOX_FILL = 0.5  # fraction of a side's length that must be ink (dashed sides reach ~0.6)
CROSS = 6  # px: a wire crossing a box side continues this far on both sides


def erase_jack_boxes(ink: np.ndarray, dets: list[Detection]) -> np.ndarray:
    """Erase the rectangle drawn around a block of RCA sockets (J401), keeping the
    wires that cross it. Unlike a connector bracket the area cannot be blanked: the
    shield wires run through the box to the S pins."""
    out = ink.copy()
    H, W = ink.shape
    for block in _jack_blocks(dets):
        ds = [dets[i] for i in block]
        x0, y0 = min(d.x for d in ds), min(d.y for d in ds)
        x1, y1 = max(d.x + d.w for d in ds), max(d.y + d.h for d in ds)
        ya, yb = max(0, y0 - BOX_SEARCH), min(H, y1 + BOX_SEARCH)
        xa, xb = max(0, x0 - BOX_SEARCH), min(W, x1 + BOX_SEARCH)

        def best(cands, frac):
            ok = [(f, c) for c, f in ((c, frac(c)) for c in cands) if f >= BOX_FILL]
            return [c for f, c in ok if f >= 0.6 * max(ok)[0]] if ok else []

        left = best(range(xa, min(W, x0 + 8)), lambda c: ink[ya:yb, c].mean())
        right = best(range(max(0, x1 - 8), xb), lambda c: ink[ya:yb, c].mean())
        if not left:
            continue
        lo, hi = min(left), max(right) if right else x1
        top = best(range(ya, min(H, y0 + 8)), lambda r: ink[r, lo:hi].mean())
        bottom = best(range(max(0, y1 - 8), yb), lambda r: ink[r, lo:hi].mean())
        for cols in (left, right):
            for c in cols:
                for r in range(ya, yb):
                    if not (ink[r, max(0, min(cols) - CROSS)] and ink[r, min(W - 1, max(cols) + CROSS)]):
                        out[r, c] = 0
        for rows in (top, bottom):
            for r in rows:
                for c in range(lo, hi + 1):
                    if not (ink[max(0, min(rows) - CROSS), c] and ink[min(H - 1, max(rows) + CROSS), c]):
                        out[r, c] = 0
    return out


def wire_mask(crop: np.ndarray, dets: list[Detection], texts: list[Text], areas: list[Rect] = (),
              pins: list[Pin] = ()) -> np.ndarray:
    """Ink minus symbols minus text minus `areas` (connector brackets).

    OCR boxes often overlap the wire or arrow a label sits on, so inside a text
    box keep dots and any ink whose connected stroke continues outside the box
    or touches a symbol pin: a glyph does neither, a wire or arrow does (a short
    lead between two abutting parts can lie wholly inside a label's box)."""
    ink = (crop < INK).astype(np.uint8)
    variants = {t: dict(_variants(img)) for t, img in load_templates().items()}
    erase = np.zeros_like(ink)
    marks = np.zeros_like(ink)
    for d in dets:
        target = marks if d.type in MARKS else erase
        target[d.y:d.y + d.h, d.x:d.x + d.w] |= variants[d.stem][d.variant] < INK
    pad = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * ERASE_PAD + 1, 2 * ERASE_PAD + 1))
    gone = cv2.dilate(erase, pad) > 0
    if marks.any():  # a mark's ink goes, a straight wire running through it stays
        gone |= (cv2.dilate(marks, pad) > 0) & (_long_lines(ink, WIRE_MIN) == 0)
    ink[gone] = 0
    for x0, y0, x1, y1 in areas:
        ink[max(0, y0):y1, max(0, x0):x1] = 0
    dots = np.zeros_like(ink)
    for x, y, w, h in find_dots(ink):
        dots[y:y + h, x:x + w] = ink[y:y + h, x:x + w]
    # Strokes are judged on ink with all text boxes blanked except the one being
    # tested, so a glyph touching a neighbouring label does not count as leaving.
    boxes = [(slice(max(0, t.y), t.y + t.h), slice(max(0, t.x), t.x + t.w)) for t in texts]
    m = TEXT_MARGIN
    out = ink.copy()
    for t, box in zip(texts, boxes):
        y0, x0 = max(0, t.y - 2 * m), max(0, t.x - 2 * m)
        win = (slice(max(0, y0 - LEAVE), t.y + t.h + 2 * m + LEAVE), slice(max(0, x0 - LEAVE), t.x + t.w + 2 * m + LEAVE))
        local = ink[win].copy()
        for ob in boxes:
            if ob is not box:
                local[_shift(ob, win)] = 0
        local[_shift(box, win)] = ink[box]
        _, lab, stats, _ = cv2.connectedComponentsWithStats(local, connectivity=8)
        bx0, by0 = t.x - win[1].start, t.y - win[0].start
        inner = lab[_shift(box, win)]
        through = [i for i in np.unique(inner) if i and not (
            stats[i, 0] >= bx0 - m and stats[i, 1] >= by0 - m
            and stats[i, 0] + stats[i, 2] <= bx0 + t.w + m and stats[i, 1] + stats[i, 3] <= by0 + t.h + m)]
        for p in pins:
            px, py = int(p.x) - win[1].start, int(p.y) - win[0].start
            if 0 <= px < lab.shape[1] and 0 <= py < lab.shape[0]:
                r = PIN_TOL
                through += [i for i in np.unique(lab[max(0, py - r):py + r + 1, max(0, px - r):px + r + 1]) if i]
        out[box] = (np.isin(inner, through) & (ink[box] > 0)) | (dots[box] > 0)
    return out


def _shift(box: tuple[slice, slice], win: tuple[slice, slice]) -> tuple[slice, slice]:
    """Express a page-crop box in the coordinates of window `win` (clipped)."""
    (ys, xs), (wy, wx) = box, win
    return (slice(max(0, ys.start - wy.start), max(0, ys.stop - wy.start)),
            slice(max(0, xs.start - wx.start), max(0, xs.stop - wx.start)))


def _long_lines(ink: np.ndarray, length: int) -> np.ndarray:
    h = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1)))
    v = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, length)))
    return h | v


def find_dots(ink: np.ndarray) -> list[tuple[int, int, int, int]]:
    disk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (DOT_DISK, DOT_DISK))
    blobs = cv2.morphologyEx(ink, cv2.MORPH_OPEN, disk)
    n, _, stats, _ = cv2.connectedComponentsWithStats(blobs)
    lo, hi = DOT_SIZE
    return [tuple(int(v) for v in s[:4]) for s in stats[1:]
            if lo <= s[2] <= hi and lo <= s[3] <= hi and 0.6 <= s[2] / s[3] <= 1.6]


# Print dropouts break some lines (P401 -17.2 V rail and output wire under PM41: up to
# 14 px fully white in the line's core, leaving pieces 27-72 px apart once runs shorter
# than WIRE_MIN are dropped). Dashed outlines look alike up close (board outline: 25-35
# px dashes, 6-12 px gaps), so a gap is bridged only between two collinear pieces that
# are each longer than any dash (DROP_LONG), whose gap holds ink specks within the
# line's own thickness, is not crossed by a wire, and contains no detected symbol (so a
# part between two leads is never shorted, however small).
DROP_GAP = 80
DROP_LONG = 100  # px: dashes are <= 60 px
DROP_SPECKS = 4
DROP_TINY = 6  # px: collinear wires this close are one wire with a hairline break
                # (P701: 1-4 px print dropouts, some made by the 1/1.26 resample); dash gaps are >= 15
DROP_END = 8  # px at each end of a gap ignored when looking for specks (corner ink)
DROP_BAND = 1  # px either side of the line's own rows/columns searched for specks


def _dropout_bridges(ink: np.ndarray, segs: list[Segment], boxes: list[Rect] = ()) -> list[tuple[int, int]]:
    out = []
    for horizontal in (True, False):
        same = [i for i, s in enumerate(segs) if s.horizontal == horizontal]
        long_ = {i for i in same if (segs[i].x1 - segs[i].x0 if horizontal else segs[i].y1 - segs[i].y0) >= DROP_LONG}
        for i in same:
            a = segs[i]
            for j in same:
                b = segs[j]
                if horizontal:
                    gap, off = b.x0 - a.x1, abs((b.y0 + b.y1) - (a.y0 + a.y1)) / 2
                    lo, hi = min(a.y0, b.y0) - DROP_BAND, max(a.y1, b.y1) + DROP_BAND + 1
                    region = ink[max(0, lo):hi, a.x1 + 1:b.x0]
                    span = (a.x1 + 1, max(0, lo), b.x0, hi)
                else:
                    gap, off = b.y0 - a.y1, abs((b.x0 + b.x1) - (a.x0 + a.x1)) / 2
                    lo, hi = min(a.x0, b.x0) - DROP_BAND, max(a.x1, b.x1) + DROP_BAND + 1
                    region = ink[a.y1 + 1:b.y0, max(0, lo):hi]
                    span = (max(0, lo), a.y1 + 1, hi, b.y0)
                tiny = 0 < gap <= DROP_TINY  # a hairline break, far shorter than a dash gap
                # pieces of one wire share rows (columns); a thickened piece shifts the centre
                share = (min(a.y1, b.y1) - max(a.y0, b.y0)) if horizontal else (min(a.x1, b.x1) - max(a.x0, b.x0))
                if not (0 < gap <= DROP_GAP and (off <= 3 or tiny and share >= 2)):
                    continue
                if not tiny and not (i in long_ and j in long_):
                    continue
                # Specks must lie inside the gap, not at its ends: two collinear wires that
                # end in corners (P601 S601-4 above and below a pole) leave corner ink there.
                inner = region[:, DROP_END:-DROP_END] if horizontal else region[DROP_END:-DROP_END, :]
                if not tiny and (inner.size == 0 or np.count_nonzero(inner) < DROP_SPECKS):
                    continue
                # A wire crossing the gap inks the line's full thickness somewhere.
                across = region.all(axis=0) if horizontal else region.all(axis=1)
                if across.any():
                    continue
                if any(x0 < span[2] and span[0] < x1 and y0 < span[3] and span[1] < y1 for x0, y0, x1, y1 in boxes):
                    continue
                out.append((i, j))
    return out


def find_segments(ink: np.ndarray) -> list[Segment]:
    segs = []
    for horizontal, ksize in ((True, (WIRE_MIN, 1)), (False, (1, WIRE_MIN))):
        lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, ksize))
        n, _, stats, _ = cv2.connectedComponentsWithStats(lines)
        for x, y, w, h, _a in stats[1:]:
            segs.append(Segment(int(x), int(y), int(x + w - 1), int(y + h - 1), horizontal))
    return segs


def find_arrows(ink: np.ndarray, dots: list[tuple[int, int, int, int]]) -> list[tuple[tuple[float, float], tuple[float, float], np.ndarray]]:
    """Diagonal pointer arrows (voltage label -> wire): ink left after removing wires and dots.

    Wires are removed with a longer kernel than in `find_segments`, because a
    shallow arrow's head has horizontal runs longer than WIRE_MIN. Each thin
    diagonal shaft then absorbs the leftover pieces just ahead of its ends (its
    head, or its far part when the arrow is cut by a wire it crosses). The head
    is the end with clearly more ink. Returns (tip, tail, pixels) per arrow."""
    lines = cv2.dilate(_long_lines(ink, ARROW_WIRE), cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))
    rest = ink & (1 - lines)
    for x, y, w, h in dots:
        rest[max(0, y - 4):y + h + 4, max(0, x - 4):x + w + 4] = 0
    n, lab, stats, _ = cv2.connectedComponentsWithStats(rest)
    pieces: list[np.ndarray] = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 20:
            continue
        ys, xs = np.nonzero(lab[y:y + h, x:x + w] == i)
        pieces.append(np.stack([xs + x, ys + y], 1).astype(float))
    shafts = [k for k, p in enumerate(pieces) if max(np.ptp(p, 0)) >= ARROW_PIECE and _stroke(p) is not None]
    used = set(shafts) | {k for k, p in enumerate(pieces) if _is_hv_line(p)}  # wire stubs are never arrow parts
    arrows = []
    for k in shafts:
        pts, d, ends = _stroke(pieces[k])
        for _ in range(2):  # absorb up to two pieces ahead of each end
            c = pts.mean(0)
            for e in ends:
                u = (e - c) / max(np.hypot(*(e - c)), 1e-9)
                for j, q in enumerate(pieces):
                    if j in used:
                        continue
                    v = q - e
                    dist = np.hypot(*v.T)
                    near = dist <= ARROW_GAP
                    if near.any() and (v[near] @ u > 0).all():
                        pts = np.vstack([pts, q])
                        used.add(j)
            t = (pts - pts.mean(0)) @ d
            ends = (pts[t.argmin()], pts[t.argmax()])
        a, b = ends
        if np.hypot(*(a - b)) < ARROW_MIN:
            continue
        mass = [int((np.hypot(*(pts - e).T) < 12).sum()) for e in (a, b)]
        if max(mass) < ARROW_HEAD * min(mass):
            continue  # no distinct head
        tip, tail = (a, b) if mass[0] > mass[1] else (b, a)
        arrows.append(((float(tip[0]), float(tip[1])), (float(tail[0]), float(tail[1])), pts.astype(int)))
    return arrows


def _is_hv_line(pts: np.ndarray) -> bool:
    c = pts.mean(0)
    sv, axes = np.linalg.svd(pts - c, full_matrices=False)[1:]
    angle = np.degrees(np.arctan2(abs(axes[0][1]), abs(axes[0][0])))
    return sv[1] <= ARROW_THIN * sv[0] and not HV_ANGLE <= angle <= 90 - HV_ANGLE


def _stroke(pts: np.ndarray):
    """(points, unit direction, (end, end)) if `pts` form a thin diagonal stroke, else None."""
    c = pts.mean(0)
    sv, axes = np.linalg.svd(pts - c, full_matrices=False)[1:]
    if sv[1] > ARROW_THIN * sv[0]:
        return None  # not thin (leftover glyph)
    d = axes[0]
    angle = np.degrees(np.arctan2(abs(d[1]), abs(d[0])))
    if not ARROW_ANGLE <= angle <= 90 - ARROW_ANGLE:
        return None  # horizontal or vertical: wire stub or glyph stroke, not an arrow
    t = (pts - c) @ d
    return pts, d, (pts[t.argmin()], pts[t.argmax()])


# --- connectivity ---------------------------------------------------------

class _DSU:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        self.p[self.find(a)] = self.find(b)


def _meets(h: Segment, v: Segment) -> tuple[bool, bool]:
    """(touch, at_end): do H and V overlap, and is the overlap at an end of either?"""
    if not (v.x0 - END_TOL <= h.x1 and h.x0 <= v.x1 + END_TOL
            and h.y0 - END_TOL <= v.y1 and v.y0 <= h.y1 + END_TOL):
        return False, False
    h_end = v.x0 - h.x0 <= END_TOL or h.x1 - v.x1 <= END_TOL
    v_end = h.y0 - v.y0 <= END_TOL or v.y1 - h.y1 <= END_TOL
    return True, h_end or v_end


MEET_GAP = 3  # px of clear paper allowed where two wires meet (anti-aliasing, resample)


def _inked_meeting(raw: np.ndarray, h: Segment, v: Segment) -> bool:
    """`_meets` allows END_TOL of slack; a stroke that stops short of a wire with clear
    paper between is not wired to it (P851: the "1" of "R851 1" stands between the
    -57 V line and the AC line, ~8 px from each). Any gap wider than MEET_GAP must be
    inked across."""
    cx = (v.x0 + v.x1) // 2
    cy = (h.y0 + h.y1) // 2
    gy = (h.y0 - v.y1) if v.y1 < h.y0 else (v.y0 - h.y1) if v.y0 > h.y1 else 0
    gx = (v.x0 - h.x1) if h.x1 < v.x0 else (h.x0 - v.x1) if h.x0 > v.x1 else 0
    if gy > MEET_GAP:
        ys = slice(v.y1 + 1, h.y0) if v.y1 < h.y0 else slice(h.y1 + 1, v.y0)
        if not raw[ys, max(0, cx - 2):cx + 3].any(axis=1).all():
            return False
    if gx > MEET_GAP:
        xs = slice(h.x1 + 1, v.x0) if h.x1 < v.x0 else slice(v.x1 + 1, h.x0)
        if not raw[max(0, cy - 2):cy + 3, xs].any(axis=0).all():
            return False
    return True


CROSS_PROBE = (4, 14)  # px beyond a wire's edge where a crossing stroke still shows


def _hidden_crossing(raw: np.ndarray, h: Segment, v: Segment) -> bool:
    """A wire that ends on another one is a T, unless its stroke carries on across it in
    the scan. That part can be missing from the wire mask when it is a symbol's lead
    (P701 R781: the bottom lead crosses the output wire, no dot). Junction dots are
    joined separately, so a dotted crossing still connects."""
    a, b = CROSS_PROBE
    H, W = raw.shape
    def inked(ys: slice, xs: slice) -> bool:
        """Every step along the probe (rows if it is tall, columns if wide) has ink."""
        part = raw[ys, xs]
        if part.size == 0:
            return False
        tall = part.shape[0] >= part.shape[1]
        return bool(part.any(axis=1 if tall else 0).all())
    cx = (v.x0 + v.x1) // 2
    cy = (h.y0 + h.y1) // 2
    # Only a wire that passes straight through can be crossed; where both end (a corner,
    # or a T whose bar is a symbol lead: P701 R704 on C714's lead) they are joined.
    m = CROSS_PROBE[1]  # a lead overshooting a corner by a stroke width does not pass through
    h_through = h.x0 < v.x0 - m and h.x1 > v.x1 + m
    v_through = v.y0 < h.y0 - m and v.y1 > h.y1 + m
    if not (h_through or v_through):
        return False
    if h_through and v.y0 >= h.y0 - END_TOL and v.y0 <= h.y1 + END_TOL:  # v ends on h from below
        if inked(slice(max(0, h.y0 - b), max(0, h.y0 - a)), slice(max(0, cx - 2), cx + 3)):
            return True
    if h_through and v.y1 <= h.y1 + END_TOL and v.y1 >= h.y0 - END_TOL:  # v ends on h from above
        if inked(slice(min(H, h.y1 + a), min(H, h.y1 + b)), slice(max(0, cx - 2), cx + 3)):
            return True
    if v_through and h.x0 >= v.x0 - END_TOL and h.x0 <= v.x1 + END_TOL:  # h ends on v from the right
        if inked(slice(max(0, cy - 2), cy + 3), slice(max(0, v.x0 - b), max(0, v.x0 - a))):
            return True
    if v_through and h.x1 <= v.x1 + END_TOL and h.x1 >= v.x0 - END_TOL:  # h ends on v from the left
        if inked(slice(max(0, cy - 2), cy + 3), slice(min(W, v.x1 + a), min(W, v.x1 + b))):
            return True
    return False


PIN_JOIN = 30  # px: two pins this close on one straight ink stroke are wired together


def _touching_pins(raw: np.ndarray, pins: list[Pin]) -> list[tuple[int, int]]:
    """Parts drawn lead to lead (P701 Q731 emitter on R781, ~17 px apart) leave no wire
    segment between them: join pins of different parts that face each other within
    PIN_JOIN along an unbroken horizontal or vertical stroke."""
    out = []
    for a in range(len(pins)):
        p = pins[a]
        for b in range(a + 1, len(pins)):
            q = pins[b]
            if p.owner == q.owner:
                continue
            dx, dy = abs(p.x - q.x), abs(p.y - q.y)
            if max(dx, dy) > PIN_JOIN or min(dx, dy) > 4:
                continue
            if dy >= dx:
                x = int(round((p.x + q.x) / 2))
                band = raw[int(min(p.y, q.y)):int(max(p.y, q.y)) + 1, max(0, x - 2):x + 3]
                ok = band.size and band.any(axis=1).all()
            else:
                y = int(round((p.y + q.y) / 2))
                band = raw[max(0, y - 2):y + 3, int(min(p.x, q.x)):int(max(p.x, q.x)) + 1]
                ok = band.size and band.any(axis=0).all()
            if ok:
                out.append((a, b))
    return out


STUB_REACH = 40  # px: a pin may reach a wire along its own ink stroke this far


def _follow_stubs(ink: np.ndarray, pins: list[Pin], segs: list[Segment], hits: dict,
                  reach: dict[int, int] | None = None) -> None:
    """A lead shorter than WIRE_MIN (R404's 15 px before it turns down) yields no
    segment, leaving its pin facing a gap. Follow the pin's own ink stroke, within
    STUB_REACH, to the nearest segment on that stroke."""
    H, W = ink.shape
    for p in pins:
        if hits[id(p)]:
            continue
        x0, y0 = max(0, int(p.x) - STUB_REACH), max(0, int(p.y) - STUB_REACH)
        win = (ink[y0:int(p.y) + STUB_REACH + 1, x0:int(p.x) + STUB_REACH + 1] > 0).astype(np.uint8)
        _, lab = cv2.connectedComponents(win, connectivity=8)
        px, py = int(p.x) - x0, int(p.y) - y0
        r = (reach or {}).get(id(p), PIN_TOL)  # a terminal circle's lead starts at its rim
        near = lab[max(0, py - r):py + r + 1, max(0, px - r):px + r + 1]
        ids = set(near[near > 0].tolist())
        if not ids:
            continue
        best = None
        for i, sg in enumerate(segs):
            xs = slice(max(0, sg.x0 - x0), max(0, min(win.shape[1], sg.x1 + 1 - x0)))
            ys = slice(max(0, sg.y0 - y0), max(0, min(win.shape[0], sg.y1 + 1 - y0)))
            part = lab[ys, xs]
            if part.size and ids & set(np.unique(part).tolist()):
                d = np.hypot(max(sg.x0 - p.x, 0, p.x - sg.x1), max(sg.y0 - p.y, 0, p.y - sg.y1))
                if best is None or d < best[0]:
                    best = (d, i)
        if best:
            hits[id(p)] = [best[1]]


BACK_REACH = 8  # px: wire end to the back wall of a connector pin's bracket


def _through_headers(pins: list[Pin], dets: list[Detection], segs: list[Segment], hits: dict, lib: dict) -> None:
    """A board header drawn with wires on both sides (P601 J603: the board above, the
    W009 cable to P651 below) passes each pin straight through: a wire running along
    the lead's axis that ends at the back wall of the bracket, opposite the lead, is
    the same conductor. Mated pairs (J504 | JM11) have no wire there."""
    templates = load_templates()
    for p in pins:
        d = dets[p.owner]
        back = lib[d.type].get("back")
        if not back:
            continue
        th, tw = templates[d.stem].shape
        bx, by = _transform(back[0], back[1], tw, th, d.variant)
        bx, by = d.x + bx, d.y + by
        along_x = abs(p.x - bx) > abs(p.y - by)  # the lead runs horizontally
        for i, s in enumerate(segs):
            if s.horizontal == along_x and i not in hits[id(p)] and min(_end_dist(s, bx, by)) <= BACK_REACH:
                hits[id(p)].append(i)


# An op-amp drawn with + on top (P601 Q601-1) matches the unmirrored template almost
# as well as the mirrored one: the signs are too small to sway the score. So read
# them. Each sign sits inside the triangle on its input's side, but not at a fixed
# spot (P601 draws them lower than P401), so find the plus-like glyph in the left part
# of the interior (a "+" spans ~16 px both ways, a "-" ~6 px in one) and see which
# input pin it is nearer.
OPAMP_INTERIOR = (30, 30, 110, 176)  # x0, y0, x1, y1 (template): left part of the triangle


def _sign_glyphs(crop: np.ndarray, corners: list[tuple[float, float]]) -> list[tuple[float, float, int]]:
    """Small ink blobs inside the box spanned by `corners`: (centre x, centre y, min side).
    Blobs touching the box border (triangle edges, circles, leads) are ignored."""
    xs, ys = [int(c[0]) for c in corners], [int(c[1]) for c in corners]
    x0, y0, x1, y1 = max(0, min(xs)), max(0, min(ys)), max(xs), max(ys)
    box = (crop[y0:y1, x0:x1] < INK).astype(np.uint8)
    n, _, st, cen = cv2.connectedComponentsWithStats(box, 8)
    out = []
    for (x, y, w, h, a), (cx, cy) in zip(st[1:], cen[1:]):
        if x == 0 or y == 0 or x + w >= box.shape[1] or y + h >= box.shape[0] or max(w, h) > 30 or a < 15:
            continue
        out.append((x0 + cx, y0 + cy, int(min(w, h))))
    return out


def fix_opamp_polarity(crop: np.ndarray, dets: list[Detection], pins: list[Pin]) -> set[int]:
    """Swap "+"/"-" pin names of op-amps whose drawn signs say the match is mirrored:
    the plus-like glyph (min side >= 10 px) lies on the "-" input's side of the axis.
    Returns the indices of the swapped detections."""
    th, tw = load_templates()["opamp"].shape
    x0, y0, x1, y1 = OPAMP_INTERIOR
    swapped = set()
    for i, d in enumerate(dets):
        if d.type != "opamp":
            continue
        corners = [_transform(x, y, tw, th, d.variant) for x in (x0, x1) for y in (y0, y1)]
        glyphs = _sign_glyphs(crop, [(d.x + cx, d.y + cy) for cx, cy in corners])
        plus = [g for g in glyphs if g[2] >= 10]
        if len(plus) != 1:
            continue  # unreadable: keep the template's orientation
        pin = {p.name: p for p in pins if p.owner == i and p.name in ("+", "-")}
        gx, gy, _ = plus[0]
        d_minus = (gx - pin["-"].x) ** 2 + (gy - pin["-"].y) ** 2
        d_plus = (gx - pin["+"].x) ** 2 + (gy - pin["+"].y) ** 2
        if d_minus < d_plus:  # the plus glyph is on the "-" input's side: mirrored
            pin["+"].name, pin["-"].name = "-", "+"
            swapped.add(i)
    return swapped


def _end_dist(s: Segment, x: float, y: float) -> tuple[float, float]:
    """Distances from (x, y) to the two ends of a segment (ends on its centre line)."""
    if s.horizontal:
        cy = (s.y0 + s.y1) / 2
        return (np.hypot(s.x0 - x, cy - y), np.hypot(s.x1 - x, cy - y))
    cx = (s.x0 + s.x1) / 2
    return (np.hypot(cx - x, s.y0 - y), np.hypot(cx - x, s.y1 - y))


CROSS_THROUGH = 15  # px: a lead running on this far past a perpendicular wire crosses it


def _drop_crossings(p: Pin, det: Detection, hits: list[int], segs: list[Segment]) -> list[int]:
    """Drop a wire that merely crosses a pin's lead. A lead that continues straight on
    past a perpendicular wire, with no junction dot, crosses it (P701 R781: the bottom
    lead crosses the output wire). A lead that stops on the wire is a T and keeps it."""
    cx, cy = det.center
    vertical = abs(p.y - cy) > abs(p.x - cx)  # lead leaves the symbol up/down
    out = 1 if (p.y > cy if vertical else p.x > cx) else -1
    along = (lambda s: (s.y0, s.y1)) if vertical else (lambda s: (s.x0, s.x1))
    pos = p.y if vertical else p.x
    lead = [h for h in hits if segs[h].horizontal != vertical]
    keep = []
    for h in hits:
        s = segs[h]
        if s.horizontal == vertical:  # perpendicular to the lead
            mid = (s.y0 + s.y1) / 2 if vertical else (s.x0 + s.x1) / 2
            through = min(_end_dist(s, p.x, p.y)) > END_TOL
            beyond = any(out * (along(segs[k])[1 if out > 0 else 0] - mid) > CROSS_THROUGH for k in lead)
            if through and beyond:
                continue
        keep.append(h)
    return keep or hits


def trace(crop: np.ndarray, dets: list[Detection], texts: list[Text]) -> TraceResult:
    pins = symbol_pins(dets)
    fix_opamp_polarity(crop, dets, pins)
    connectors, brackets = group_connectors(dets, pins)
    ink = erase_jack_boxes(wire_mask(crop, dets, texts, brackets, pins), dets)
    dots = find_dots(ink)
    arrows = find_arrows(ink, dots)
    for _tip, _tail, pts in arrows:
        ink[pts[:, 1], pts[:, 0]] = 0  # a shallow arrowhead would otherwise pass for a wire stub
    segs = find_segments(ink)

    n_seg = len(segs)
    dsu = _DSU(n_seg + len(dots) + len(pins))
    raw = crop < INK
    hs = [i for i, s in enumerate(segs) if s.horizontal]
    vs = [i for i, s in enumerate(segs) if not s.horizontal]
    for i in hs:
        for j in vs:
            touch, at_end = _meets(segs[i], segs[j])
            if touch and at_end and _inked_meeting(raw, segs[i], segs[j]) \
                    and not _hidden_crossing(raw, segs[i], segs[j]):
                dsu.union(i, j)
    sym_boxes = [(d.x - 3, d.y - 3, d.x + d.w + 3, d.y + d.h + 3) for d in dets]
    for i, j in _dropout_bridges(ink, segs, sym_boxes):
        dsu.union(i, j)
    for k, (x, y, w, h) in enumerate(dots):
        cx, cy = x + w / 2, y + h / 2
        for i, s in enumerate(segs):
            if s.near(cx, cy, w / 2):
                dsu.union(i, n_seg + k)

    lib = load_library()

    def attached(p: Pin) -> list[int]:
        reach = lib[dets[p.owner].type].get("pin_reach")
        if reach:
            # A terminal circle (pin at its centre) owns the wires that END at it, from
            # any side; a wire passing beside the circle is not its lead (P601 S601).
            hits = [i for i, s in enumerate(segs) if min(_end_dist(s, p.x, p.y)) <= reach]
        else:
            hits = [i for i, s in enumerate(segs) if s.near(p.x, p.y, PIN_TOL)]
        tol = reach or PIN_TOL
        on_dots = [n_seg + k for k, (x, y, w, h) in enumerate(dots)
                   if x - tol <= p.x <= x + w + tol and y - tol <= p.y <= y + h + tol]
        if not reach and not on_dots:
            hits = _drop_crossings(p, dets[p.owner], hits, segs)
        return hits + on_dots

    hits = {id(p): attached(p) for p in pins}
    _through_headers(pins, dets, segs, hits, lib)
    _follow_stubs(ink, pins, segs, hits,
                  {id(p): lib[dets[p.owner].type]["rim"] for p in pins if "rim" in lib[dets[p.owner].type]})
    base = n_seg + len(dots)
    for k, p in enumerate(pins):
        for h in hits[id(p)]:
            dsu.union(base + k, h)  # a pin touching two wires joins them (pin at a corner)
    joined = set()
    for a, b in _touching_pins(raw, pins):
        dsu.union(base + a, base + b)
        joined |= {a, b}
    mated = mate_connectors(dets, pins, connectors)
    lead_hits = {p.owner: hits[id(p)] for p in pins if dets[p.owner].type == "connector_pin"}
    for a, b in mated:
        for i, j in zip(connectors[a], connectors[b]):
            if lead_hits[i] and lead_hits[j]:
                dsu.union(lead_hits[i][0], lead_hits[j][0])
    net_of_pin = {(p.owner, p.name): dsu.find(base + k) for k, p in enumerate(pins) if hits[id(p)] or k in joined}
    floating = [p for k, p in enumerate(pins) if not hits[id(p)] and k not in joined
                and p.name not in lib[dets[p.owner].type].get("optional_pins", ())
                and p.name not in lib[dets[p.owner].type].get("may_float", ())]
    nets: dict[int, list[int]] = {}
    for i in range(n_seg):
        nets.setdefault(dsu.find(i), []).append(i)
    return TraceResult(segs, dots, pins, net_of_pin, floating, nets, connectors, [(tip, tail) for tip, tail, _ in arrows], mated)


# --- IR -------------------------------------------------------------------

VOLT_DIST = 120  # px: printed voltage to wire attach distance
ABOVE_PENALTY = 1.5  # distance factor for a wire above a horizontal label
ARROW_REACH = 90  # px: arrow tail to its label
ARROW_HIT = 40  # px: arrow tip to the wire or pin it points at


def _seg_dist(t: Text, s: Segment) -> float:
    tx, ty = t.center
    dx = max(s.x0 - tx, 0, tx - s.x1)
    dy = max(s.y0 - ty, 0, ty - s.y1)
    return (dx * dx + dy * dy) ** 0.5


def _assign_arrows(volts: list[Text], arrows) -> dict[int, tuple]:
    """Each arrow belongs to the voltage label nearest its tail (within ARROW_REACH)."""
    out: dict[int, tuple] = {}
    best: dict[int, float] = {}
    for tip, tail in arrows:
        cands = [(_box_dist((t.x, t.y, t.w, t.h), tail), k) for k, t in enumerate(volts)]
        if not cands:
            continue
        d, k = min(cands)
        if d <= ARROW_REACH and d < best.get(k, 1e9):
            out[k], best[k] = (tip, tail), d
    return out


def _box_dist(box: tuple[float, float, float, float], pt: tuple[float, float]) -> float:
    x, y, w, h = box
    dx = max(x - pt[0], 0, pt[0] - (x + w))
    dy = max(y - pt[1], 0, pt[1] - (y + h))
    return (dx * dx + dy * dy) ** 0.5


def _arrow_target(arrow, res: TraceResult, seg_net: dict[int, int]) -> tuple[int | None, float]:
    """Net under an arrow tip: nearest wire segment or symbol pin."""
    (tx, ty), _ = arrow
    cands = [(_box_dist((s.x0, s.y0, s.x1 - s.x0 + 1, s.y1 - s.y0 + 1), (tx, ty)), seg_net[i])
             for i, s in enumerate(res.segments)]
    cands += [(float(np.hypot(p.x - tx, p.y - ty)), res.net_of_pin[(p.owner, p.name)])
              for p in res.pins if (p.owner, p.name) in res.net_of_pin]
    d, net = min(cands, default=(None, None))
    return (net, d) if d is not None and d <= ARROW_HIT else (None, 0.0)


def _label_target(t: Text, res: TraceResult, seg_net: dict[int, int]) -> tuple[int | None, float]:
    """Net of the wire nearest a label. Horizontal labels are drafted above their wire,
    so wires above the label count as farther."""
    best = (None, 1e9)
    cy = t.center[1]
    for i, s in enumerate(res.segments):
        d = _seg_dist(t, s)
        if not t.vertical and s.y1 < cy:
            d *= ABOVE_PENALTY
        if d < best[1]:
            best = (seg_net[i], d)
    return best if best[1] <= VOLT_DIST else (None, 0.0)


JACK_GAP = 220  # px: sockets of one jack block are stacked closer than this


def _jack_blocks(dets: list[Detection]) -> list[list[int]]:
    """RCA sockets stacked in one column (a 2P jack block like J401), top first."""
    jacks = sorted((i for i, d in enumerate(dets) if d.type == "rca_jack"), key=lambda i: (dets[i].x // 40, dets[i].y))
    blocks: list[list[int]] = []
    for i in jacks:
        last = dets[blocks[-1][-1]] if blocks else None
        if last and abs(dets[i].x - last.x) < 40 and dets[i].y - last.y < JACK_GAP and len(blocks[-1]) < 2:
            blocks[-1].append(i)
        else:
            blocks.append([i])
    return blocks


def to_ir(dets: list[Detection], texts: list[Text], res: TraceResult,
          offset: tuple[int, int] = (0, 0), meta: dict | None = None, scale: float = 1.0) -> IR:
    """Assemble an IR from detections, OCR texts and traced nets (crop coords -> page coords).

    Refs/values come from `ocr.associate`; unlabelled parts keep "?<i>". Nets are
    named "gnd" if a ground symbol touches them, else "n<k>". A pin touching no wire
    gets its own "nc<k>" net so ERC reports it.
    """
    ox, oy = offset

    def page_box(x, y, w, h):
        """Crop box -> page box. `scale` > 1 when the crop was shrunk to page-4 drawing scale."""
        return (round(x * scale) + ox, round(y * scale) + oy, round(w * scale), round(h * scale))
    gnd = {res.net_of_pin[(i, "G")] for i, d in enumerate(dets)
           if d.type == "ground" and (i, "G") in res.net_of_pin}
    names: dict[int, str] = {n: "gnd" for n in gnd}
    unconnected = iter(range(1, 10**6))

    def name(net: int) -> str:
        if net not in names:
            names[net] = f"n{sum(v != 'gnd' for v in names.values()) + 1}"
        return names[net]

    def pin_net(i: int, pin: str) -> str:
        if (i, pin) in res.net_of_pin:
            return name(res.net_of_pin[(i, pin)])
        return f"nc{next(unconnected)}"

    parts = [i for i, d in enumerate(dets) if d.type not in NON_COMPONENTS]
    jack_blocks = _jack_blocks(dets)
    stacks = res.connectors + jack_blocks
    boxes = [(dets[i].type, (dets[i].x, dets[i].y, dets[i].w, dets[i].h)) for i in parts]
    for st in stacks:
        ds = [dets[i] for i in st]
        x0, y0 = min(d.x for d in ds), min(d.y for d in ds)
        boxes.append(("connector", (x0, y0, max(d.x + d.w for d in ds) - x0, max(d.y + d.h for d in ds) - y0)))
    labels = associate(boxes, texts)

    lib = load_library()
    components = []
    for k, i in enumerate(parts):
        d, (ref_t, val_t) = dets[i], labels[k]
        conf = d.score * (ref_t.score if ref_t else 0.5)
        components.append(Component(
            ref=ref_t.norm if ref_t else f"?{i + 1}",
            type=d.type,
            value=val_t.norm if val_t else "",
            pins={p: pin_net(i, p) for p in lib[d.type]["pins"]
                  if (i, p) in res.net_of_pin or p not in lib[d.type].get("optional_pins", ())},
            bbox=page_box(d.x, d.y, d.w, d.h),
            confidence=round(conf, 3),
            note=f"{DETECTOR} {d.variant}",
        ))
    # An optional pin (an op-amp section's undrawn supply) that met only a stray stub
    # sits alone on its net: it is not drawn, drop it.
    count: dict[str, int] = {}
    for c in components:
        for n in c.pins.values():
            count[n] = count.get(n, 0) + 1
    for st in stacks:
        for i in st:
            for p in ("P", "T", "S"):
                if (i, p) in res.net_of_pin:
                    count[name(res.net_of_pin[(i, p)])] = count.get(name(res.net_of_pin[(i, p)]), 0) + 1
    for k, c in enumerate(components):
        opt = lib[c.type].get("optional_pins", ())
        lone = {p for p, n in c.pins.items() if p in opt and count.get(n, 0) == 1}
        if lone:
            components[k] = c.model_copy(update={"pins": {p: n for p, n in c.pins.items() if p not in lone}})
    connectors = []
    for k, st in enumerate(stacks):
        ref_t = labels[len(parts) + k][0]
        x, y, w, h = boxes[len(parts) + k][1]
        connectors.append(Connector(
            ref=ref_t.norm if ref_t else f"J?{k + 1}",
            pins=({f"{p}{n}": pin_net(i, p) for n, i in enumerate(st, 1) for p in ("T", "S")}
                  if dets[st[0]].type == "rca_jack" else {str(n): pin_net(i, "P") for n, i in enumerate(st, 1)}),
            bbox=page_box(x, y, w, h),
            confidence=round(min(dets[i].score for i in st) * (ref_t.score if ref_t else 0.5), 3),
            note=("RCA sockets numbered from the top: T = tip, S = shield"
                  if dets[st[0]].type == "rca_jack" else "pins numbered from the top/left"),
        ))
    annotations = []
    volts = [t for t in texts if t.kind == "voltage"]
    arrow_of = _assign_arrows(volts, res.arrows)
    seg_net = {si: n for n, segs in res.nets.items() for si in segs}
    for k, t in enumerate(volts):
        if k in arrow_of:
            net, d = _arrow_target(arrow_of[k], res, seg_net)
            how = "arrow"
        else:
            net, d = _label_target(t, res, seg_net)
            how = "nearest wire"
        if net is None:
            continue
        annotations.append(Annotation(
            net=name(net), voltage=float(t.norm), bbox=page_box(t.x, t.y, t.w, t.h),
            confidence=round(t.score * (1 - d / (2 * VOLT_DIST)), 3), note=how,
        ))
    out = IR(meta={**(meta or {}), "tracer": TRACER, "detector": DETECTOR,
                   "ocr": ENGINE, "ocr_version": PROMPT_VERSION},
             components=components, connectors=connectors, annotations=annotations)
    return derive_nets(out)
