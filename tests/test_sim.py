import shutil
from pathlib import Path

import pytest
import yaml

from schem2spice import ir as irmod, netlist, sim, validate
from schem2spice.project import current as _project  # noqa: E402

P = _project()  # the reference project (tests/conftest.py)

GOLDEN = P.root / "golden" / "pm11"

pytestmark = pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")

# Printed values the pilot does not yet reproduce within tolerance.
# outh/outl: simulated bias spread across D501+D503+R517 is ~1.39 V vs 1.2 V printed.
# Keep this list honest: shrink it as models improve, never grow it to make CI pass.
KNOWN_DEVIATIONS = {"outh", "outl"}


def test_pm11_op_matches_printed_voltages(tmp_path):
    ir = irmod.load(GOLDEN / "ir.json")
    ir, tb, _ = netlist.prepare(ir, yaml.safe_load((GOLDEN / "testbench.yaml").read_text()))
    cir = tmp_path / "pm11.cir"
    cir.write_text(netlist.emit(ir, tb))
    volts = sim.run_op(cir)
    checks = validate.compare(ir, volts, tb["tolerance"]["rel"], tb["tolerance"]["abs"])
    failing = {c.net for c in checks if not c.ok}
    assert failing == KNOWN_DEVIATIONS, validate.report(checks)


def test_extracted_p501l_simulates_like_golden(tmp_path):
    """Scan -> detect/ocr/trace -> confirmed corrections -> bom -> netlist -> ngspice
    must give every printed voltage the same simulated value as the golden IR."""
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("rapidocr")
    from schem2spice import bom, detect, ocr, review, trace
    page = P.pages_dir / "page-04.png"
    if not page.exists():
        pytest.skip("run `python -m schem2spice.ingest` first")
    x0, y0, x1, y1 = 6250, 650, 9300, 3200
    crop = cv2.imread(str(page), cv2.IMREAD_GRAYSCALE)[y0:y1, x0:x1]
    dets, texts = detect.detect_symbols(crop), ocr.read_text(crop)
    x = trace.to_ir(dets, texts, trace.trace(crop, dets, texts), (x0, y0), {"pdf_page": 4})
    x, _ = review.apply_corrections(x, review.load_corrections("p501l"))
    x, issues = bom.apply(x, bom.load_parts_list())
    assert not [i for i in issues if i.level == "error"]

    tb = yaml.safe_load((GOLDEN / "testbench.yaml").read_text())

    def run(ir, name):
        ir, bench, _ = netlist.prepare(ir, tb)
        cir = tmp_path / f"{name}.cir"
        cir.write_text(netlist.emit(ir, bench))
        checks = validate.compare(ir, sim.run_op(cir), tb["tolerance"]["rel"], tb["tolerance"]["abs"])
        return sorted((c.printed, round(c.simulated, 3)) for c in checks)

    assert run(x, "p501l") == run(irmod.load(GOLDEN / "ir.json"), "golden")


def test_extracted_p401_matches_printed_voltages(tmp_path):
    """P401, both channels (unseen sheet): scan -> corrections (incl. still-proposed
    items) -> bom 22B -> ngspice. Every printed voltage within tolerance, and the two
    channels simulate alike (symmetric design: catches a misread value, like RM33
    read "89", that would still pass the +/-10 % check)."""
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("rapidocr")
    from schem2spice import bom, detect, ocr, review, trace
    page = P.pages_dir / "page-04.png"
    if not page.exists():
        pytest.skip("run `python -m schem2spice.ingest` first")
    x0, y0, x1, y1 = 900, 600, 5800, 4750
    crop = cv2.imread(str(page), cv2.IMREAD_GRAYSCALE)[y0:y1, x0:x1]
    dets, texts = detect.detect_symbols(crop), ocr.read_text(crop)
    x = trace.to_ir(dets, texts, trace.trace(crop, dets, texts), (x0, y0), {"pdf_page": 4})
    x, _ = review.apply_corrections(x, review.load_corrections("p401"), include_proposed=True)
    x, _ = review.apply_annotation_corrections(x, review.load_corrections("p401", "annotations"), True)
    x, issues = bom.apply(x, bom.load_parts_list(), "22B")
    tb = yaml.safe_load((P.testbenches_dir / "p401.yaml").read_text())
    ir, bench, _ = netlist.prepare(x, tb)
    live = {c.ref for c in ir.components}
    assert not [i for i in issues if i.level == "error" and i.ref in live]  # errors only in cut parts
    cir = tmp_path / "p401.cir"
    cir.write_text(netlist.emit(ir, bench))
    volts = sim.run_op(cir)
    checks = validate.compare(ir, volts, tb["tolerance"]["rel"], tb["tolerance"]["abs"])
    assert len(checks) == 28 and all(c.ok for c in checks), validate.report(checks)
    pins = {f"{c.ref}.{p}": n for c in ir.components for p, n in c.pins.items()}
    for left, right in [("QM33.E", "QM43.E"), ("QM36.E", "QM46.E"), ("QM34.C", "QM44.C"),
                        ("RM32.2", "RM42.2"), ("Q401-1.out", "Q401-2.out")]:
        assert abs(volts[netlist.node(pins[left])] - volts[netlist.node(pins[right])]) < 1e-3, (left, right)


def test_extracted_p501_matches_printed_voltages(tmp_path):
    """P501 volume board, both channels (PM11 + PM21): scan -> corrections (incl.
    proposed) -> bom 22B -> ngspice. Misses only at the +/-0.6 V output-stage bias
    points (outh/outl, KNOWN_DEVIATIONS of the pilot), and the channels are twins."""
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("rapidocr")
    from schem2spice import bom, detect, ocr, review, trace
    page = P.pages_dir / "page-04.png"
    if not page.exists():
        pytest.skip("run `python -m schem2spice.ingest` first")
    x0, y0, x1, y1 = 6250, 650, 9300, 4600
    crop = cv2.imread(str(page), cv2.IMREAD_GRAYSCALE)[y0:y1, x0:x1]
    dets, texts = detect.detect_symbols(crop), ocr.read_text(crop)
    x = trace.to_ir(dets, texts, trace.trace(crop, dets, texts), (x0, y0), {"pdf_page": 4})
    x, _ = review.apply_corrections(x, review.load_corrections("p501"), include_proposed=True)
    x, _ = review.apply_annotation_corrections(x, review.load_corrections("p501", "annotations"), True)
    x, issues = bom.apply(x, bom.load_parts_list(), "22B")
    assert not [i for i in issues if i.level == "error"]
    assert not [c.ref for c in x.components if c.ref.startswith("?")]
    tb = yaml.safe_load((P.testbenches_dir / "p501.yaml").read_text())
    ir, bench, _ = netlist.prepare(x, tb)
    cir = tmp_path / "p501.cir"
    cir.write_text(netlist.emit(ir, bench))
    volts = sim.run_op(cir)
    checks = validate.compare(ir, volts, tb["tolerance"]["rel"], tb["tolerance"]["abs"])
    assert len(checks) == 33, validate.report(checks)
    bad = [c for c in checks if not c.ok]
    assert len(bad) == 7 and all(abs(c.printed) == 0.6 and abs(c.delta) < 0.1 for c in bad), validate.report(checks)
    pins = {f"{c.ref}.{p}": n for c in ir.components for p, n in c.pins.items()}
    for left, right in [("QM11.S", "QM21.S"), ("QM14.C", "QM24.C"), ("QM15.C", "QM25.C"),
                        ("Q501.E", "Q502.E"), ("Q503.E", "Q504.E"), ("C509.+", "C510.+")]:
        assert abs(volts[netlist.node(pins[left])] - volts[netlist.node(pins[right])]) < 1e-3, (left, right)


# P701: the input pair's base voltages (-0.3 V printed at the input base, -0.2 V at the
# feedback base) imply hFE ~150-225 for the 2SC3067; the stand-in model (no datasheet on
# hand, 2SC2240 parameters, hFE 400) gives -0.08 V. Q719/Q720 base: -0.67 V vs -0.6 V
# (Darlington Vbe of the stand-in subcircuit). Reported, not fitted.
P701_KNOWN_LIMIT = 0.25  # V


def test_extracted_p701_matches_printed_voltages(tmp_path):
    """P701 power amp (page 7, scale 1.26, both channels, Class AB): scan -> corrections
    (incl. proposed) -> bom 22B -> ngspice, bias trimmers set by the manual's idling
    procedure. Misses only at the known points, and the channels are twins."""
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("rapidocr")
    from schem2spice import bom, detect, ocr, review, trace
    from schem2spice.cli import normalize_scale
    page = P.pages_dir / "page-07.png"
    if not page.exists():
        pytest.skip("run `python -m schem2spice.ingest` first")
    x0, y0, x1, y1, scale = 1500, 650, 7350, 5750, 1.26
    crop = normalize_scale(cv2.imread(str(page), cv2.IMREAD_GRAYSCALE)[y0:y1, x0:x1], scale)
    dets, texts = detect.detect_symbols(crop), ocr.read_text(crop, n_refs=True)
    x = trace.to_ir(dets, texts, trace.trace(crop, dets, texts), (x0, y0), {"pdf_page": 7}, scale)
    x, _ = review.apply_corrections(x, review.load_corrections("p701"), include_proposed=True)
    x, _ = review.apply_annotation_corrections(x, review.load_corrections("p701", "annotations"), True)
    x, issues = bom.apply(x, bom.load_parts_list(), "22B")
    assert not [i for i in issues if i.level == "error"]
    assert not [c.ref for c in x.components if c.ref.startswith("?")]
    tb = yaml.safe_load((P.testbenches_dir / "p701.yaml").read_text())
    ir, bench, _ = netlist.prepare(x, tb)
    cir = tmp_path / "p701.cir"
    cir.write_text(netlist.emit(ir, bench))
    volts = sim.run_op(cir)
    checks = validate.compare(ir, volts, tb["tolerance"]["rel"], tb["tolerance"]["abs"])
    assert len(checks) == 80, validate.report(checks)
    bad = [c for c in checks if not c.ok]
    assert len(bad) == 5 and all(abs(c.delta) < P701_KNOWN_LIMIT for c in bad), validate.report(checks)
    pins = {f"{c.ref}.{p}": n for c in ir.components for p, n in c.pins.items()}
    v = lambda pin: volts[netlist.node(pins[pin])]  # noqa: E731
    assert abs(v("R781.1") - v("R781.2") - 0.018) < 1e-3  # the idling procedure's 18 mV
    for left, right in [("Q701-1.B", "Q702-1.B"), ("Q713.E", "Q714.E"), ("Q719.B", "Q720.B"),
                        ("Q731.B", "Q732.B"), ("Q733.B", "Q734.B"), ("R781.M", "R782.M")]:
        assert abs(v(left) - v(right)) < 1e-3, (left, right)


def test_extracted_p851_matches_printed_voltages(tmp_path):
    """P851 power supply (PDF 10 copy, scale 1.26): scan -> corrections (incl. proposed)
    -> bom 22B -> ngspice with the raw rectified rails driven and P601 as the +-18 V
    load. Every printed voltage of the regulators within tolerance."""
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("rapidocr")
    from schem2spice import bom, detect, ocr, review, trace
    from schem2spice.cli import normalize_scale
    page = P.pages_dir / "page-10.png"
    if not page.exists():
        pytest.skip("run `python -m schem2spice.ingest` first")
    x0, y0, x1, y1, scale = 880, 950, 4650, 3700, 1.26
    crop = normalize_scale(cv2.imread(str(page), cv2.IMREAD_GRAYSCALE)[y0:y1, x0:x1], scale)
    dets, texts = detect.detect_symbols(crop), ocr.read_text(crop)
    x = trace.to_ir(dets, texts, trace.trace(crop, dets, texts), (x0, y0), {"pdf_page": 10}, scale)
    x, _ = review.apply_corrections(x, review.load_corrections("p851"), include_proposed=True)
    x, _ = review.apply_annotation_corrections(x, review.load_corrections("p851", "annotations"), True)
    x, issues = bom.apply(x, bom.load_parts_list(), "22B")
    assert not [i for i in issues if i.level == "error"]
    assert not [c.ref for c in x.components if c.ref.startswith("?")]
    tb = yaml.safe_load((P.testbenches_dir / "p851.yaml").read_text())
    ir, bench, _ = netlist.prepare(x, tb)
    cir = tmp_path / "p851.cir"
    cir.write_text(netlist.emit(ir, bench))
    volts = sim.run_op(cir)
    checks = validate.compare(ir, volts, tb["tolerance"]["rel"], tb["tolerance"]["abs"])
    assert len(checks) == 15 and all(c.ok for c in checks), validate.report(checks)
    pins = {f"{c.ref}.{p}": n for c in ir.components for p, n in c.pins.items()}
    v = lambda pin: volts[netlist.node(pins[pin])]  # noqa: E731
    assert abs(v("R857.2") + v("R858.2")) < 0.1  # +-18 V regulators are symmetric


# Printed +0.03 V on P601's high-impedance signal nodes vs about -0.04 V simulated:
# the NJM2068 input bias current (200 nA typ) through the 220k input resistors; the
# unit's own offset is not modelled. Keep this honest like KNOWN_DEVIATIONS.
P601_NEAR_ZERO_LIMIT = 0.1  # V


def test_extracted_p601_matches_printed_voltages(tmp_path):
    """P601 tone amp (page 6, drawn at 1.26x page 4): scan -> scale normalization ->
    corrections (incl. proposed) -> bom 22B -> ngspice."""
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("rapidocr")
    from schem2spice import bom, detect, ocr, review, trace
    from schem2spice.cli import normalize_scale
    page = P.pages_dir / "page-06.png"
    if not page.exists():
        pytest.skip("run `python -m schem2spice.ingest` first")
    x0, y0, x1, y1, scale = 1750, 550, 8100, 3450, 1.26
    crop = normalize_scale(cv2.imread(str(page), cv2.IMREAD_GRAYSCALE)[y0:y1, x0:x1], scale)
    dets, texts = detect.detect_symbols(crop), ocr.read_text(crop)
    x = trace.to_ir(dets, texts, trace.trace(crop, dets, texts), (x0, y0), {"pdf_page": 6}, scale)
    x, _ = review.apply_corrections(x, review.load_corrections("p601"), include_proposed=True)
    x, _ = review.apply_annotation_corrections(x, review.load_corrections("p601", "annotations"), True)
    x, issues = bom.apply(x, bom.load_parts_list(), "22B")
    assert not [i for i in issues if i.level == "error"]
    assert not [c.ref for c in x.components if c.ref.startswith("?")]

    pins = {f"{c.ref}.{p}": n for c in x.components for p, n in c.pins.items()}
    pins.update({f"{j.ref}.{p}": n for j in x.connectors for p, n in j.pins.items()})
    # S601 poles come in mirrored L/R pairs: same role, other channel.
    for left, right in [("S601-1", "S601-2"), ("S601-3", "S601-4"), ("S601-5", "S601-6"), ("S601-7", "S601-8")]:
        assert (pins[f"{left}.C"] == pins[f"{left}.1"]) == (pins[f"{right}.C"] == pins[f"{right}.1"])
    assert pins["S601-3.C"] == pins["J601.1"] and pins["S601-4.C"] == pins["J601.4"]
    assert pins["S601-1.C"] == pins["J602.1"] and pins["S601-2.C"] == pins["J602.3"]

    tb = yaml.safe_load((P.testbenches_dir / "p601.yaml").read_text())
    ir, bench, _ = netlist.prepare(x, tb)
    cir = tmp_path / "p601.cir"
    cir.write_text(netlist.emit(ir, bench))
    volts = sim.run_op(cir)
    checks = validate.compare(ir, volts, tb["tolerance"]["rel"], tb["tolerance"]["abs"])
    assert len(checks) == 21
    for c in checks:
        if abs(c.printed) >= 1:
            assert c.ok, validate.report(checks)
        else:
            assert abs(c.delta) <= P601_NEAR_ZERO_LIMIT, validate.report(checks)
    for left, right in [("Q601-1.out", "Q601-2.out"), ("Q602-1.out", "Q602-2.out"),
                        ("Q603-1.out", "Q603-2.out"), ("Q604-1.out", "Q604-2.out")]:
        assert abs(volts[netlist.node(pins[left])] - volts[netlist.node(pins[right])]) < 1e-3, (left, right)


# Spec checks the chain does not meet, each explained in CLAUDE.md ("System chain"):
# line input impedance (the drawn circuit gives ~76 kohm at full volume, not 20 kohm)
# and treble range (+-8.6 dB at 10 kHz vs +-7 dB). Shrink, never grow.
KNOWN_SPEC_MISSES = {("line", "Tuner/CD/Aux input impedance 20 kohm"),
                     ("treble_boost", "Tone characteristic +-7 dB at 10 kHz"),
                     ("treble_cut", "Tone characteristic +-7 dB at 10 kHz")}


def test_system_chain_meets_specs(tmp_path):
    """Input jack to speaker: P401x (selector, phono) -> P501 -> P601 <-> P651 -> P701,
    every board rebuilt from the scan (corrections incl. proposed, bom 22B), joined at
    the connector wiring (systems/chain.yaml). The chain's DC operating point matches
    the boards simulated alone, and the AC cases meet the published specs (PDF 3)
    except the known misses."""
    pytest.importorskip("cv2")
    pytest.importorskip("rapidocr")
    from schem2spice import system as sm
    from schem2spice.cli import build_ir
    if not (P.pages_dir / "page-04.png").exists():
        pytest.skip("run `python -m schem2spice.ingest` first")
    spec = sm.load(P.systems_dir / "chain.yaml")
    for b, bspec in spec["boards"].items():
        path = tmp_path / f"{b}.json"
        irmod.save(build_ir(b), path)
        bspec["ir"] = str(path)
    checks, _ = sm.evaluate_op(spec, "line", tmp_path)
    assert sum(c.ok for c in checks) == 142, validate.report(checks)  # = the boards alone
    _, results = sm.evaluate(spec, tmp_path)
    missed = {(r.case, r.spec) for r in results if not r.ok}
    assert missed == KNOWN_SPEC_MISSES, "\n".join(f"{r.case}: {r.spec}: {r.value}" for r in results)
