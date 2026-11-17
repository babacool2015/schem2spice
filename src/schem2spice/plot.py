"""Frequency-response charts from a system run (out/sys/<case>_response.csv).

A system file may list `plots:`, each one chart:

    plots:
      - name: tone                       # -> <out>/<name>.svg
        title: Tone controls at the extremes
        ylabel: Change from flat (dB)
        x: [10, 100000]                  # Hz, log axis
        y: [-12, 12]                     # dB (default: fitted to the curves)
        out: L                           # output probe (default: the first in `outputs`)
        curves:
          - {case: bass_boost, minus: line, label: Bass boost}
          - {case: bass_cut, minus: line, label: Bass cut, dash: true}
          - {ideal: riaa, normalize: 1000, label: Ideal RIAA}
        bands:   [{x: [10, 100000], y: [-1, 0], label: "-1 dB window"}]
        markers: [{f: 100, db: 7, label: "spec +7 dB"}]

A curve is a case's response in dB, optionally minus another case's (`minus`), minus
the ideal RIAA curve (`minus_ideal: riaa`), and shifted to 0 dB at `normalize` Hz.
`{ideal: riaa}` draws the reference curve itself. The SVGs are self-contained (white
background, so they read the same in a README on a light or dark page), and
`<system>_plots.html` shows them all with the spec results.
"""

from __future__ import annotations

import csv
import html
import math
from pathlib import Path
from typing import Any

import numpy as np

# Categorical order validated for colour-vision deficiency on a light surface;
# a second curve of the same quantity (cut vs boost) keeps the hue and is dashed.
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#c98500", "#6250d6", "#e34948"]
REF = "#6f6e69"
W, H = 900, 420
M = {"l": 64, "r": 20, "t": 70, "b": 44}


def load_response(path: Path) -> dict[str, np.ndarray]:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    return {k: np.array([float(r[k]) for r in rows]) for k in rows[0]}


def riaa_db(f: np.ndarray) -> np.ndarray:
    w = 2 * np.pi * f
    return 20 * np.log10(np.hypot(1, w * 318e-6) / (np.hypot(1, w * 3180e-6) * np.hypot(1, w * 75e-6)))


def _interp(freq: np.ndarray, y: np.ndarray, f: float) -> float:
    return float(np.interp(math.log10(f), np.log10(freq), y))


def curve(spec: dict[str, Any], responses: dict[str, dict[str, np.ndarray]], out: str) -> tuple[np.ndarray, np.ndarray]:
    if spec.get("ideal") == "riaa":
        f = next(iter(responses.values()))["freq_hz"]
        y = riaa_db(f)
    else:
        r = responses[spec["case"]]
        f, y = r["freq_hz"], r[f"{out}_db"].copy()
        if "minus" in spec:
            y = y - responses[spec["minus"]][f"{out}_db"]
        if spec.get("minus_ideal") == "riaa":
            y = y - riaa_db(f)  # with `normalize`, the difference is shifted as a whole
    if "normalize" in spec:
        y = y - _interp(f, y, spec["normalize"])
    return f, y


def _fmt_hz(f: float) -> str:
    for div, unit in ((1e6, "MHz"), (1e3, "kHz")):
        if f >= div:
            v = f / div
            return f"{v:g}{unit}"
    return f"{f:g}Hz"


def _nice_step(span: float) -> float:
    raw = span / 8
    mag = 10 ** math.floor(math.log10(raw))
    return next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)


def svg(p: dict[str, Any], responses: dict[str, dict[str, np.ndarray]], out: str) -> str:
    x0, x1 = p.get("x", [10, 100000])
    curves = []
    for i, c in enumerate(p["curves"]):
        f, y = curve(c, responses, c.get("out", out))
        keep = (f >= x0 * 0.999) & (f <= x1 * 1.001)
        color = REF if "ideal" in c else COLORS[c.get("color", i) % len(COLORS)]
        curves.append((c, f[keep], y[keep], color))
    if "y" in p:
        y0, y1 = p["y"]
    else:
        lo = min(float(y.min()) for _, _, y, _ in curves)
        hi = max(float(y.max()) for _, _, y, _ in curves)
        step = _nice_step(max(hi - lo, 1))
        y0, y1 = math.floor(lo / step) * step, math.ceil(hi / step) * step
    ystep = p.get("ystep", _nice_step(y1 - y0))
    pw, ph = W - M["l"] - M["r"], H - M["t"] - M["b"]
    lx0, lx1 = math.log10(x0), math.log10(x1)

    def X(f: float) -> float:
        return M["l"] + (math.log10(f) - lx0) / (lx1 - lx0) * pw

    def Y(v: float) -> float:
        return M["t"] + (y1 - v) / (y1 - y0) * ph

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
         f'font-family="Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         f'<text x="{M["l"]}" y="22" font-size="16" font-weight="600" fill="#141412">{html.escape(p.get("title", p["name"]))}</text>',
         f'<clipPath id="plot"><rect x="{M["l"]}" y="{M["t"]}" width="{pw}" height="{ph}"/></clipPath>']
    for b in p.get("bands", []):
        bx0, bx1 = max(b["x"][0], x0), min(b["x"][1], x1)
        by0, by1 = max(b["y"][0], y0), min(b["y"][1], y1)
        o.append(f'<rect x="{X(bx0):.1f}" y="{Y(by1):.1f}" width="{X(bx1) - X(bx0):.1f}" height="{Y(by0) - Y(by1):.1f}" '
                 f'fill="#2a78d6" fill-opacity="0.08"/>')
    for d in range(math.floor(lx0), math.ceil(lx1) + 1):
        for k in range(1, 10):
            f = k * 10 ** d
            if f < x0 * 0.999 or f > x1 * 1.001:
                continue
            o.append(f'<line x1="{X(f):.1f}" x2="{X(f):.1f}" y1="{M["t"]}" y2="{M["t"] + ph}" stroke="#e4e3de" '
                     f'stroke-width="{1 if k == 1 else 0.5}"/>')
            if k == 1 or (k in (2, 5) and lx1 - lx0 <= 3):
                o.append(f'<text x="{X(f):.1f}" y="{H - 22}" text-anchor="middle" fill="#6f6e69">{_fmt_hz(f)}</text>')
    for i in range(int(round((y1 - y0) / ystep)) + 1):
        v = round(y0 + i * ystep, 9)
        zero = abs(v) < 1e-9 and y0 < 0
        o.append(f'<line x1="{M["l"]}" x2="{M["l"] + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" '
                 f'stroke="{"#b9b8b2" if zero else "#e4e3de"}" stroke-width="1"/>')
        lab = f"{v:+g}" if y0 < 0 and v else f"{v:g}"
        o.append(f'<text x="{M["l"] - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" fill="#6f6e69">{lab.replace("-", "−")}</text>')
    o.append(f'<text x="16" y="{M["t"] + ph / 2:.0f}" text-anchor="middle" fill="#52514e" '
             f'transform="rotate(-90 16 {M["t"] + ph / 2:.0f})">{html.escape(p.get("ylabel", "dB"))}</text>')
    for g in p.get("guides", []):
        o.append(f'<line x1="{X(max(g["x"][0], x0)):.1f}" x2="{X(min(g["x"][1], x1)):.1f}" y1="{Y(g["db"]):.1f}" '
                 f'y2="{Y(g["db"]):.1f}" stroke="#52514e" stroke-dasharray="5 4"/>')
    for c, f, y, color in curves:
        pts = " ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in zip(f, y))
        dash = ' stroke-dasharray="7 5"' if c.get("dash") or "ideal" in c else ""
        o.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
                 f'stroke-linejoin="round" clip-path="url(#plot)"{dash}/>')
    for mk in p.get("markers", []):
        x, y, r = X(mk["f"]), Y(mk["db"]), 6
        o.append(f'<path d="M{x:.1f},{y - r:.1f}L{x + r:.1f},{y:.1f}L{x:.1f},{y + r:.1f}L{x - r:.1f},{y:.1f}Z" '
                 f'fill="#ffffff" stroke="#141412" stroke-width="1.5"><title>{html.escape(mk.get("label", ""))}</title></path>')
    # legend: a row under the title, left-aligned, wrapping
    lx, ly = M["l"], 46
    items = [(html.escape(c.get("label", c.get("case", ""))), color, bool(c.get("dash") or "ideal" in c))
             for c, _, _, color in curves]
    for label, color, dashed in items + ([("spec", None, False)] if p.get("markers") else []):
        width = 30 + 6.6 * len(label) + 18
        if lx + width > W - M["r"]:
            lx, ly = M["l"], ly + 16
        if color is None:
            o.append(f'<path d="M{lx + 10},{ly - 5}L{lx + 15},{ly}L{lx + 10},{ly + 5}L{lx + 5},{ly}Z" fill="#fff" '
                     f'stroke="#141412" stroke-width="1.5"/>')
        else:
            dash = ' stroke-dasharray="5 3"' if dashed else ""
            o.append(f'<line x1="{lx}" x2="{lx + 22}" y1="{ly}" y2="{ly}" stroke="{color}" stroke-width="2"{dash}/>')
        o.append(f'<text x="{lx + 30}" y="{ly + 4}" fill="#52514e">{label}</text>')
        lx += width
    o.append("</svg>")
    return "\n".join(o)


def render(system: dict[str, Any], out_dir: Path, name: str, report: str | None = None) -> list[Path]:
    """Write one SVG per plot of the system file and <name>_plots.html; return the paths."""
    plots = system.get("plots", [])
    if not plots:
        return []
    out = next(iter(system.get("outputs", {"L": None})))
    needed = {c[k] for p in plots for c in p["curves"] for k in ("case", "minus") if k in c}
    responses = {}
    for case in needed:
        path = out_dir / f"{case}_response.csv"
        if not path.exists():
            raise FileNotFoundError(f"{path}: run the system first (case {case})")
        responses[case] = load_response(path)
    written = []
    figures = []
    for p in plots:
        s = svg(p, responses, p.get("out", out))
        path = out_dir / f"{p['name']}.svg"
        path.write_text(s + "\n")
        written.append(path)
        note = f"<p>{html.escape(p['note'])}</p>" if p.get("note") else ""
        figures.append(f'<figure id="{html.escape(p["name"])}">{s}{note}</figure>')
    page = out_dir / f"{name}_plots.html"
    rep = f"<h2>Spec checks</h2><pre>{html.escape(report)}</pre>" if report else ""
    page.write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(name)} frequency response</title>
<style>body{{font-family:Helvetica,Arial,sans-serif;max-width:940px;margin:24px auto;padding:0 16px;color:#141412;background:#f6f6f3}}
figure{{margin:0 0 28px;background:#fff;border:1px solid #e4e3de;border-radius:6px;padding:8px}}
figure svg{{width:100%;height:auto;display:block}} figure p{{margin:6px 8px;color:#52514e;font-size:14px}}
pre{{background:#fff;border:1px solid #e4e3de;padding:12px;overflow-x:auto;font-size:12px}}</style></head>
<body><h1>{html.escape(name)}: frequency response</h1>
{"".join(figures)}{rep}</body></html>
""")
    written.append(page)
    return written
