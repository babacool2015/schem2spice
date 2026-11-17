"""Electrical rule checks on the IR."""

from __future__ import annotations

from .ir import IR, derive_nets
from .netlist import load_library


def conflicting_voltages(ir: IR, rel: float = 0.10, abs_: float = 0.1) -> dict[str, list[float]]:
    """Nets whose printed voltages cannot all be true: a label attached to the wrong
    wire. Spread beyond max(rel * |V|, abs_) counts as a conflict."""
    by_net: dict[str, list[float]] = {}
    for a in ir.annotations:
        by_net.setdefault(a.net, []).append(a.voltage)
    return {n: sorted(v) for n, v in by_net.items()
            if max(v) - min(v) > max(rel * max(abs(x) for x in v), abs_)}


def check(ir: IR, driven: set[str] = frozenset(), voltages: bool = True) -> list[str]:
    """Return a list of problems. `driven` = nets the testbench connects to (sources, loads)."""
    problems: list[str] = []
    ir = derive_nets(ir)
    conn_refs = {j.ref for j in ir.connectors}
    lib = load_library()
    floaters = {f"{c.ref}.{p}" for c in ir.components for p in lib[c.type].get("may_float", ()) if p in c.pins}
    for n in ir.nets:
        if all(p.split(".")[0] in conn_refs for p in n.pins):
            continue  # only connector pins: an off-board signal, not a floating part pin
        if all(p in floaters for p in n.pins):
            continue  # an unused switch contact
        if len(n.pins) < 2 and n.name not in driven and n.name != "gnd":
            problems.append(f"net {n.name}: single connection {n.pins}")
    for c in ir.components:
        if len(set(c.pins.values())) == 1 and c.type != "potentiometer":
            problems.append(f"{c.ref}: all pins on one net ({next(iter(c.pins.values()))})")
    for net, volts in (conflicting_voltages(ir) if voltages else {}).items():
        problems.append(f"net {net}: printed voltages disagree {volts}")
    known = {n.name for n in ir.nets}
    for a in ir.annotations:
        if a.net not in known:
            problems.append(f"annotation {a.voltage} V on unknown net {a.net}")
    return problems
