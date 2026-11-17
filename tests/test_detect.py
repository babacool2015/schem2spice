import shutil
from pathlib import Path

import pytest

from schem2spice import diff, ir as irmod

cv2 = pytest.importorskip("cv2")
from schem2spice import detect  # noqa: E402
from schem2spice.project import current as _project  # noqa: E402

P = _project()  # the reference project (tests/conftest.py)

PAGE = P.pages_dir / "page-04.png"
GOLDEN = P.root / "golden" / "pm11" / "ir.json"
SCOPE = (6250, 650, 9150, 2500)  # x0, y0, x1, y1 on page 4: PM11 + P501 left channel

# Real symbols inside SCOPE that the golden IR does not cover (shared supply resistors).
OUT_OF_GOLDEN = [(7053, 721), (7053, 836)]  # R525, R526


@pytest.mark.skipif(not PAGE.exists(), reason="run `python -m schem2spice.ingest` first")
def test_detector_finds_every_golden_symbol():
    page = cv2.imread(str(PAGE), cv2.IMREAD_GRAYSCALE)
    x0, y0, x1, y1 = SCOPE
    found = detect.to_components(detect.detect_symbols(page[y0:y1, x0:x1]), (x0, y0))
    d = diff.diff_symbols(irmod.load(GOLDEN).components, found)
    assert not d.missed and not d.wrong_type, d.report()
    assert sorted(f.bbox[:2] for f in d.extra) == OUT_OF_GOLDEN, d.report()
