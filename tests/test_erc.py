from pathlib import Path

from schem2spice import erc, ir as irmod
from schem2spice.project import current as _project  # noqa: E402

P = _project()  # the reference project (tests/conftest.py)

GOLDEN = P.root / "golden" / "pm11"


def test_golden_ir_passes_erc():
    ir = irmod.load(GOLDEN / "ir.json")
    assert erc.check(ir, driven={"vp", "vn", "lin", "lout"}) == []


def test_floating_pin_is_reported():
    ir = irmod.load(GOLDEN / "ir.json")
    ir.component("RM16").pins["2"] = "dangling"
    problems = erc.check(ir, driven={"vp", "vn", "lin", "lout"})
    assert any("dangling" in p for p in problems)
