"""Compare simulated node voltages to the voltages printed on the schematic."""

from __future__ import annotations

from dataclasses import dataclass

from .ir import IR
from .netlist import node


@dataclass
class Check:
    net: str
    printed: float
    simulated: float | None
    limit: float
    note: str | None

    @property
    def delta(self) -> float | None:
        return None if self.simulated is None else self.simulated - self.printed

    @property
    def ok(self) -> bool:
        return self.delta is not None and abs(self.delta) <= self.limit


def compare(ir: IR, voltages: dict[str, float], rel: float = 0.10, abs_: float = 0.05) -> list[Check]:
    checks = []
    for a in ir.annotations:
        sim = voltages.get(node(a.net).lower())
        if node(a.net) == "0":
            sim = 0.0
        checks.append(Check(a.net, a.voltage, sim, max(rel * abs(a.voltage), abs_), a.note))
    return checks


def report(checks: list[Check]) -> str:
    rows = ["| net | printed | simulated | delta | limit | ok | where |", "|---|---:|---:|---:|---:|:-:|---|"]
    for c in checks:
        sim = "missing" if c.simulated is None else f"{c.simulated:.3f}"
        d = "" if c.delta is None else f"{c.delta:+.3f}"
        rows.append(f"| {c.net} | {c.printed:.2f} | {sim} | {d} | {c.limit:.2f} | {'✓' if c.ok else '✗'} | {c.note or ''} |")
    n_ok = sum(c.ok for c in checks)
    rows.append(f"\n{n_ok}/{len(checks)} within tolerance")
    return "\n".join(rows)
