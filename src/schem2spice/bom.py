"""Stage 9: parse the electrical parts list and merge it with the IR values.

The parts list (PDF pages 13-17) is a scanned table: REF. DESIG. | PART NO. |
DESCRIPTION, one or two tables per page, grouped in board sections
("P501-RESISTORS", "PM11-SEMICONDUCTORS"...). Three kinds of rows:

- listed:  "QM11  4822 130 42839  F. E. T. 2SK369 (BL)"
- range:   "DM11 / ~ / DM15" stacked in the ref column, with the part number and
           description on the middle line (OCR reads the "~" as a stray "2")
- common:  "R***  Carbon Film Fixed Resistor ±5% 1/6W: R501~R508, R523, ..."
           -> the value is only on the schematic

`apply` gives every IR component its value from the parts list when the list has
one (the typed list beats schematic OCR), keeps the schematic value for common
parts, and reports anything it cannot settle as an `Issue`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .ir import IR

from . import project

PARSER = "partslist/v2"  # v2: refs with the safety mark
LINE_TOL = 12  # px at 300 ppi: tokens whose y-centers differ less share a table line
RANGE_SPAN = 110  # px: max distance from a range's middle line to its closing ref

_REF = re.compile(r"^[A-Z]{1,2}\d{2,4}$")
_COMMON = re.compile(r"^[RCA]\s*\*")  # "R***", read as "R**", "R*", "C**_", "A***" (P851)
_SECTION = re.compile(r"^(P[A-Z]?\d{2,3})-([A-Z].*)$")  # "PM11-RESISTORS (COMMON)"
_REF_ITEM = re.compile(r"([A-Z]{1,2}\d{2,4})(?:\s*[~～]\s*([A-Z]{1,2}\d{2,4}))?")
_VARIANT = re.compile(r"\[/?([0-9A-Z/]+)\]")

_RES = re.compile(r"(\d+(?:\.\d+)?)\s*([kKM]?)\s*Ω")
_CAP = re.compile(r"(\d+(?:\.\d+)?)\s*([pnuμµ])F")
_IND = re.compile(r"(\d+(?:\.\d+)?)\s*([muμµ]?)H\b")
# Semiconductor rows: "<kind> <part> (<grades>)"; OCR garbles some kind words.
_SEMI = re.compile(r"(?:Diode|Ze\w+er|Tra\w*sistor(?:, Digital)?|IC|F\. ?E\. ?T\.|L\. ?E\. ?D\.|Photo Unit)\s+"
                   r"([A-Z0-9][A-Z0-9.\-]*\d[A-Z0-9.\-]*)\s*(?:\(([^)]*)\))?")
_IC_SUFFIX = re.compile(r"^(NJM\d{4})[A-Z]+$")  # NJM2068DD -> NJM2068 (package code)
_TOL = re.compile(r"±\s*(\d+(?:\.\d+)?)\s*%")

_SEMI_TYPES = {"diode", "npn", "pnp", "njfet", "opamp", "led", "darlington", "photocoupler"}
_VALUELESS = {"switch_spdt", "rotary_pole"}  # parts whose behaviour is a testbench parameter, not a value
_CAP_TYPES = {"capacitor", "capacitor_polarized"}
_RES_TYPES = {"resistor", "potentiometer", "resistor_ct"}


@dataclass
class PartEntry:
    refs: list[str]
    part_no: str | None
    description: str
    board: str | None
    section: str | None
    common: bool
    page: int
    y: int
    variants: list[str] = field(default_factory=list)

    @property
    def values(self) -> list[str]:
        """Values in IR form: '100', '3.3M', '0.1u', '2SK369-BL'. Several for dual pots."""
        d = self.description
        if m := _SEMI.search(d):
            part = _IC_SUFFIX.sub(r"\1", m.group(1))
            # Ranks only for 2S transistors; "(RED)" on an LED is a colour.
            grades = [g for g in re.split(r"[\s,./]+", m.group(2) or "") if g] if part.startswith("2S") else []
            return [f"{part}-{grades[0]}" if len(grades) == 1 else part]
        out = [normalize_value(a + b + "F") for a, b in _CAP.findall(d)]
        out += [normalize_value(a + b + "H") for a, b in _IND.findall(d)]
        out += [normalize_value(a + b) for a, b in _RES.findall(d)]
        return list(dict.fromkeys(v for v in out if v))

    @property
    def tolerance(self) -> str | None:
        m = _TOL.search(self.description)
        return f"{m.group(1)}%" if m else None


def normalize_value(s: str) -> str | None:
    """Schematic or parts-list value -> IR form. '4k7' -> '4.7k', '100 Ω' -> '100',
    '250KΩ' -> '250k', '0.1μF' -> '0.1u', '100pF' -> '100p'. None if unparseable."""
    v = s.strip().replace(" ", "").replace("Ω", "").replace("ohm", "")
    v = re.sub(r"[μµ]", "u", v)
    v = re.sub(r"(?<=[pnu])F$", "", v)
    v = re.sub(r"(?<=\d)([mu]?)H$", r"\1", v)  # 320mH -> 320m (SPICE milli)
    if m := re.fullmatch(r"(\d+)([kKMRpnu])(\d+)", v):  # 4k7, 4R7, 2u2
        a, mult, b = m.groups()
        v = f"{a}.{b}{'' if mult == 'R' else mult}"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKMmpnu]?)", v)
    if not m:
        return None
    num, mult = m.groups()
    if num.startswith("0") and "." not in num and len(num) > 1:
        return None  # "000", "07": OCR debris, not a value
    return num + {"K": "k"}.get(mult, mult)


# --- parsing -----------------------------------------------------------------

# Column header words that locate each parts-list table (override in project.yaml:
# parts_list.headers.{ref,part_number,description}).
HEADERS = {"ref": ["REF.", "DESIG."], "part_number": ["PART NO."], "description": ["DESCRIPTION"]}


def _tables(texts) -> list[tuple[int, int, int, int]]:
    """(ref_x, partno_x, desc_x, header_bottom) per table, from its header words."""
    h = {**HEADERS, **((p := project.optional()) and p.cfg.get("parts_list", {}).get("headers") or {})}
    words = {k: {w.replace(" ", "").upper() for w in v} for k, v in h.items()}
    parts = [t for t in texts if t.text.replace(" ", "").upper() in words["part_number"]]
    descs = [t for t in texts if t.text.replace(" ", "").upper() in words["description"]]
    refs = [t for t in texts if t.text.replace(" ", "").upper() in words["ref"]]
    out = []
    for p in parts:
        left = [r.x for r in refs if r.x < p.x and abs(r.y - p.y) < 80]
        right = [d.x for d in descs if d.x > p.x and abs(d.y - p.y) < 80]
        if left and right:
            out.append((max(left), p.x, min(right), p.y + p.h))
    return sorted(out)


def _lines(tokens):
    lines: list[list] = []
    for t in sorted(tokens, key=lambda t: t.center[1]):
        if lines and abs(t.center[1] - lines[-1][0].center[1]) < LINE_TOL:
            lines[-1].append(t)
        else:
            lines.append([t])
    return lines


def _expand(items: list[tuple[str, str]]) -> list[str]:
    refs = []
    for a, b in items:
        if not b:
            refs.append(a)
            continue
        pa, na = re.match(r"([A-Z]+)(\d+)", a).groups()
        pb, nb = re.match(r"([A-Z]+)(\d+)", b).groups()
        if pa != pb or int(nb) < int(na):
            refs += [a, b]
            continue
        refs += [f"{pa}{n:0{len(na)}d}" for n in range(int(na), int(nb) + 1)]
    return refs


def _ref(s: str) -> str | None:
    # A safety-critical part's ref carries the mark: "▲ G851", "▲D858", read "AD876" too
    # (no ref in this manual starts with A).
    s = re.sub(r"^(?:▲\s*|A(?=[A-Z]{1,2}\d{2,4}$))", "", s.strip())
    return re.sub(r"^([A-Z]{1,2})0(\d{3})$", r"\1\2", s) if _REF.match(s) else None  # "C0447" -> "C447"


def parse_page(texts, page: int, ctx: tuple[str | None, str | None] = (None, None),
               part_number: re.Pattern | None = None) -> tuple[list[PartEntry], tuple[str | None, str | None]]:
    """Parse one page. `ctx` is the (board, section) open at the end of the previous
    table, since sections run on across columns and pages. Rows whose ref OCR missed
    get `refs == []` and are kept for the report, never indexed."""
    partno = part_number or project.current().part_number
    if partno is None:
        raise ValueError("project.yaml: parts_list.part_number (the part-number column's pattern) is missing")
    tables = _tables(texts)
    entries: list[PartEntry] = []
    board, section = ctx
    for i, (ref_x, part_x, desc_x, top) in enumerate(tables):
        right = tables[i + 1][0] - 40 if i + 1 < len(tables) else 10**9
        toks = [t for t in texts if ref_x - 60 <= t.x < right and t.y > top + 5]
        cur: PartEntry | None = None
        pending: str | None = None  # range start waiting for its part-number line
        open_range: tuple[PartEntry, str, float] | None = None
        for line in _lines(toks):
            ref = next((r for t in line if t.x < part_x - 20 and (r := _ref(t.text))), None)
            common = any(t.x < part_x - 20 and _COMMON.match(t.text.strip()) for t in line)
            pno = next((re.sub(r"\s+", " ", t.text.strip()) for t in line if partno.match(t.text.strip())), None)
            desc = " ".join(t.text.strip() for t in sorted(line, key=lambda t: t.x) if t.x >= desc_x - 40)
            y = int(line[0].center[1])
            if m := _SECTION.match(desc):
                board, section = m.group(1), m.group(2)
                cur, pending, open_range = None, None, None
                continue
            if common:
                cur = PartEntry([], None, desc, board, section, True, page, y)
                entries.append(cur)
                continue
            if ref and not pno and not desc:
                if open_range and y - open_range[2] < RANGE_SPAN:
                    e, start, _ = open_range
                    e.refs = _expand([(start, ref)])
                    open_range = None
                else:
                    pending = ref
                continue
            if pno or (ref and desc):
                if ref:
                    cur = PartEntry([ref], pno, desc, board, section, False, page, y)
                elif pending:
                    cur = PartEntry([pending], pno, desc, board, section, False, page, y)
                    open_range = (cur, pending, y)
                elif cur and not cur.common and "[" in cur.description + desc:
                    cur = PartEntry(list(cur.refs), pno, desc, board, section, False, page, y)  # variant line
                else:
                    cur = PartEntry([], pno, desc, board, section, False, page, y)  # ref not read
                pending = None
                entries.append(cur)
                continue
            if desc and cur:
                cur.description += " " + desc
    for e in entries:
        e.variants = [v for g in _VARIANT.findall(e.description) for v in g.split("/") if v]
        if e.common:
            listing = e.description.split(":", 1)[-1]
            e.refs = _expand(_REF_ITEM.findall(_VARIANT.sub("", listing)))
    return entries, (board, section)


def load_parts_list(pages: list[int] | None = None) -> list[PartEntry]:
    """Parts-list rows from the project's parts-list pages (project.yaml `parts_list`)."""
    from . import ocr

    proj = project.current()
    pages = pages if pages is not None else proj.parts_pages
    if not pages:
        raise ValueError(f"{proj.root}/project.yaml: no parts_list.pages")
    entries: list[PartEntry] = []
    ctx: tuple[str | None, str | None] = (None, None)
    for p in pages:
        found, ctx = parse_page(ocr.read_page(proj.read_page(p)), p, ctx)
        entries += found
    return entries


def index(entries: list[PartEntry]) -> dict[str, list[PartEntry]]:
    idx: dict[str, list[PartEntry]] = {}
    for e in entries:
        for r in e.refs:
            idx.setdefault(r, []).append(e)
    return idx


# --- merge -------------------------------------------------------------------

@dataclass
class Issue:
    ref: str
    level: str  # error: cannot simulate as is | warn: value changed or doubtful
    msg: str

    def __str__(self) -> str:
        return f"{self.level:5} {self.ref}: {self.msg}"


def _kind_fits(typ: str, e: PartEntry) -> bool:
    d = e.description
    if typ in _SEMI_TYPES:
        return bool(_SEMI.search(d))
    if typ in _CAP_TYPES:
        return bool(_CAP.search(d)) or (e.common and "Capacitor" in d)
    if typ in _RES_TYPES:
        return bool(_RES.search(d)) or (e.common and "Resistor" in d)
    return True


def _schematic_value(c) -> str | None:
    """The component's schematic value in IR form. Japanese schematics print bare
    capacitor numbers: a decimal is in uF ("0.1"), an integer in pF ("47"), except
    electrolytics, which are always uF ("10")."""
    if not c.value:
        return None
    if c.type in _SEMI_TYPES:
        return c.value if re.match(r"^(2S|1SS|NJM)", c.value) else None
    v = normalize_value(c.value)
    if v and c.type in _CAP_TYPES and re.fullmatch(r"[\d.]+", v):
        v += "u" if "." in v or c.type == "capacitor_polarized" else "p"
    return v


def _agrees(schem: str, listed: str) -> bool:
    """Schematic value consistent with the list: equal, or the list adds the rank
    the schematic leaves out ("2SK369" vs "2SK369-BL")."""
    return schem == listed or listed.startswith(schem + "-")


def apply(ir: IR, entries: list[PartEntry], variant: str | None = None) -> tuple[IR, list[Issue]]:
    """Fill component values/tolerances from the parts list. Pure: returns a new IR."""
    idx = index(entries)
    issues: list[Issue] = []
    comps = []
    for c in ir.components:
        upd: dict = {}
        base = re.sub(r"-\d$", "", c.ref)  # R529-1 -> R529 (ganged sections share one row)
        schem = _schematic_value(c)
        listed = found = idx.get(base, [])
        if variant:
            found = [e for e in found if not e.variants or variant in e.variants]
        if listed and not found:
            issues.append(Issue(c.ref, "error", f"not fitted in variant {variant}: rows only for "
                                                f"{sorted({v for e in listed for v in e.variants})}; cut it or fit a jumper"))
            comps.append(c)
            continue
        found = [e for e in found if _kind_fits(c.type, e)] or found

        if not found:
            if schem:
                upd["value"] = schem
            msg = "no ref read on the schematic" if c.ref.startswith("?") else "not in the parts list"
            issues.append(Issue(c.ref, "error" if not schem else "warn",
                                f"{msg}; value {'kept from schematic: ' + schem if schem else 'unknown'}"))
        else:
            e = found[0]
            if len({tuple(f.values) for f in found}) > 1:
                issues.append(Issue(c.ref, "error",
                                    f"value depends on the model variant {[f.variants for f in found]}: "
                                    f"{[f.values for f in found]}; pass a variant"))
            elif not _kind_fits(c.type, e) and not e.values and schem:
                upd["value"] = schem  # P701 R752 "2immigming": OCR lost the row, not a type clash
                issues.append(Issue(c.ref, "warn", f"parts list row unreadable ({e.description!r}); "
                                                   f"value kept from schematic: {schem}"))
            elif not _kind_fits(c.type, e):
                issues.append(Issue(c.ref, "error", f"symbol is a {c.type} but the parts list says {e.description!r}"))
            elif c.type in _VALUELESS:
                pass
            elif c.type == "rc_snubber":  # "Comp. 0.47uF+6.8 ohm" -> "0.47u+6.8"
                cm, rm = _CAP.search(e.description), _RES.search(e.description)
                if cm and rm:
                    upd["value"] = (normalize_value(cm.group(0)) or "") + "+" + (normalize_value(rm.group(0)) or "")
                    if schem and schem != upd["value"]:
                        issues.append(Issue(c.ref, "warn", f"schematic read {c.value!r}, parts list {upd['value']!r}: using the list"))
            elif e.common or not e.values:
                if schem:
                    upd["value"] = schem
                elif e.common:
                    issues.append(Issue(c.ref, "error",
                                        f"common part ({e.board}): value only on the schematic, read {c.value!r}"))
                else:
                    issues.append(Issue(c.ref, "error", f"no value in the parts list row {e.description!r} "
                                                        f"nor on the schematic"))
            elif len(e.values) > 1:  # dual pot: 250k x2 + 2k x2
                if schem in e.values:
                    upd["value"] = schem
                else:
                    issues.append(Issue(c.ref, "error", f"which section? parts list {e.values}, schematic {c.value!r}"))
            elif c.value_over_list and schem:
                upd["value"] = schem
                if not _agrees(schem, e.values[0]):
                    issues.append(Issue(c.ref, "warn", f"parts list says {e.values[0]!r}; keeping the schematic "
                                                       f"{schem!r} by correction: {c.value_over_list}"))
            else:
                v = e.values[0]
                upd["value"] = v
                if schem and not _agrees(schem, v):
                    issues.append(Issue(c.ref, "warn", f"schematic read {c.value!r}, parts list {v!r}: using the list"))
            if e.tolerance and c.type in _RES_TYPES | _CAP_TYPES:
                upd["tolerance"] = e.tolerance
        comps.append(c.model_copy(update=upd))
    meta = dict(ir.meta, bom=PARSER, bom_variant=variant)
    return ir.model_copy(update={"components": comps, "meta": meta}), issues


def report(ir: IR, issues: list[Issue]) -> str:
    rows = ["| Ref | Type | Value | Tol |", "|---|---|---|---|"]
    rows += [f"| {c.ref} | {c.type} | {c.value or '?'} | {c.tolerance or ''} |"
             for c in sorted(ir.components, key=lambda c: c.ref)]
    errs = sum(i.level == "error" for i in issues)
    head = f"# BOM\n\n{len(ir.components)} parts, {errs} errors, {len(issues) - errs} warnings\n"
    body = "\n".join(f"- {i}" for i in issues) or "- none"
    return head + "\n## Issues\n\n" + body + "\n\n## Parts\n\n" + "\n".join(rows) + "\n"
