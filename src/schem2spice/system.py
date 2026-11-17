"""Chain boards into one circuit and check it against the published specifications.

A system file (systems/*.yaml) lists the boards (each an IR + its own testbench, which
keeps its rails, cuts and parameters), the links between them (the connector wiring,
checked on the sheet: `p401x:J408.1` = `p501:R529-2.2`), the extra pins that are
ground, and `cases`: switch/pot settings with an AC source and the spec checks.

Each board's nets and refs are prefixed with the board name, so `n12` of P501 becomes
`p501_n12` and its R501 `p501_R501`; ground stays shared. A board source or load on a
linked net (the stand-in input of one board, the stand-in load of the previous) is
dropped and reported, never silently.
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from . import ir as irmod
from . import netlist, project, sim, validate
from .ir import IR



class _DSU:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if rb == "gnd":  # ground wins as the name of a merged net
                ra, rb = rb, ra
            self.p[rb] = ra


@dataclass
class Board:
    name: str
    ir: IR  # prepared: cuts applied, nets prefixed
    tb: dict[str, Any]
    pins: dict[str, str]  # "R529-2.2" -> prefixed net


def _prefix(b: str, net: str) -> str:
    return net if net == "gnd" else f"{b}_{net}"


def _load_board(name: str, spec: dict[str, Any], params: dict[str, Any]) -> tuple[Board, list[str]]:
    ir = irmod.load(project.current().path(spec["ir"]))
    tb = yaml.safe_load(project.current().path(spec["tb"]).read_text())
    tb["params"] = {**tb.get("params", {}), **params}
    tb["cut"] = tb.get("cut", []) + spec.get("cut", [])
    tb["leaves"] = tb.get("leaves", []) + spec.get("leaves", [])
    ir, tb, notes = netlist.prepare(ir, tb)
    ren = {n: _prefix(name, n) for n in {n for c in ir.components for n in c.pins.values()}
           | {n for j in ir.connectors for n in j.pins.values()} | {a.net for a in ir.annotations}}
    comps = [c.model_copy(update={"ref": f"{name}_{c.ref}", "pins": {p: ren[n] for p, n in c.pins.items()}})
             for c in ir.components]
    conns = [j.model_copy(update={"ref": f"{name}_{j.ref}", "pins": {p: ren[n] for p, n in j.pins.items()}})
             for j in ir.connectors]
    annots = [a.model_copy(update={"net": ren[a.net], "note": f"{name}: {a.note}" if a.note else name})
              for a in ir.annotations]
    pins = {f"{c.ref}.{p}": ren[n] for c in ir.components for p, n in c.pins.items()}
    pins.update({f"{j.ref}.{p}": ren[n] for j in ir.connectors for p, n in j.pins.items()})
    out = irmod.derive_nets(ir.model_copy(update={"components": comps, "connectors": conns, "annotations": annots}))
    tb["params"] = {f"{name}_{k}": v for k, v in tb["params"].items()}
    for key in ("sources", "loads"):
        tb[key] = [dict(x, net=_prefix(name, x["net"])) for x in tb.get(key, [])]
    tb["_open"] = [_prefix(name, n) for n in tb.get("_open", [])]
    return Board(name, out, tb, pins), [f"{name}: {n}" for n in notes]


def _resolve(boards: dict[str, Board], ref: str) -> str:
    """"p501:R529-2.2" -> the prefixed net of that pin."""
    b, pin = ref.split(":", 1)
    if b not in boards:
        raise KeyError(f"{ref}: no board {b}")
    if pin not in boards[b].pins:
        raise KeyError(f"{ref}: pin {pin} not on board {b} (cut, or not in its IR)")
    return boards[b].pins[pin]


@dataclass
class Circuit:
    lines: list[str]
    libs: set[str]
    annotations: list  # printed voltages, nets renamed to the merged names
    nets: dict[str, str]  # "p501:R529-2.2" style names resolved later through `resolve`
    notes: list[str]
    resolve: Any  # callable: "board:pin" -> spice node


def build(system: dict[str, Any], case: dict[str, Any] | None = None) -> Circuit:
    case = case or {}
    params = {b: {**system.get("params", {}).get(b, {}), **case.get("params", {}).get(b, {})}
              for b in system["boards"]}
    boards, notes = {}, []
    for name, spec in system["boards"].items():
        boards[name], n = _load_board(name, spec, params[name])
        notes += n
    dsu = _DSU()
    linked: set[str] = set()
    for ln in system.get("links", []):
        a, b = _resolve(boards, ln["a"]), _resolve(boards, ln["b"])
        dsu.union(a, b)
        linked |= {a, b}
        notes.append(f"link {ln['a']} = {ln['b']}: {' '.join(ln.get('why', '').split())}")
    for g in system.get("ground", []):
        n = _resolve(boards, g["pin"])
        dsu.union("gnd", n)
        linked.add(n)
        notes.append(f"ground {g['pin']}: {' '.join(g.get('why', '').split())}")
    linked = {dsu.find(n) for n in linked}

    lines: list[str] = []
    libs: set[str] = set()
    annots = []
    for b in boards.values():
        merged = {n: dsu.find(n) for n in {n for c in b.ir.components for n in c.pins.values()}}
        ir = b.ir.model_copy(update={"components": [
            c.model_copy(update={"pins": {p: merged[n] for p, n in c.pins.items()}}) for c in b.ir.components]})
        tb = dict(b.tb)
        for key in ("sources", "loads"):
            keep = []
            for x in tb.get(key, []):
                if dsu.find(x["net"]) in linked:
                    notes.append(f"{b.name}: {key[:-1]} {x['name']} dropped: its net is linked to another board")
                else:
                    keep.append(dict(x, net=dsu.find(x["net"])))
            tb[key] = keep
        body, lb = netlist.emit_body(ir, tb)
        lines += [f"* --- board {b.name} ({b.ir.meta.get('crop', '')}) ---"] + body
        lines += netlist.testbench_lines(tb, suffix=f"_{b.name}")[1:]
        libs |= lb
        annots += [a.model_copy(update={"net": dsu.find(a.net)}) for a in b.ir.annotations]

    def resolve(ref: str) -> str:
        return netlist.node(dsu.find(_resolve(boards, ref)))

    lines.append("* --- system sources and loads ---")
    for s in case.get("sources", []):
        ac = f" AC {s['ac']}" if "ac" in s else ""
        lines.append(f"{s['name']} {resolve(s['net'])} 0 DC {s.get('dc', 0)}{ac}")
    for ld in system.get("loads", []) + case.get("loads", []):
        lines.append(f"{ld['name']} {resolve(ld['net'])} 0 {netlist.spice_value(str(ld['value']))}")
    return Circuit(lines, libs, annots, {}, notes, resolve)


def _deck(title: str, c: Circuit, control: list[str]) -> str:
    head = [f"* {title}"] + [f"* {n}" for n in c.notes]
    head += [f".include {project.model_file(lib_)}" for lib_ in sorted(c.libs)]
    return "\n".join(head + c.lines + [".control"] + control + [".endc", ".end"]) + "\n"


def run_op(c: Circuit, path: Path, title: str, rel: float, abs_: float) -> list[validate.Check]:
    path.write_text(_deck(title, c, ["op", "print all"]))
    volts = sim.run_op(path)
    return validate.compare(IR(annotations=c.annotations), volts, rel, abs_)


def run_ac(c: Circuit, path: Path, title: str, probes: dict[str, str], fstart: float = 1,
           fstop: float = 1e6, per_decade: int = 40) -> dict[str, np.ndarray]:
    """AC sweep; returns {"freq": f, name: complex response} for each probe, where a
    probe is a node ("v(p701_n35)") or a source current ("i(vinl)")."""
    data = path.with_suffix(".ac.txt")
    vecs = " ".join(probes.values())
    path.write_text(_deck(title, c, [f"ac dec {per_decade} {fstart} {fstop}",
                                     "set wr_singlescale", "set wr_vecnames", f"wrdata {data} {vecs}"]))
    proc = subprocess.run(["ngspice", "-b", str(path)], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not data.exists():
        raise RuntimeError(f"ngspice failed ({proc.returncode}):\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    rows = data.read_text().split("\n")
    arr = np.array([[float(x) for x in r.split()] for r in rows[1:] if r.strip()])
    out = {"freq": arr[:, 0]}
    for k, name in enumerate(probes):  # wr_singlescale: freq, then re/im pairs
        out[name] = arr[:, 1 + 2 * k] + 1j * arr[:, 2 + 2 * k]
    return out


def at(resp: dict[str, np.ndarray], name: str, f: float) -> complex:
    """Response interpolated at frequency f (log-frequency, on magnitude and phase)."""
    fr = resp["freq"]
    lf = np.log10(fr)
    mag = np.interp(math.log10(f), lf, np.log(np.abs(resp[name])))
    ph = np.interp(math.log10(f), lf, np.unwrap(np.angle(resp[name])))
    return complex(math.exp(mag) * math.cos(ph), math.exp(mag) * math.sin(ph))


def db(x: complex | float) -> float:
    return 20 * math.log10(abs(x))


def node_probe(c: Circuit, ref: str) -> str:
    return f"v({c.resolve(ref)})"


def source_probe(name: str) -> str:
    return f"i({name.lower()})"


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name)


def load(path: Path | str) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text())


# --- spec checks --------------------------------------------------------------

@dataclass
class Result:
    case: str
    spec: str
    what: str
    value: str
    expect: str
    ok: bool


def riaa(f: np.ndarray | float) -> np.ndarray | complex:
    """RIAA playback response (3180 / 318 / 75 us), unnormalized."""
    s = 2j * np.pi * np.asarray(f)
    return (1 + s * 318e-6) / ((1 + s * 3180e-6) * (1 + s * 75e-6))


def evaluate(system: dict[str, Any], out_dir: Path, only: list[str] | None = None) -> tuple[str, list[Result]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []
    responses: dict[str, dict[str, np.ndarray]] = {}
    sections: list[str] = []
    outputs = system.get("outputs", {})
    for name, case in system["cases"].items():
        if only and name not in only:
            continue
        c = build(system, case)
        probes = {k: node_probe(c, v) for k, v in outputs.items()}
        probes.update({s["name"]: source_probe(s["name"]) for s in case.get("sources", [])})
        probes.update({k: node_probe(c, v) for k, v in case.get("probes", {}).items()})
        resp = run_ac(c, out_dir / f"{name}.cir", f"system {name}", probes)
        responses[name] = resp
        _write_csv(out_dir / f"{name}_response.csv", resp, [k for k in probes if k in outputs or k in case.get("probes", {})])
        for chk in case.get("checks", []):
            results.append(_check(name, chk, resp, responses))
        sections.append(f"- **{name}**: {case.get('note', '')}")
    rows = ["| case | spec | measured | expected | ok |", "|---|---|---:|---:|:-:|"]
    rows += [f"| {r.case} | {r.spec} | {r.value} | {r.expect} | {'✓' if r.ok else '✗'} |" for r in results]
    text = "# System checks\n\n" + "\n".join(sections) + "\n\n" + "\n".join(rows)
    text += f"\n\n{sum(r.ok for r in results)}/{len(results)} within the specification\n"
    return text, results


def _check(case: str, chk: dict[str, Any], resp: dict[str, np.ndarray], prior: dict) -> Result:
    kind, spec = chk["kind"], chk.get("spec", "")
    if kind == "gain":  # |out / in| at f, the input source being 1 V AC
        g = db(at(resp, chk["out"], chk["f"]))
        ok = abs(g - chk["db"]) <= chk["tol_db"]
        return Result(case, spec, kind, f"{g:.2f} dB at {chk['f']:g} Hz", f"{chk['db']:.2f} ± {chk['tol_db']} dB", ok)
    if kind == "flat":  # response at each f relative to ref_f
        ref = db(at(resp, chk["out"], chk["ref_f"]))
        rel = [db(at(resp, chk["out"], f)) - ref for f in chk["f"]]
        ok = all(r >= chk["min_db"] for r in rel) and all(r <= chk.get("max_db", 1e9) for r in rel)
        got = ", ".join(f"{r:+.2f} dB at {f:g} Hz" for r, f in zip(rel, chk["f"]))
        return Result(case, spec, kind, got, f">= {chk['min_db']} dB", ok)
    if kind == "zin":  # source current of a 1 V AC source
        z = 1 / abs(at(resp, chk["source"], chk["f"]))
        ok = abs(z - chk["ohms"]) <= chk["tol"] * chk["ohms"]
        return Result(case, spec, kind, f"{z / 1e3:.1f} kΩ", f"{chk['ohms'] / 1e3:g} kΩ ± {chk['tol'] * 100:g} %", ok)
    if kind == "relative":  # this case against another at f (tone controls)
        g = db(at(resp, chk["out"], chk["f"])) - db(at(prior[chk["vs"]], chk["out"], chk["f"]))
        ok = abs(g - chk["db"]) <= chk["tol_db"]
        return Result(case, spec, kind, f"{g:+.2f} dB at {chk['f']:g} Hz vs {chk['vs']}",
                      f"{chk['db']:+.1f} ± {chk['tol_db']} dB", ok)
    if kind == "riaa":  # this case / the flat case, against the RIAA curve, both at 1 kHz = 0
        fs = np.logspace(math.log10(chk["f"][0]), math.log10(chk["f"][1]), 31)
        dev = []
        for f in fs:
            mine = db(at(resp, chk["out"], f)) - db(at(prior[chk["vs"]], chk["out"], f))
            dev.append(mine - db(riaa(f)))
        ref = (db(at(resp, chk["out"], 1000)) - db(at(prior[chk["vs"]], chk["out"], 1000))) - db(riaa(1000))
        dev = [d - ref for d in dev]
        worst = max(dev, key=abs)
        f_worst = fs[dev.index(worst)]
        ok = abs(worst) <= chk["tol_db"]
        return Result(case, spec, kind, f"worst {worst:+.2f} dB at {f_worst:.0f} Hz", f"± {chk['tol_db']} dB", ok)
    raise ValueError(f"unknown check kind {kind}")


def _write_csv(path: Path, resp: dict[str, np.ndarray], names: list[str]) -> None:
    head = "freq_hz," + ",".join(f"{n}_db,{n}_deg" for n in names)
    lines = [head]
    for i, f in enumerate(resp["freq"]):
        cols = [f"{f:.6g}"]
        for n in names:
            v = resp[n][i]
            cols += [f"{db(v):.4f}", f"{math.degrees(np.angle(v)):.2f}"]
        lines.append(",".join(cols))
    path.write_text("\n".join(lines) + "\n")


def evaluate_op(system: dict[str, Any], case: str, out_dir: Path, rel: float = 0.10, abs_: float = 0.05):
    c = build(system, system["cases"][case])
    return run_op(c, out_dir / f"{case}_op.cir", f"system {case} op", rel, abs_), c.notes
