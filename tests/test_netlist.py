from pathlib import Path

import pytest
import yaml

from schem2spice import ir as irmod
from schem2spice.netlist import emit, prepare, spice_value, to_ohms
from schem2spice.project import current as _project  # noqa: E402

P = _project()  # the reference project (tests/conftest.py)

GOLDEN = P.root / "golden" / "pm11"


def test_spice_value_mega_is_not_milli():
    assert spice_value("3.3M") == "3.3Meg"
    assert spice_value("2.2k") == "2.2k"
    assert spice_value("0.1u") == "0.1u"
    assert to_ohms("250k") == 250e3


def test_golden_netlist_has_every_component():
    ir = irmod.load(GOLDEN / "ir.json")
    ir, tb, _ = prepare(ir, yaml.safe_load((GOLDEN / "testbench.yaml").read_text()))
    cir = emit(ir, tb)
    for c in ir.components:
        assert c.ref.replace("-", "_") in cir, c.ref


def test_pin_names_must_match_symbol():
    ir = irmod.load(GOLDEN / "ir.json")
    bad = ir.model_copy(deep=True)
    bad.component("QM11").pins = {"1": "a", "2": "b", "3": "c"}
    with pytest.raises(ValueError):
        emit(bad, {})


def test_switch_poles_follow_testbench_state():
    from schem2spice.ir import IR, Component
    sw = Component(ref="S403-1", type="switch_spdt", value="", pins={"1": "a", "C": "c", "2": "b"})
    r = Component(ref="R1", type="resistor", value="1k", pins={"1": "c", "2": "gnd"})
    cir = emit(IR(components=[sw, r]), {"params": {"S403": "drawn"}})
    assert "RS403_1_2 c b 0.001" in cir and "RS403_1_1 c a 1e+12" in cir
    with pytest.raises(KeyError):
        emit(IR(components=[sw, r]), {"params": {}})


def test_opamp_section_borrows_supply_from_sibling():
    from schem2spice.ir import IR, Component
    a = Component(ref="Q401-1", type="opamp", value="NJM2068", pins={"+": "p", "-": "m", "out": "o", "V+": "vp"})
    b = Component(ref="Q401-2", type="opamp", value="NJM2068", pins={"+": "p2", "-": "m2", "out": "o2", "V-": "vn"})
    from schem2spice.netlist import _sibling_pins
    assert _sibling_pins(IR(components=[a, b]), a, {"V-"})["V-"] == "vn"
    with pytest.raises(ValueError):
        _sibling_pins(IR(components=[a]), a, {"V-"})


def test_rotary_pole_closes_only_the_selected_throw():
    pole = irmod.Component(ref="S401-1", type="rotary_pole", value="",
                           pins={"C": "out", "1": "phono", "2": "cd", **{t: f"nc{t}" for t in "3456"}})
    ir = irmod.IR(components=[pole])
    text = emit(ir, {"params": {"S401": 2}})
    assert "RS401_1_2 out cd 0.001" in text and "RS401_1_1 out phono 1e+12" in text
    assert "RS401_1_2 out cd 0.001" in emit(ir, {"params": {"S401": "drawn"}})  # drawn = CD
    with pytest.raises(KeyError):
        emit(ir, {"params": {}})


def test_region_cut_keeps_listed_parts():
    parts = [irmod.Component(ref=r, type="resistor", value="1k", pins={"1": "a", "2": "b"}, bbox=(10, 10, 4, 4))
             for r in ("R427", "R428", "R430")]
    ir, _, notes = prepare(irmod.IR(components=parts), {"cut": [{"region": [0, 0, 100, 100], "keep": ["R427"]}]})
    assert [c.ref for c in ir.components] == ["R427"]
