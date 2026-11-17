import pytest

pytest.importorskip("cv2")
from schem2spice import ir as irmod, review  # noqa: E402


def _ir():
    mk = lambda ref, typ, val, bbox: irmod.Component(ref=ref, type=typ, value=val, pins={}, bbox=bbox, confidence=0.4)
    return irmod.IR(components=[mk("?3", "resistor", "", (100, 100, 40, 76)),
                                mk("R526", "resistor", "100", (100, 300, 76, 40)),
                                mk("?9", "capacitor", "", (500, 500, 74, 46))])


def test_corrections_match_by_type_and_position():
    items = [
        {"at": [125, 140], "type": "resistor", "ref": "R527", "value": "1.5k", "status": "confirmed"},
        {"at": [138, 320], "type": "resistor", "ref": "R525", "status": "proposed"},
        {"at": [537, 523], "type": "resistor", "ref": "R1", "status": "confirmed"},  # wrong type: no match
        {"at": [537, 523], "type": "capacitor", "delete": True, "status": "confirmed"},
    ]
    out, log = review.apply_corrections(_ir(), items)
    assert [(c.ref, c.value) for c in out.components] == [("R527", "1.5k"), ("R526", "100")]
    assert out.components[0].confidence == 1.0 and "corrected (confirmed)" in out.components[0].note
    assert [line.split()[0] for line in log] == ["set", "skip", "MISS", "del"]
    out, _ = review.apply_corrections(_ir(), items, include_proposed=True)
    assert out.components[1].ref == "R525" and out.components[1].confidence == 0.4


def test_corrections_only_apply_to_their_page():
    ir = _ir().model_copy(update={"meta": {"pdf_page": 4}})
    item = {"at": [120, 138], "type": "resistor", "ref": "R527", "status": "confirmed"}
    assert review.apply_corrections(ir, [dict(item, page=4)])[0].components[0].ref == "R527"
    out, log = review.apply_corrections(ir, [dict(item, page=7)])
    assert out.components[0].ref == "?3" and log[0].startswith("MISS")


def test_duplicate_refs_are_reported():
    items = [{"at": [120, 138], "type": "resistor", "ref": "R526", "status": "confirmed"}]
    _, log = review.apply_corrections(_ir(), items)
    assert log[-1].startswith("WARN") and "R526" in log[-1]


def test_status_is_required(tmp_path, monkeypatch):
    (tmp_path / "x.yaml").write_text("components:\n  - {at: [1, 2], type: resistor, ref: R1}\n")
    monkeypatch.setattr(review, "CORRECTIONS", tmp_path)
    with pytest.raises(ValueError):
        review.load_corrections("x")


def test_voltage_labels_can_be_moved_and_conflicts_found():
    from schem2spice import erc
    ir = irmod.IR(components=[irmod.Component(ref="Q1", type="npn", value="x", pins={"C": "a", "B": "b", "E": "c"})],
                  annotations=[irmod.Annotation(net="a", voltage=-17.2, bbox=(100, 100, 40, 20)),
                               irmod.Annotation(net="a", voltage=0.0, bbox=(300, 100, 20, 20))],
                  meta={"pdf_page": 4})
    assert erc.conflicting_voltages(ir) == {"a": [-17.2, 0.0]}
    item = {"page": 4, "at": [120, 110], "net_pin": "Q1.E", "status": "confirmed"}
    out, log = review.apply_annotation_corrections(ir, [item])
    assert [a.net for a in out.annotations] == ["c", "a"] and not erc.conflicting_voltages(out)
    out, _ = review.apply_annotation_corrections(ir, [dict(item, status="proposed")])
    assert out.annotations[0].net == "a"
