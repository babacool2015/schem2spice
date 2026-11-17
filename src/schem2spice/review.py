"""Stage 7: human review.

- `render_trace`: each traced net in its own colour over the dimmed crop, junction
  dots circled, symbol pins green (red if they touch no wire), arrows label -> tip.
- correction store: `corrections/<crop>.yaml`, re-applied on every extract.
- `review_sheet`: HTML page listing what needs a human look, with the scan around
  each part and the correction on file.
"""

from __future__ import annotations

import colorsys

import cv2
import numpy as np

from .trace import TraceResult


def render_trace(crop: np.ndarray, res: TraceResult, scale: float = 0.5) -> np.ndarray:
    vis = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR) // 3 + 170
    for k, segs in enumerate(res.nets.values()):
        r, g, b = colorsys.hsv_to_rgb((k * 0.618) % 1, 1, 0.85)
        for i in segs:
            s = res.segments[i]
            cv2.rectangle(vis, (s.x0, s.y0), (s.x1, s.y1), (int(255 * b), int(255 * g), int(255 * r)), -1)
    for x, y, w, h in res.dots:
        cv2.circle(vis, (x + w // 2, y + h // 2), w // 2 + 3, (0, 0, 0), 2)
    for (tx, ty), (sx, sy) in res.arrows:
        cv2.arrowedLine(vis, (int(sx), int(sy)), (int(tx), int(ty)), (255, 0, 255), 3, tipLength=0.3)
    floating = {id(p) for p in res.floating}
    for p in res.pins:
        cv2.circle(vis, (int(p.x), int(p.y)), 6, (0, 0, 255) if id(p) in floating else (0, 160, 0), -1)
    return cv2.resize(vis, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


# --- correction store --------------------------------------------------------
#
# corrections/<crop>.yaml holds human fixes to an extracted IR. Items match a
# detection by symbol type and page position (the "?<i>" names are not stable
# across detector changes), so they keep applying on re-runs. Status:
#   proposed  - read off the scan by the assistant, not yet checked by a human
#   confirmed - checked by a human
# Only confirmed items apply unless include_proposed is set.

import base64  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

import yaml  # noqa: E402

from .ir import IR  # noqa: E402

from . import project  # noqa: E402

CORRECTIONS: Path | None = None  # override (tests); default: the project's corrections/
MATCH_DIST = 40  # px: max distance between a correction's `at` and a detection centre
STATUSES = ("proposed", "confirmed")


def _centre(bbox) -> tuple[float, float]:
    x, y, w, h = bbox
    return x + w / 2, y + h / 2


def load_corrections(crop: str, section: str = "components", _seen: frozenset = frozenset()) -> list[dict]:
    """Items of one section of corrections/<crop>.yaml: "components" or "annotations".
    `include: [other]` pulls in another crop's items first (they match by page
    position, so a wider crop reuses a narrower one's checked fixes)."""
    path = (CORRECTIONS or project.current().corrections_dir) / f"{crop}.yaml"
    if not path.exists() or crop in _seen:
        return []
    data = yaml.safe_load(path.read_text()) or {}
    items = [it for inc in data.get("include", []) for it in load_corrections(inc, section, _seen | {crop})]
    items += data.get(section, []) or []
    for it in items:
        if it.get("status") not in STATUSES:
            raise ValueError(f"{path}: item at {it.get('at')}: status must be one of {STATUSES}")
    return items


def _match(ir: IR, item: dict) -> int | None:
    if "page" in item and item["page"] != ir.meta.get("pdf_page"):
        return None  # same coordinates on another sheet are another part
    ax, ay = item["at"]
    best = None
    for k, c in enumerate(ir.components):
        if c.bbox is None or c.type != item["type"]:
            continue
        cx, cy = _centre(c.bbox)
        d = ((cx - ax) ** 2 + (cy - ay) ** 2) ** 0.5
        if d <= MATCH_DIST and (best is None or d < best[0]):
            best = (d, k)
    return best[1] if best else None


def apply_corrections(ir: IR, items: list[dict], include_proposed: bool = False) -> tuple[IR, list[str]]:
    """Apply correction items (ref/value overrides, deletions). Pure: returns a new IR
    and one log line per item, including those skipped or unmatched."""
    comps = list(ir.components)
    log, drop, applied = [], set(), 0
    for it in items:
        tag = f"{it['type']} at {it['at']}" + (f" p.{it['page']}" if "page" in it else "")
        if it["status"] == "proposed" and not include_proposed:
            log.append(f"skip  {tag}: proposed, not confirmed")
            continue
        k = _match(ir, it)
        if k is None:
            log.append(f"MISS  {tag}: no {it['type']} detected within {MATCH_DIST} px on PDF page {ir.meta.get('pdf_page')}")
            continue
        c = comps[k]
        if it.get("delete"):
            drop.add(k)
            log.append(f"del   {c.ref} ({tag})")
            applied += 1
            continue
        upd, what = {}, []
        for f in ("ref", "value"):
            if f in it and str(it[f]) != getattr(c, f):
                upd[f] = str(it[f])
                what.append(f"{f} {getattr(c, f)!r} -> {it[f]!r}")
        if "over_list" in it:  # keep this value against the parts list (bom)
            upd["value_over_list"] = " ".join(str(it["over_list"]).split())
            what.append("value kept over the parts list")
        note = f"corrected ({it['status']}): {', '.join(what)}" if what else None
        if note:
            upd["note"] = f"{c.note}; {note}" if c.note else note
            upd["confidence"] = 1.0 if it["status"] == "confirmed" else c.confidence
        comps[k] = c.model_copy(update=upd)
        applied += bool(what)
        log.append(f"set   {c.ref}: {', '.join(what) or 'already right'} [{it['status']}]")
    refs = [c.ref for k, c in enumerate(comps) if k not in drop]
    dup = sorted({r for r in refs if refs.count(r) > 1})
    if dup:
        log.append(f"WARN  duplicate refs after corrections: {dup}")
    meta = dict(ir.meta, corrections=applied, corrections_proposed=include_proposed)
    kept = [c for k, c in enumerate(comps) if k not in drop]
    return ir.model_copy(update={"components": kept, "nets": _renamed_nets(ir, kept), "meta": meta}), log


def _renamed_nets(ir: IR, comps: list) -> list:
    """Net pin lists rebuilt from the corrected parts (ref renames, deletions)."""
    members: dict[str, list[str]] = {}
    for c in comps:
        for p, n in c.pins.items():
            members.setdefault(n, []).append(f"{c.ref}.{p}")
    for j in ir.connectors:
        for p, n in j.pins.items():
            members.setdefault(n, []).append(f"{j.ref}.{p}")
    return [n.model_copy(update={"pins": members.get(n.name, [])}) for n in ir.nets]


def apply_annotation_corrections(ir: IR, items: list[dict], include_proposed: bool = False) -> tuple[IR, list[str]]:
    """Re-attach printed voltage labels: each item finds the label whose box centre is
    within MATCH_DIST of `at` and moves it to the net of `net_pin` ("QM36.E") and/or
    sets its value (`set_voltage`), or drops it with `delete: true`."""
    pins = {f"{c.ref}.{p}": n for c in ir.components for p, n in c.pins.items()}
    pins.update({f"{j.ref}.{p}": n for j in ir.connectors for p, n in j.pins.items()})
    annots = list(ir.annotations)
    log, drop = [], set()
    for it in items:
        tag = f"{it.get('voltage', '?')} V label at {it['at']}"
        if it["status"] == "proposed" and not include_proposed:
            log.append(f"skip  {tag}: proposed, not confirmed")
            continue
        if "page" in it and it["page"] != ir.meta.get("pdf_page"):
            log.append(f"MISS  {tag}: other page")
            continue
        ax, ay = it["at"]
        cands = [(np.hypot(_centre(a.bbox)[0] - ax, _centre(a.bbox)[1] - ay), k)
                 for k, a in enumerate(annots) if a.bbox and k not in drop]
        cands = [c for c in cands if c[0] <= MATCH_DIST]
        if not cands:
            log.append(f"MISS  {tag}: no printed voltage there")
            continue
        k = min(cands)[1]
        a = annots[k]
        if it.get("delete"):
            drop.add(k)
            log.append(f"del   {a.voltage} V on {a.net}")
            continue
        upd, what = {}, []
        if "set_voltage" in it:  # OCR misread the value ("10.03V": pin number "1" fused on)
            upd["voltage"] = float(it["set_voltage"])
            what.append(f"value {a.voltage} -> {upd['voltage']}")
        if "net_pin" in it:
            if it["net_pin"] not in pins:
                log.append(f"MISS  {tag}: pin {it['net_pin']} not in the IR")
                continue
            upd["net"] = pins[it["net_pin"]]
            what.append(f"{a.net} -> {upd['net']} ({it['net_pin']})")
        annots[k] = a.model_copy(update={**upd, "note": f"corrected ({it['status']}): {', '.join(what)}"})
        log.append(f"set   {a.voltage} V: {', '.join(what)} [{it['status']}]")
    return ir.model_copy(update={"annotations": [a for k, a in enumerate(annots) if k not in drop]}), log


# --- review sheet ------------------------------------------------------------

def needs_review(ir: IR, issues, floating_refs: set[str] = frozenset(), min_conf: float = 0.6) -> dict[str, list[str]]:
    """ref -> reasons it needs a human look."""
    out: dict[str, list[str]] = {}
    for c in ir.components:
        if c.ref.startswith("?"):
            out.setdefault(c.ref, []).append("no ref read on the schematic")
        elif c.confidence < min_conf and "corrected" not in (c.note or ""):
            out.setdefault(c.ref, []).append(f"low confidence {c.confidence}")
        if c.ref in floating_refs:
            out.setdefault(c.ref, []).append("pin touches no wire")
    for i in issues:
        if not (i.ref.startswith("?") and "no ref read" in i.msg):
            out.setdefault(i.ref, []).append(f"bom {i.level}: {i.msg}")
    return out


def _thumb(page: np.ndarray, bbox, margin: int = 170, size: int = 320) -> str:
    x, y, w, h = bbox
    H, W = page.shape
    x0, y0, x1, y1 = max(0, x - margin), max(0, y - margin), min(W, x + w + margin), min(H, y + h + margin)
    t = cv2.cvtColor(page[y0:y1, x0:x1], cv2.COLOR_GRAY2BGR)
    cv2.rectangle(t, (x - x0, y - y0), (x - x0 + w, y - y0 + h), (40, 40, 220), 4)
    t = cv2.resize(t, (size, size * t.shape[0] // t.shape[1]), interpolation=cv2.INTER_AREA)
    return base64.b64encode(cv2.imencode(".png", t)[1].tobytes()).decode()


def _label_cards(page: np.ndarray, ir: IR, items: list[dict]) -> list[str]:
    """One card per printed voltage on a net whose printed voltages disagree."""
    import html

    from .erc import conflicting_voltages

    bad = conflicting_voltages(ir)
    members: dict[str, list[str]] = {}
    for c in ir.components:
        for p, n in c.pins.items():
            members.setdefault(n, []).append(f"{c.ref}.{p}")
    cards = []
    for a in ir.annotations:
        if a.net not in bad or not a.bbox:
            continue
        cx, cy = _centre(a.bbox)
        it = next((i for i in items if np.hypot(i["at"][0] - cx, i["at"][1] - cy) <= MATCH_DIST), None)
        if it:
            fix = "<b>delete</b>" if it.get("delete") else "; ".join(
                ([f"move to the net of <b>{html.escape(it['net_pin'])}</b>"] if "net_pin" in it else [])
                + ([f"value <b>{it['set_voltage']} V</b>"] if "set_voltage" in it else []))
            corr = (f'<div class="fix {it["status"]}"><span class="badge">{it["status"]}</span> {fix}'
                    f'<div class="why">{html.escape(it.get("why", ""))}</div></div>')
        else:
            corr = '<div class="fix none"><span class="badge">no correction on file</span></div>'
        net_pins = ", ".join(sorted(members.get(a.net, []))[:8])
        cards.append(
            f'<article><img alt="{a.voltage} V" src="data:image/png;base64,{_thumb(page, a.bbox, margin=220)}">'
            f'<div class="body"><h2>{a.voltage:+g} V <small>label at [{cx:.0f}, {cy:.0f}] · attached by {html.escape(a.note or "?")}</small></h2>'
            f'<ul><li>net {html.escape(a.net)} carries printed {bad[a.net]}: they cannot all be right</li>'
            f'<li>net pins: {html.escape(net_pins)}</li></ul>{corr}</div></article>')
    return cards


def review_sheet(page: np.ndarray, ir: IR, reasons: dict[str, list[str]], items: list[dict], title: str,
                 label_items: list[dict] = ()) -> str:
    """Self-contained HTML page: one card per component that needs a look or carries a
    correction. Each card shows the scan around the part, what was extracted, why it is
    listed and the correction on file (with its status)."""
    import html

    by_pos = {}
    for it in items:
        k = _match(ir, it)
        if k is not None:
            by_pos[k] = it
    cards = []
    for k, c in enumerate(ir.components):
        if c.ref not in reasons and k not in by_pos:
            continue
        it = by_pos.get(k)
        if it:
            fix = ", ".join(f"{f} = <b>{html.escape(str(it[f]))}</b>" for f in ("ref", "value") if f in it)
            if it.get("delete"):
                fix = "<b>delete</b> (false detection)"
            corr = (f'<div class="fix {it["status"]}"><span class="badge">{it["status"]}</span> {fix}'
                    f'<div class="why">{html.escape(it.get("why", ""))}</div></div>')
        else:
            corr = '<div class="fix none"><span class="badge">no correction on file</span></div>'
        rs = "".join(f"<li>{html.escape(r)}</li>" for r in reasons.get(c.ref, [])) or "<li>resolved by the correction</li>"
        cx, cy = _centre(c.bbox)
        cards.append(
            f'<article><img alt="{html.escape(c.ref)}" src="data:image/png;base64,{_thumb(page, c.bbox)}">'
            f'<div class="body"><h2>{html.escape(c.ref)} <small>{c.type} · value {html.escape(c.value or "—")}'
            f' · at [{cx:.0f}, {cy:.0f}]</small></h2><ul>{rs}</ul>{corr}</div></article>')
    labels = _label_cards(page, ir, list(label_items))
    if labels:
        cards.append('<h1 style="margin-top:28px">Printed voltages on the wrong wire?</h1>')
        cards += labels
    items = list(items) + list(label_items)
    n_prop = sum(it["status"] == "proposed" for it in items)
    n_conf = sum(it["status"] == "confirmed" for it in items)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root {{ --bg:#f6f6f3; --card:#fff; --ink:#1d1d1b; --muted:#6b6b66; --line:#deded8;
        --prop:#b26b00; --prop-bg:#fff4e0; --conf:#1f7a3a; --conf-bg:#e6f4ea; --none:#a33; --none-bg:#fbeaea; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#18181a; --card:#232326; --ink:#ececea; --muted:#a0a09a;
        --line:#3a3a3e; --prop:#f0b45a; --prop-bg:#3a2c14; --conf:#7fd39a; --conf-bg:#173022; --none:#f08a8a; --none-bg:#3a1c1c; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 system-ui, sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} .lead {{ color:var(--muted); margin:0 0 20px; }}
article {{ display:flex; gap:16px; background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:12px; margin-bottom:12px; flex-wrap:wrap; }}
img {{ width:320px; max-width:100%; height:auto; border-radius:6px; background:#fff; }}
.body {{ flex:1; min-width:240px; }} h2 {{ font-size:18px; margin:2px 0 6px; }}
h2 small {{ font-weight:400; color:var(--muted); font-size:13px; }} ul {{ margin:0 0 10px; padding-left:18px; }}
.fix {{ border-radius:8px; padding:8px 10px; }} .why {{ color:var(--muted); font-size:13px; margin-top:4px; }}
.badge {{ font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.04em; margin-right:6px; }}
.proposed {{ background:var(--prop-bg); }} .proposed .badge {{ color:var(--prop); }}
.confirmed {{ background:var(--conf-bg); }} .confirmed .badge {{ color:var(--conf); }}
.none {{ background:var(--none-bg); }} .none .badge {{ color:var(--none); }}
code {{ font-size:13px; }}
</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p class="lead">{len(cards)} parts to check · {n_prop} proposed, {n_conf} confirmed corrections.
To accept a proposal, set its <code>status</code> to <code>confirmed</code> in <code>corrections/{html.escape(str(ir.meta.get('crop', '')))}.yaml</code>.</p>
{''.join(cards)}
</main></body></html>
"""
