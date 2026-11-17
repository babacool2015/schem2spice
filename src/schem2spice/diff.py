"""Compare an extracted IR (or detections) against a golden IR."""

from __future__ import annotations

from dataclasses import dataclass, field

from .ir import Component


@dataclass
class SymbolDiff:
    matched: list[tuple[Component, Component]] = field(default_factory=list)  # (golden, found), same type
    wrong_type: list[tuple[Component, Component]] = field(default_factory=list)
    missed: list[Component] = field(default_factory=list)
    extra: list[Component] = field(default_factory=list)

    @property
    def recall(self) -> float:
        n = len(self.matched) + len(self.wrong_type) + len(self.missed)
        return len(self.matched) / n if n else 1.0

    @property
    def precision(self) -> float:
        n = len(self.matched) + len(self.wrong_type) + len(self.extra)
        return len(self.matched) / n if n else 1.0

    def report(self) -> str:
        lines = [f"recall {self.recall:.0%}  precision {self.precision:.0%}  "
                 f"({len(self.matched)} ok, {len(self.wrong_type)} wrong type, "
                 f"{len(self.missed)} missed, {len(self.extra)} extra)"]
        lines += [f"  wrong type: {g.ref} is {g.type}, found {f.type} ({f.confidence})" for g, f in self.wrong_type]
        lines += [f"  missed: {g.ref} ({g.type}) at {g.bbox}" for g in self.missed]
        lines += [f"  extra: {f.type} at {f.bbox} ({f.confidence})" for f in self.extra]
        return "\n".join(lines)


def _center(c: Component) -> tuple[float, float]:
    x, y, w, h = c.bbox
    return x + w / 2, y + h / 2


def diff_symbols(golden: list[Component], found: list[Component], max_dist: float = 40) -> SymbolDiff:
    """Pair golden and found symbols by bbox center (closest first), within `max_dist` px."""
    pairs = []
    for gi, g in enumerate(golden):
        gx, gy = _center(g)
        for fi, f in enumerate(found):
            fx, fy = _center(f)
            d = ((gx - fx) ** 2 + (gy - fy) ** 2) ** 0.5
            if d <= max_dist:
                pairs.append((d, gi, fi))
    out, used_g, used_f = SymbolDiff(), set(), set()
    for _, gi, fi in sorted(pairs):
        if gi in used_g or fi in used_f:
            continue
        used_g.add(gi)
        used_f.add(fi)
        g, f = golden[gi], found[fi]
        (out.matched if g.type == f.type else out.wrong_type).append((g, f))
    out.missed = [g for i, g in enumerate(golden) if i not in used_g]
    out.extra = [f for i, f in enumerate(found) if i not in used_f]
    return out


@dataclass
class TextDiff:
    refs_found: list[str] = field(default_factory=list)
    refs_missed: list[str] = field(default_factory=list)
    volts_found: list[str] = field(default_factory=list)
    volts_missed: list[str] = field(default_factory=list)

    def report(self) -> str:
        nr = len(self.refs_found) + len(self.refs_missed)
        nv = len(self.volts_found) + len(self.volts_missed)
        return (f"refs {len(self.refs_found)}/{nr}  missed: {', '.join(self.refs_missed) or '-'}\n"
                f"voltages {len(self.volts_found)}/{nv}  missed: {', '.join(self.volts_missed) or '-'}")


def diff_text(golden, texts, region: tuple[int, int, int, int], offset=(0, 0), max_dist: float = 150) -> TextDiff:
    """Golden refs/printed voltages inside `region` (x0, y0, x1, y1, page px) vs OCR texts near them."""
    x0, y0, x1, y1 = region
    ox, oy = offset

    def inside(b):
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        return x0 <= cx < x1 and y0 <= cy < y1

    def near(b, kind, norm):
        # distance from the text center to the nearest edge of the bbox (labels sit beside symbols)
        def dist(t):
            tx, ty = t.center[0] + ox, t.center[1] + oy
            dx = max(b[0] - tx, 0, tx - (b[0] + b[2]))
            dy = max(b[1] - ty, 0, ty - (b[1] + b[3]))
            return (dx * dx + dy * dy) ** 0.5
        return any(t.kind == kind and t.norm == norm and dist(t) <= max_dist for t in texts)

    out = TextDiff()
    items = [(c.ref, c.bbox) for c in golden.components] + [(j.ref, j.bbox) for j in golden.connectors]
    for ref, bbox in items:
        if bbox and inside(bbox):
            (out.refs_found if near(bbox, "ref", ref) else out.refs_missed).append(ref)
    for a in golden.annotations:
        if a.bbox and inside(a.bbox):
            norm = f"{a.voltage:.2f}".rstrip("0").rstrip(".") if a.voltage % 1 else f"{a.voltage:.1f}"
            label = f"{a.voltage:g}V ({a.net})"
            (out.volts_found if near(a.bbox, "voltage", norm) else out.volts_missed).append(label)
    return out


@dataclass
class NetDiff:
    """Connectivity of the extracted IR vs golden, over the parts both contain.

    A pin is correct when the set of other (shared) pins on its net is the same in
    both IRs, so net names never matter. Swappable pins (R/C ends, JFET D/S) may be
    in either order.
    """
    pins_ok: list[str] = field(default_factory=list)
    pins_bad: list[tuple[str, list[str], list[str]]] = field(default_factory=list)  # pin, golden mates, found mates
    unmatched: list[str] = field(default_factory=list)  # golden parts/connectors with no extracted counterpart
    volts_ok: list[str] = field(default_factory=list)
    volts_bad: list[str] = field(default_factory=list)

    def report(self) -> str:
        n = len(self.pins_ok) + len(self.pins_bad)
        nv = len(self.volts_ok) + len(self.volts_bad)
        lines = [f"pins {len(self.pins_ok)}/{n} on the right net; voltages {len(self.volts_ok)}/{nv} on the right net"]
        lines += [f"  {p}: golden with {g or '-'}, found with {f or '-'}" for p, g, f in self.pins_bad]
        lines += [f"  voltage {v}" for v in self.volts_bad]
        if self.unmatched:
            lines.append(f"  not extracted: {', '.join(self.unmatched)}")
        return "\n".join(lines)


def _pin_nets(ir) -> dict[str, str]:
    out = {f"{c.ref}.{p}": n for c in ir.components for p, n in c.pins.items()}
    out |= {f"{j.ref}.{p}": n for j in ir.connectors for p, n in j.pins.items()}
    return out


def diff_nets(golden, found, region: tuple[int, int, int, int] | None = None,
              max_dist: float = 40, swappable: dict[str, list[str]] | None = None) -> NetDiff:
    """Compare connectivity. Parts are paired by position (as in `diff_symbols`), not by
    OCR'd ref, so trace is judged independently of text association. Only golden parts
    whose bbox center lies in `region` (page x0, y0, x1, y1) are scored."""
    if swappable is None:
        from .netlist import load_library
        swappable = {t: v["swappable"] for t, v in load_library().items() if "swappable" in v}

    def inside(b):
        if region is None:
            return True
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        return region[0] <= cx < region[2] and region[1] <= cy < region[3]

    g_parts = [c for c in golden.components if c.bbox and inside(c.bbox)]
    sd = diff_symbols(g_parts, found.components, max_dist)
    rename = {f.ref: g.ref for g, f in sd.matched + sd.wrong_type}
    g_conns = [j for j in golden.connectors if j.bbox and inside(j.bbox)]
    pairs = sorted(
        (((_center(g)[0] - _center(f)[0]) ** 2 + (_center(g)[1] - _center(f)[1]) ** 2) ** 0.5, gi, fi)
        for gi, g in enumerate(g_conns) for fi, f in enumerate(found.connectors))
    used_g, used_f = set(), set()
    for d, gi, fi in pairs:
        if d <= 3 * max_dist and gi not in used_g and fi not in used_f:
            used_g.add(gi), used_f.add(fi)
            rename[found.connectors[fi].ref] = g_conns[gi].ref
    out = NetDiff(unmatched=[c.ref for c in sd.missed] + [j.ref for i, j in enumerate(g_conns) if i not in used_g])

    gp = _pin_nets(golden)
    fp = {}
    for pin, net in _pin_nets(found).items():
        ref, name = pin.rsplit(".", 1)
        if ref in rename:
            fp[f"{rename[ref]}.{name}"] = net
    shared = sorted(set(gp) & set(fp))

    def mates(nets: dict[str, str], pin: str) -> set[str]:
        ref = pin.rsplit(".", 1)[0]
        return {p for p in shared if nets[p] == nets[pin] and p.rsplit(".", 1)[0] != ref}

    # Orient swappable pin pairs to agree with golden where the drawing cannot tell them apart.
    types = {c.ref: c.type for c in golden.components}
    for _ in range(2):
        for ref, typ in types.items():
            if typ not in swappable:
                continue
            a, b = (f"{ref}.{p}" for p in swappable[typ])
            if a not in fp or b not in fp or a not in gp:
                continue
            straight = len(mates(fp, a) & mates(gp, a)) + len(mates(fp, b) & mates(gp, b))
            swapped = len(mates(fp, a) & mates(gp, b)) + len(mates(fp, b) & mates(gp, a))
            if swapped > straight:
                fp[a], fp[b] = fp[b], fp[a]

    for p in shared:
        g, f = mates(gp, p), mates(fp, p)
        if g == f:
            out.pins_ok.append(p)
        else:
            out.pins_bad.append((p, sorted(g), sorted(f)))

    # Printed voltages: same value nearby, on the net that golden puts it on.
    g_net_pins = {}
    for p in shared:
        g_net_pins.setdefault(gp[p], set()).add(p)
    for a in golden.annotations:
        if not (a.bbox and inside(a.bbox)) or a.net not in g_net_pins:
            continue
        label = f"{a.voltage:g}V ({a.net})"
        cands = [b for b in found.annotations if b.bbox and abs(b.voltage - a.voltage) < 1e-6
                 and ((b.bbox[0] - a.bbox[0]) ** 2 + (b.bbox[1] - a.bbox[1]) ** 2) ** 0.5 < 150]
        ok = any({p for p in shared if fp[p] == b.net} == g_net_pins[a.net] for b in cands)
        (out.volts_ok if ok else out.volts_bad).append(label + ("" if cands else " not found"))
    return out
