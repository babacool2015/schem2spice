"""Emit an ngspice netlist from IR + model map + testbench."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .ir import IR

from . import project

GROUND_NETS = {"gnd", "0"}
SWITCH_ON, SWITCH_OFF = 1e-3, 1e12  # ohms: closed / open switch contact


def load_library(path: Path | None = None) -> dict[str, Any]:
    """Symbol library: `path`, else the shared library.yaml with the project's overlay
    (same name = replaced, new names added)."""
    if path:
        return yaml.safe_load(path.read_text())
    lib: dict[str, Any] = {}
    for d in reversed(project.symbol_dirs()):
        if (d / "library.yaml").exists():
            lib.update(yaml.safe_load((d / "library.yaml").read_text()) or {})
    return lib


def load_model_map(models_dir: Path | None = None) -> dict[str, Any]:
    """Part value -> SPICE model: `models_dir`/map.yaml, else shared + project overlay."""
    dirs = [models_dir] if models_dir else list(reversed(project.model_dirs()))
    mmap: dict[str, Any] = {}
    for d in dirs:
        if (d / "map.yaml").exists():
            mmap.update(yaml.safe_load((d / "map.yaml").read_text()) or {})
    return mmap


def spice_value(value: str) -> str:
    """Schematic value -> SPICE number. 'M' on a schematic means mega, in SPICE milli."""
    v = value.strip().replace("Ω", "").replace("ohm", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([pnumkMG]?)", v)
    if not m:
        raise ValueError(f"unparseable value {value!r}")
    num, suffix = m.groups()
    return num + {"M": "Meg", "G": "G"}.get(suffix, suffix)


def to_ohms(value: str) -> float:
    scale = {"": 1, "k": 1e3, "Meg": 1e6, "m": 1e-3}
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Meg|k|m)?", spice_value(value))
    if not m:
        raise ValueError(value)
    return float(m.group(1)) * scale[m.group(2) or ""]


def node(net: str) -> str:
    return "0" if net.lower() in GROUND_NETS else net


def element_name(letter: str, ref: str) -> str:
    return letter + re.sub(r"[^A-Za-z0-9_]", "_", ref)


def _pin_nets(ir: IR) -> dict[str, str]:
    out = {f"{c.ref}.{p}": n for c in ir.components for p, n in c.pins.items()}
    out.update({f"{j.ref}.{p}": n for j in ir.connectors for p, n in j.pins.items()})
    return out


def name_supplies(ir: IR) -> tuple[IR, list[str]]:
    """An op-amp section's drawn supply pin is named V+ (top) or V- (bottom) by its
    position in the template, but sheets do not keep to that (P601 Q604-2 draws V- on
    top). Rename it from the sign of the printed voltages on its net; without labels
    the positional name stands and is reported."""
    by_net: dict[str, list[float]] = {}
    for a in ir.annotations:
        by_net.setdefault(a.net, []).append(a.voltage)
    notes, comps = [], []
    for c in ir.components:
        if c.type == "opamp":
            pins = dict(c.pins)
            for p in [p for p in ("V+", "V-") if p in c.pins]:
                v = by_net.get(c.pins[p], [])
                want = "V+" if v and min(v) > 1 else "V-" if v and max(v) < -1 else None
                if want is None:
                    notes.append(f"{c.ref}.{p}: no printed rail voltage on its net; named by position")
                elif want != p and want not in pins:
                    pins[want] = pins.pop(p)
                    notes.append(f"{c.ref}: drawn supply pin is {want} (printed {v[0]:+g} V), not {p}")
            c = c.model_copy(update={"pins": pins})
        comps.append(c)
    return ir.model_copy(update={"components": comps}), notes


def prepare(ir: IR, testbench: dict[str, Any]) -> tuple[IR, dict[str, Any], list[str]]:
    """Bind a testbench to an IR, so one testbench serves the golden and the extracted
    IR (whose nets are named n1, n2...). Pure; returns (ir, testbench, notes).

    - `net:` of a source/load may be a pin, "JM11.3", instead of a net name.
    - `ground: R501.2` names the net of that pin "gnd" (crops often hold no ground symbol).
    - `bind: [{pin: Q401-1.V-, to: JM31.6, why: ...}]` gives a part a pin the crop does
      not show (an op-amp section whose supply is drawn on its sibling outside the crop).
    - `cut: [{refs: [...], why: ...}]` removes parts outside the simulated scope; a group
      may give `region: [x0, y0, x1, y1]` (page px) instead, cutting every part whose
      bbox centre lies inside (for parts OCR never named) except those in `keep:`. Each cut
      is reported in the notes and as a comment in the netlist, never silent. Printed
      voltages on nets that only cut parts touched are set aside ("not simulated").
    - `leaves: [{pin: RN08.2, why: ...}]` declares a wire that runs off the crop to a
      part outside the simulated scope; the pin's net is left open (ERC does not count
      it as a floating pin) and the reason goes in the notes.
    """
    ir, notes = name_supplies(ir)
    pins = _pin_nets(ir)

    def resolve(ref: str) -> str:
        if "." not in ref:
            return ref
        if ref not in pins:
            raise KeyError(f"testbench pin {ref} not in the IR")
        return pins[ref]

    rename: dict[str, str] = {}
    if g := testbench.get("ground"):
        rename[resolve(g)] = "gnd"
    ren = lambda n: rename.get(n, n)  # noqa: E731

    present = {c.ref for c in ir.components}
    cut: set[str] = set()
    def inside(c, r) -> bool:
        if not c.bbox:
            return False
        x, y, w, h = c.bbox
        return r[0] <= x + w / 2 < r[2] and r[1] <= y + h / 2 < r[3]

    for group in testbench.get("cut", []):
        here = [r for r in group.get("refs", []) if r in present]
        if "region" in group:
            here += [c.ref for c in ir.components if inside(c, group["region"]) and c.ref not in here
                     and c.ref not in group.get("keep", ())]
        cut.update(here)
        if here:
            notes.append(f"cut {', '.join(here)}: {' '.join(group.get('why', '').split())}")
    bound: dict[str, dict[str, str]] = {}
    for b in testbench.get("bind", []):
        ref, pin = b["pin"].rsplit(".", 1)
        if ref not in present:
            continue
        bound.setdefault(ref, {})[pin] = resolve(b["to"])
        notes.append(f"bind {b['pin']} to {b['to']}: {' '.join(b.get('why', '').split())}")
    comps = [c.model_copy(update={"pins": {p: ren(n) for p, n in {**c.pins, **bound.get(c.ref, {})}.items()}})
             for c in ir.components if c.ref not in cut]
    conns = [j.model_copy(update={"pins": {p: ren(n) for p, n in j.pins.items()}}) for j in ir.connectors]
    live = {n for c in comps for n in c.pins.values()}
    annots = []
    for a in ir.annotations:
        a = a.model_copy(update={"net": ren(a.net)})
        if a.net in live:
            annots.append(a)
        else:
            notes.append(f"not simulated: {a.voltage:+.2f} V on {a.net} ({a.note or 'only cut parts touch it'})")
    nets = [n.model_copy(update={"name": ren(n.name)}) for n in ir.nets if ren(n.name) in live]
    out = ir.model_copy(update={"components": comps, "connectors": conns, "annotations": annots, "nets": nets})
    tb = dict(testbench)
    tb["_open"] = []
    for lv in testbench.get("leaves", []):
        if lv["pin"].split(".")[0] in cut or lv["pin"] not in pins:
            continue
        tb["_open"].append(ren(pins[lv["pin"]]))
        notes.append(f"open {lv['pin']}: {' '.join(lv.get('why', '').split())}")
    for key in ("sources", "loads"):
        tb[key] = [dict(x, net=ren(resolve(x["net"]))) for x in testbench.get(key, [])]
    tb["_notes"] = notes
    return out, tb, notes


def _sibling_pins(ir: IR, c, missing: set[str]) -> dict[str, str]:
    """Fill optional pins a section does not draw (an op-amp half's supply) from the
    other sections of the same package: "Q401-1" takes V- from "Q401-2"."""
    base = c.ref.rsplit("-", 1)[0]
    pins = dict(c.pins)
    for s in ir.components:
        if s is not c and s.type == c.type and s.ref.rsplit("-", 1)[0] == base:
            pins.update({p: n for p, n in s.pins.items() if p in missing and p not in pins})
    if lost := missing - set(pins):
        raise ValueError(f"{c.ref}: pins {sorted(lost)} not drawn on this section or any sibling of {base}")
    return pins


def emit(ir: IR, testbench: dict[str, Any], title: str = "schem2spice") -> str:
    lines, libs = emit_body(ir, testbench)
    lines += testbench_lines(testbench)
    head = [f"* {title}", f"* source: {ir.meta.get('source')}  board: {ir.meta.get('board')}  module: {ir.meta.get('module')}"]
    head += [f"* {n}" for n in testbench.get("_notes", [])]
    head += [f".include {project.model_file(lib_)}" for lib_ in sorted(libs)]
    tail = [".control", "op", "print all", ".endc", ".end"]
    return "\n".join(head + lines + tail) + "\n"


def testbench_lines(testbench: dict[str, Any], suffix: str = "") -> list[str]:
    """Source and load lines; `suffix` keeps names unique when boards are chained (the
    first letter is the SPICE element type, so it cannot be a prefix)."""
    lines = ["* testbench"]
    for s in testbench.get("sources", []):
        ac = f" AC {s['ac']}" if "ac" in s else ""
        lines.append(f"{s['name']}{suffix} {node(s['net'])} 0 DC {s.get('dc', 0)}{ac}")
    for ld in testbench.get("loads", []):
        lines.append(f"{ld['name']}{suffix} {node(ld['net'])} 0 {spice_value(ld['value'])}")
    return lines


def emit_body(ir: IR, testbench: dict[str, Any]) -> tuple[list[str], set[str]]:
    """The circuit's element lines (no sources, no control) and the model libraries
    they need. `testbench` gives switch/pot params and the driven nets."""
    lib = load_library()
    mmap = load_model_map()
    params = testbench.get("params", {})
    lines: list[str] = []
    libs: set[str] = set()

    for c in ir.components:
        sym = lib[c.type]
        spec = sym["spice"]
        optional = set(sym.get("optional_pins", ()))
        if not set(sym["pins"]) - optional <= set(c.pins) <= set(sym["pins"]):
            raise ValueError(f"{c.ref}: pins {sorted(c.pins)} do not match symbol {c.type} {sym['pins']}")
        if set(c.pins) != set(sym["pins"]):
            c = c.model_copy(update={"pins": _sibling_pins(ir, c, set(sym["pins"]) - set(c.pins))})

        if spec.get("expand") == "potentiometer":
            pos = float(params.get(c.ref, 0.5))
            total = to_ohms(c.value)
            ra, rb = max(pos * total, 1e-3), max((1 - pos) * total, 1e-3)
            for suffix, a, b, r in (("A", c.pins["1"], c.pins["W"], ra), ("B", c.pins["W"], c.pins["2"], rb)):
                if node(a) == node(b):
                    lines.append(f"* {c.ref}.{suffix} shorted by wiper ({a})")
                    continue
                lines.append(f"{element_name('R', c.ref)}_{suffix} {node(a)} {node(b)} {r:.6g}")
            continue

        if spec.get("expand") == "center_tap":
            for suffix, a, b in (("A", c.pins["1"], c.pins["M"]), ("B", c.pins["M"], c.pins["2"])):
                lines.append(f"{element_name('R', c.ref)}_{suffix} {node(a)} {node(b)} {spice_value(c.value)}")
            continue

        if spec.get("expand") == "series_rc":  # value "0.47u+6.8": C in series with R
            cval, rval = c.value.split("+")
            mid = f"{element_name('N', c.ref)}_m"
            lines.append(f"{element_name('C', c.ref)} {node(c.pins['1'])} {mid} {spice_value(cval)}")
            lines.append(f"{element_name('R', c.ref)} {mid} {node(c.pins['2'])} {spice_value(rval)}")
            continue

        if spec.get("expand") == "switch":
            base = c.ref.rsplit("-", 1)[0]
            state = str(params.get(c.ref, params.get(base, "")))
            if state not in ("drawn", "other"):
                raise KeyError(f"{c.ref}: testbench params need {base}: drawn | other (switch position)")
            closed = "2" if state == "drawn" else "1"
            for throw in ("1", "2"):
                r = SWITCH_ON if throw == closed else SWITCH_OFF
                lines.append(f"{element_name('R', c.ref)}_{throw} {node(c.pins['C'])} {node(c.pins[throw])} {r:g}")
            continue

        if spec.get("expand") == "rotary":
            base = c.ref.rsplit("-", 1)[0]
            state = str(params.get(c.ref, params.get(base, "")))
            state = sym.get("drawn", "") if state == "drawn" else state
            throws = [p for p in sym["pins"] if p != "C"]
            if state not in throws:
                raise KeyError(f"{c.ref}: testbench params need {base}: drawn | {' | '.join(throws)} (selector position)")
            for throw in throws:
                if throw in c.pins:
                    r = SWITCH_ON if throw == state else SWITCH_OFF
                    lines.append(f"{element_name('R', c.ref)}_{throw} {node(c.pins['C'])} {node(c.pins[throw])} {r:g}")
            continue

        nodes = " ".join(node(c.pins[p]) for p in spec["order"])
        name = element_name(spec["letter"], c.ref)
        if spec["letter"] in ("R", "C", "L"):
            lines.append(f"{name} {nodes} {spice_value(c.value)}")
        else:
            entry = mmap.get(c.value)
            if entry is None:
                raise KeyError(f"{c.ref}: no model mapping for {c.value!r} in models/map.yaml (shared or project)")
            libs.add(entry["lib"])
            lines.append(f"{name} {nodes} {entry['model']}")

    # A node reached only through capacitors has no DC path (P851 C864 on a line that
    # leaves the sheet): tie it to ground through 1 Gohm so the operating point exists.
    driven = {node(s["net"]) for s in testbench.get("sources", [])} | {node(x["net"]) for x in testbench.get("loads", [])}
    kinds: dict[str, set[str]] = {}
    for ln in lines:
        if ln.startswith("*"):
            continue
        parts = ln.split()
        nodes_ = parts[1:3] if parts[0][0] in "RCLDV" else parts[1:-1]
        for n_ in nodes_:
            kinds.setdefault(n_, set()).add(parts[0][0])
    for n_, ks in sorted(kinds.items()):
        if n_ != "0" and ks == {"C"} and n_ not in driven:
            lines.append(f"RDC_{n_} {n_} 0 1G  ; DC path for a node reached only through capacitors")
    return lines, libs
