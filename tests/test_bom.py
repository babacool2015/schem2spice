from pathlib import Path

import pytest

from schem2spice import bom, ir as irmod
from schem2spice.ocr import Text
from schem2spice.project import current as _project  # noqa: E402

P = _project()  # the reference project (tests/conftest.py)

GOLDEN = P.root / "golden" / "pm11" / "ir.json"


@pytest.mark.parametrize("raw, want", [
    ("4k7", "4.7k"), ("4R7", "4.7"), ("100 Ω", "100"), ("250KΩ", "250k"), ("3.3MΩ", "3.3M"),
    ("0.1μF", "0.1u"), ("100pF", "100p"), ("2u2", "2.2u"), ("000", None), ("x", None),
])
def test_normalize_value(raw, want):
    assert bom.normalize_value(raw) == want


def _t(text, x, y):
    return Text(text, x, y, 20 * len(text), 40, 1.0, False)


def test_parse_ranges_variants_and_common_lists():
    """Synthetic table in the layout of PDF page 14/15 (300 ppi, ~33 px line pitch)."""
    texts = [_t("REF.", 100, 100), _t("PART NO.", 300, 120), _t("DESCRIPTION", 600, 120),
             _t("P501-RESISTORS", 600, 200),
             _t("R509", 100, 260), _t("4822 050 23308", 260, 290), _t("3.3MΩ ±5% 1/6W", 640, 290),
             _t("2", 120, 292), _t("R514", 100, 322),
             _t("C401", 100, 380), _t("4822 121 42712", 260, 380), _t("Film 100pF ±5% [/20B/21B]", 600, 380),
             _t("4822 121 42708", 260, 413), _t("Film 330pF ±5% [/22B]", 600, 413),
             _t("P501-RESISTORS (COMMON)", 600, 480),
             _t("R***", 100, 540), _t("Carbon Film Fixed Resistor, ±5%", 600, 540),
             _t("1/6W:", 600, 573), _t("R501~R503, R523,", 600, 606), _t("R527", 600, 639)]
    entries, ctx = bom.parse_page(texts, 15)
    assert ctx == ("P501", "RESISTORS (COMMON)")
    rng, c1, c2, common = entries
    assert rng.refs == [f"R{n}" for n in range(509, 515)] and rng.values == ["3.3M"] and rng.tolerance == "5%"
    assert (c1.refs, c1.values, c1.variants) == (["C401"], ["100p"], ["20B", "21B"])
    assert (c2.refs, c2.values, c2.variants) == (["C401"], ["330p"], ["22B"])
    assert common.common and common.refs == ["R501", "R502", "R503", "R523", "R527"]


def _entry(refs, desc, common=False, variants=()):
    return bom.PartEntry(refs, None, desc, "P501", None, common, 15, 0, list(variants))


def test_apply_rules():
    entries = [_entry(["QM15"], "Transistor 2SA970 (GR, BL)"),
               _entry(["QM11"], "F. E. T. 2SK369 (BL)"),
               _entry(["C505"], "Film 0.1μF ±5%"),
               _entry(["RM13"], "Carbon Film Fixed Resistor, ±5% 1/6W: RM13", common=True),
               _entry(["R529"], "250KΩx 2+ 2KΩx 2 Variable"),
               _entry(["C401"], "Film 100pF ±5%", variants=["21B"]),
               _entry(["C401"], "Film 330pF ±5%", variants=["22B"])]
    comp = [irmod.Component(ref=r, type=t, value=v, pins={})
            for r, t, v in [("QM15", "pnp", "2SA9701"), ("QM11", "njfet", "2SK369"), ("C505", "capacitor", "0.1"),
                            ("RM13", "resistor", "68"), ("R529-1", "potentiometer", "2k"),
                            ("R529-2", "potentiometer", ""), ("C401", "capacitor", ""), ("?7", "resistor", "")]]
    out, issues = bom.apply(irmod.IR(components=comp), entries)
    got = {c.ref: c.value for c in out.components}
    assert got == {"QM15": "2SA970", "QM11": "2SK369-BL", "C505": "0.1u", "RM13": "68",
                   "R529-1": "2k", "R529-2": "", "C401": "", "?7": ""}
    assert {(i.ref, i.level) for i in issues} == {("QM15", "warn"), ("R529-2", "error"),
                                                  ("C401", "error"), ("?7", "error")}
    out, _ = bom.apply(irmod.IR(components=comp), entries, variant="22B")
    assert out.component("C401").value == "330p"


@pytest.fixture(scope="module")
def parts():
    pytest.importorskip("rapidocr")
    if not (P.pages_dir / "page-13.png").exists():
        pytest.skip("run `python -m schem2spice.ingest` first")
    return bom.load_parts_list()


def test_parts_list_rows_for_the_pilot(parts):
    idx = bom.index(parts)
    want = {"RM11": ["100"], "DM13": ["1SS176"], "QM11": ["2SK369-BL"], "QM14": ["2SA970"],
            "C503": ["47p"], "C509": ["10u"], "R511": ["3.3M"], "R521": ["10"], "R529": ["250k", "2k"],
            "Q501": ["2SC2705"], "Q503": ["2SA1145"], "D503": ["1SS176"], "Q401": ["NJM2068"],
            "C436": ["0.01u"], "C447": ["100p"]}
    assert {r: idx[r][0].values for r in want} == want
    assert idx["R523"][0].common and idx["RM16"][0].common


def test_golden_listed_values_come_from_the_parts_list(parts):
    """Blank every value in the golden IR: bom must restore all listed ones and flag
    exactly the common parts (value only on the schematic) and the dual pot."""
    g = irmod.load(GOLDEN)
    blank = g.model_copy(update={"components": [c.model_copy(update={"value": ""}) for c in g.components]})
    out, issues = bom.apply(blank, parts)
    idx = bom.index(parts)
    common = {c.ref for c in g.components if idx[c.ref.split("-")[0]][0].common}
    assert {i.ref for i in issues} == common | {"R529-1", "R529-2"}
    for c, gc in zip(out.components, g.components):
        assert c.tolerance == gc.tolerance, c.ref
        if c.ref not in common | {"R529-1", "R529-2"}:
            assert c.value == gc.value, c.ref
    # With values present the golden IR is unchanged and clean.
    same, issues = bom.apply(g, parts)
    assert not issues and [c.value for c in same.components] == [c.value for c in g.components]


def test_extracted_pm11_values_match_golden(parts):
    cv2 = pytest.importorskip("cv2")
    from schem2spice import detect, ocr, trace
    x0, y0, x1, y1 = (6950, 1300, 8200, 2450)
    page = cv2.imread(str(P.pages_dir / "page-04.png"), cv2.IMREAD_GRAYSCALE)
    crop = page[y0:y1, x0:x1]
    dets, texts = detect.detect_symbols(crop), ocr.read_text(crop)
    x = trace.to_ir(dets, texts, trace.trace(crop, dets, texts), (x0, y0))
    out, issues = bom.apply(x, parts)
    assert all(i.level == "warn" for i in issues)  # only "2SA9701" -> 2SA970
    g = irmod.load(GOLDEN)
    gold = {c.ref: (c.value, c.tolerance) for c in g.components}
    assert {c.ref: (c.value, c.tolerance) for c in out.components} == {r: gold[r] for r in {c.ref for c in out.components}}
    assert len(out.components) == 17
