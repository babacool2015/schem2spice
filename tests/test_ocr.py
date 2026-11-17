from pathlib import Path

import pytest

from schem2spice import diff, ir as irmod
from schem2spice.ocr import Text, classify
from schem2spice.project import current as _project  # noqa: E402

P = _project()  # the reference project (tests/conftest.py)

PAGE = P.pages_dir / "page-04.png"
GOLDEN = P.root / "golden" / "pm11" / "ir.json"
PM11 = (6950, 1300, 8200, 2450)
SCOPE = (6250, 650, 9150, 2500)

# Labels the OCR does not read yet in the full scope. Shrink as OCR improves.
KNOWN_REF_MISSES = {"R529-1", "R501", "R521", "C503"}  # C507/C509 read since tiled OCR (ocr/v4)


def _t(s):
    return classify(Text(s, 0, 0, 10, 10, 1.0, False))


@pytest.mark.parametrize("raw,kind,norm", [
    ("0M16", "ref", "QM16"), ("QN13", "ref", "QM13"), ("0501", "ref", "Q501"),
    ("-13.1V", "voltage", "-13.1"), ("0.17V", "voltage", "0.17"), ("33K", "value", "33k"),
    ("2SK369", "value", "2SK369"), ("R529-2", "ref", "R529-2"),
])
def test_classify(raw, kind, norm):
    t = _t(raw)
    assert (t.kind, t.norm) == (kind, norm)


@pytest.fixture(scope="module")
def page():
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("rapidocr")
    if not PAGE.exists():
        pytest.skip("run `python -m schem2spice.ingest` first")
    return cv2.imread(str(PAGE), cv2.IMREAD_GRAYSCALE)


def test_pm11_module_text_complete(page):
    from schem2spice import ocr
    x0, y0, x1, y1 = PM11
    d = diff.diff_text(irmod.load(GOLDEN), ocr.read_text(page[y0:y1, x0:x1]), PM11, offset=(x0, y0))
    assert not d.refs_missed and not d.volts_missed, d.report()


def test_full_scope_text(page):
    from schem2spice import ocr
    x0, y0, x1, y1 = SCOPE
    d = diff.diff_text(irmod.load(GOLDEN), ocr.read_text(page[y0:y1, x0:x1]), SCOPE, offset=(x0, y0))
    assert not d.volts_missed, d.report()
    assert set(d.refs_missed) == KNOWN_REF_MISSES, d.report()


def test_tiles_stay_under_rapidocr_limit():
    from schem2spice.ocr import TILE, TILE_OVERLAP, _tiles
    assert _tiles(900) == [0]
    starts = _tiles(3675)  # p401l at scale 0.75
    assert starts[0] == 0 and starts[-1] + TILE == 3675
    assert all(b - a <= TILE - TILE_OVERLAP for a, b in zip(starts, starts[1:]))


def test_fused_labels_are_split_into_words():
    from schem2spice.ocr import Text, _split_words, classify
    words = _split_words(classify(Text("-0.04V 10/50V", 4540, 1400, 304, 81, 0.9, False)))
    assert [(w.kind, w.norm) for w in words][0] == ("voltage", "-0.04")
    assert words[0].x == 4540 and words[1].x > words[0].x + words[0].w - 2
    assert classify(Text("0V", 0, 0, 10, 10, 1.0, False)).kind == "voltage"
    t = classify(Text("F0.04V", 0, 0, 10, 10, 1.0, False))  # "|-" fused by a wire end
    assert (t.kind, t.norm) == ("voltage", "-0.04")
    assert classify(Text("25V", 0, 0, 10, 10, 1.0, False)).kind != "voltage"  # capacitor rating
