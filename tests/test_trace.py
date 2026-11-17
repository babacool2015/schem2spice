from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from schem2spice import detect, diff, ir as irmod, trace  # noqa: E402
from schem2spice.netlist import load_library  # noqa: E402
from schem2spice.project import current as _project  # noqa: E402

P = _project()  # the reference project (tests/conftest.py)

PAGE = P.pages_dir / "page-04.png"
GOLDEN = P.root / "golden" / "pm11" / "ir.json"
PM11 = (6950, 1300, 8200, 2450)
P501L = (6250, 650, 9300, 3200)  # crop: wires leave the golden scope and come back
SCOPE = (6250, 650, 9150, 2500)  # scored region (golden IR coverage)


@pytest.mark.parametrize("typ", [t for t, v in load_library().items() if "template_pins" in v])
def test_pin_transform_follows_template_variants(typ):
    """A pin mark placed on the template lands where _transform says, in all 8 variants."""
    tmpl = detect.load_templates()[typ]
    h, w = tmpl.shape
    for name, (px, py) in load_library()[typ]["template_pins"].items():
        marked = np.full_like(tmpl, 255)
        marked[int(py), int(px)] = 0
        for variant, img in detect._variants(marked):
            ys, xs = np.nonzero(img == 0)
            assert (xs[0], ys[0]) == tuple(int(v) for v in trace._transform(int(px), int(py), w, h, variant)), (name, variant)


def _canvas():
    return np.full((300, 300), 255, np.uint8)


def _nets(img):
    return len(trace.trace(img, [], []).nets)


def test_crossing_without_dot_stays_two_nets():
    img = _canvas()
    cv2.line(img, (20, 150), (280, 150), 0, 7)
    cv2.line(img, (150, 20), (150, 280), 0, 7)
    assert _nets(img) == 2


def test_crossing_with_dot_is_one_net():
    img = _canvas()
    cv2.line(img, (20, 150), (280, 150), 0, 7)
    cv2.line(img, (150, 20), (150, 280), 0, 7)
    cv2.circle(img, (150, 150), 15, 0, -1)
    assert _nets(img) == 1


def test_tee_and_corner_join():
    img = _canvas()
    cv2.line(img, (20, 150), (280, 150), 0, 7)  # T: vertical ends on the horizontal
    cv2.line(img, (150, 20), (150, 150), 0, 7)
    cv2.line(img, (20, 250), (100, 250), 0, 7)  # corner, separate net
    cv2.line(img, (100, 250), (100, 200), 0, 7)
    assert _nets(img) == 2


@pytest.fixture(scope="module")
def page():
    pytest.importorskip("rapidocr")
    if not PAGE.exists():
        pytest.skip("run `python -m schem2spice.ingest` first")
    return cv2.imread(str(PAGE), cv2.IMREAD_GRAYSCALE)


def _extract(page, rect):
    from schem2spice import ocr
    x0, y0, x1, y1 = rect
    crop = page[y0:y1, x0:x1]
    dets, texts = detect.detect_symbols(crop), ocr.read_text(crop)
    res = trace.trace(crop, dets, texts)
    return res, trace.to_ir(dets, texts, res, (x0, y0))


def test_pm11_extracted_ir_matches_golden(page):
    res, x = _extract(page, PM11)
    g = irmod.load(GOLDEN)
    d = diff.diff_nets(g, x, PM11)
    assert not d.pins_bad and not d.volts_bad and not d.unmatched, d.report()
    assert not res.floating
    # Every part and the connector carries its OCR'd ref.
    refs = {c.ref for c in x.components} | {j.ref for j in x.connectors}
    golden_refs = {c.ref for c in g.components
                   if PM11[0] <= diff._center(c)[0] < PM11[2] and PM11[1] <= diff._center(c)[1] < PM11[3]}
    assert golden_refs | {"JM11"} == refs


def test_p501_left_channel_nets_match_golden(page):
    res, x = _extract(page, P501L)
    d = diff.diff_nets(irmod.load(GOLDEN), x, SCOPE)
    assert not d.pins_bad and not d.volts_bad and not d.unmatched, d.report()
    assert not res.floating
    assert len(res.mated) == 1  # J504 | JM11
