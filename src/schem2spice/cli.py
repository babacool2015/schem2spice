"""Command line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from . import erc, ir as irmod, netlist, project, sim, validate


def cmd_sim(args: argparse.Namespace) -> int:
    proj = project.current()
    tb = yaml.safe_load(proj.resolve(args.tb).read_text())
    ir, tb, notes = netlist.prepare(irmod.load(proj.resolve(args.ir)), tb)
    ir = irmod.derive_nets(ir)
    for n in notes:
        print("testbench:", n)
    driven = {s["net"] for s in tb.get("sources", [])} | {ld["net"] for ld in tb.get("loads", [])}
    driven |= set(tb.get("_open", []))  # testbench `leaves:` wires that run off the crop
    # Contradictory printed voltages are a validation-data problem (a label on the
    # wrong wire), not a circuit one: warn, simulate, and let validation show it.
    for net, volts in erc.conflicting_voltages(ir).items():
        print(f"ERC warning: net {net}: printed voltages disagree {volts}")
    problems = erc.check(ir, driven, voltages=False)
    for p in problems:
        print("ERC:", p)
    if problems:
        return 1
    out = _out(args)
    cir = out / f"{args.name}.cir"
    cir.write_text(netlist.emit(ir, tb, title=args.name))
    volts = sim.run_op(cir)
    tol = tb.get("tolerance", {})
    checks = validate.compare(ir, volts, tol.get("rel", 0.10), tol.get("abs", 0.05))
    text = validate.report(checks)
    if notes:
        text += "\n\nTestbench notes:\n" + "\n".join(f"- {n}" for n in notes)
    (out / f"{args.name}_validate.md").write_text(text + "\n")
    print(text)
    return 0 if all(c.ok for c in checks) else 2


def cmd_extract(args: argparse.Namespace) -> int:
    """page crop -> detect -> ocr -> trace -> IR (+ overlay, + diff against a golden IR)."""
    import cv2

    from . import detect, diff, ocr, review, trace

    proj = project.current()
    crops = proj.crops()
    if args.crop not in crops:
        print(f"no crop {args.crop!r} in {proj.crops_file}")
        return 1
    crop_def = crops[args.crop]
    page_no = args.page or crop_def["page"]
    x0, y0, x1, y1 = crop_def["rect"]
    page = proj.read_page(page_no)
    crop = page[y0:y1, x0:x1]
    # Sheets are not all drawn at one scale (page 6 is 1.26x page 4). Every pixel
    # threshold and template was set on page 4, so bring the crop to that scale and
    # map coordinates back to the page in to_ir.
    scale = float(crop_def.get("scale", 1.0))
    crop = normalize_scale(crop, scale)
    dets = detect.detect_symbols(crop)
    texts = ocr.read_text(crop, n_refs=crop_def.get("n_refs", False))
    res = trace.trace(crop, dets, texts)
    meta = {"source": f"pages/page-{page_no:02d}.png", "pdf_page": page_no, "crop": args.crop,
            "rect": [x0, y0, x1, y1], "scale": scale}
    ir = trace.to_ir(dets, texts, res, (x0, y0), meta, scale)
    items = review.load_corrections(args.crop)
    if items:
        ir, log = review.apply_corrections(ir, items, args.proposed)
        for line in log:
            print("correction:", line)
    labels = review.load_corrections(args.crop, "annotations")
    if labels:
        ir, log = review.apply_annotation_corrections(ir, labels, args.proposed)
        for line in log:
            print("correction:", line)
    out = _out(args)
    irmod.save(ir, out / f"{args.crop}_extracted.json")
    cv2.imwrite(str(out / f"{args.crop}_trace.png"), review.render_trace(crop, res))
    print(f"{len(ir.components)} parts, {len(ir.connectors)} connectors, {len(ir.nets)} nets, "
          f"{len(ir.annotations)} voltages, {len(res.floating)} floating pins "
          f"-> {out / f'{args.crop}_extracted.json'}, {out / f'{args.crop}_trace.png'}")
    if args.golden:
        g = irmod.load(proj.resolve(args.golden))
        region = tuple(args.score) if args.score else (x0, y0, x1, y1)
        print("symbols:", diff.diff_symbols(
            [c for c in g.components if c.bbox and _inside(c.bbox, region)], ir.components).report())
        print("nets:", diff.diff_nets(g, ir, region).report())
    return 0


def cmd_bom(args: argparse.Namespace) -> int:
    """IR -> values/tolerances from the parts list -> IR + markdown report."""
    from . import bom

    src = project.current().resolve(args.ir)
    ir, issues = bom.apply(irmod.load(src), bom.load_parts_list(), _variant(args))
    name = src.stem.removesuffix("_extracted")
    out = _out(args)
    irmod.save(ir, out / f"{name}_bom.json")
    (out / f"{name}_bom.md").write_text(bom.report(ir, issues))
    for i in issues:
        print(i)
    errs = sum(i.level == "error" for i in issues)
    print(f"{len(ir.components)} parts, {errs} errors, {len(issues) - errs} warnings "
          f"-> {out / f'{name}_bom.json'}, {out / f'{name}_bom.md'}")
    return 1 if errs else 0


def cmd_review(args: argparse.Namespace) -> int:
    """extracted IR + bom issues + corrections on file -> out/<crop>_review.html"""
    import cv2

    from . import bom, review

    proj = project.current()
    out = _out(args)
    src = out / f"{args.crop}_extracted.json"
    if not src.exists():
        print(f"{src} missing: run `extract --crop {args.crop}` first")
        return 1
    ir = irmod.load(src)
    page = cv2.imread(str(proj.path(ir.meta["source"])), cv2.IMREAD_GRAYSCALE)
    ir_b, issues = bom.apply(ir, bom.load_parts_list(), _variant(args))
    lib = netlist.load_library()
    floating = {c.ref for c in ir.components
                if any(n.startswith("nc") and p not in lib[c.type].get("may_float", ()) for p, n in c.pins.items())}
    reasons = review.needs_review(ir_b, issues, floating)
    items = review.load_corrections(args.crop)
    label_items = review.load_corrections(args.crop, "annotations")
    title = f"Review: {args.crop}"
    path = out / f"{args.crop}_review.html"
    path.write_text(review.review_sheet(page, ir_b, reasons, items, title, label_items))
    every = items + label_items
    print(f"{len(reasons)} parts need a look, {len(every)} corrections on file "
          f"({len(label_items)} for voltage labels, {sum(i['status'] == 'proposed' for i in every)} proposed) -> {path}")
    for ref, rs in reasons.items():
        print(f"  {ref}: {'; '.join(rs)}")
    from .erc import conflicting_voltages
    for net, volts in conflicting_voltages(ir_b).items():
        print(f"  net {net}: printed voltages disagree {volts}")
    return 0


def cmd_system(args: argparse.Namespace) -> int:
    """Chain boards (systems/*.yaml) -> DC operating point of the whole chain against
    every board's printed voltages, then AC cases against the published specs."""
    from . import system as sysmod

    proj = project.current()
    spec = sysmod.load(proj.resolve(args.file))
    out = _out(args, "sys")
    if args.build:  # rebuild each board's IR from the scan (proposed corrections applied)
        for b, bspec in spec["boards"].items():
            path = proj.path(bspec["ir"])
            irmod.save(build_ir(bspec.get("crop", b)), path)
            print(f"built {b} -> {path}")
    name = Path(args.file).stem
    first = args.case[0] if args.case else next(iter(spec["cases"]))
    checks, notes = sysmod.evaluate_op(spec, first, out)
    op = validate.report(checks)
    text, results = sysmod.evaluate(spec, out, args.case)
    ok = all(r.ok for r in results)
    text += f"\n## DC operating point of the chain (case {first})\n\n" + op
    text += "\n\nNotes:\n" + "\n".join(f"- {n}" for n in notes)
    (out / f"{name}_report.md").write_text(text + "\n")
    print(text.split("## DC")[0])
    print(op.splitlines()[-1])
    if not args.case:  # every case ran: draw the charts
        from . import plot

        for path in plot.render(spec, out, name, text.split("## DC")[0]):
            print("plot:", path)
    return 0 if ok else 2


def cmd_plot(args: argparse.Namespace) -> int:
    """Redraw a system's charts from its last run (response CSVs), without simulating."""
    from . import plot
    from . import system as sysmod

    proj = project.current()
    path = proj.resolve(args.file)
    out = _out(args, "sys")
    report = out / f"{path.stem}_report.md"
    text = report.read_text().split("## DC")[0] if report.exists() else None
    written = plot.render(sysmod.load(path), out, path.stem, text)
    if not written:
        print(f"{path}: no plots: section")
        return 1
    for w in written:
        print("plot:", w)
    return 0


def build_ir(crop_name: str, variant: str | None = "project", proposed: bool = True):
    """Scan crop -> detect/ocr/trace -> corrections (incl. proposed) -> bom: the IR the
    system chain uses, built in memory (tests, `system --build`)."""
    from . import bom, detect, ocr, review, trace

    proj = project.current()
    c = proj.crops()[crop_name]
    x0, y0, x1, y1 = c["rect"]
    page = proj.read_page(c["page"])
    if variant == "project":
        variant = proj.variant
    scale = float(c.get("scale", 1.0))
    crop = normalize_scale(page[y0:y1, x0:x1], scale)
    dets = detect.detect_symbols(crop)
    texts = ocr.read_text(crop, n_refs=c.get("n_refs", False))
    meta = {"source": f"pages/page-{c['page']:02d}.png", "pdf_page": c["page"], "crop": crop_name,
            "rect": [x0, y0, x1, y1], "scale": scale}
    x = trace.to_ir(dets, texts, trace.trace(crop, dets, texts), (x0, y0), meta, scale)
    x, _ = review.apply_corrections(x, review.load_corrections(crop_name), proposed)
    x, _ = review.apply_annotation_corrections(x, review.load_corrections(crop_name, "annotations"), proposed)
    x, _ = bom.apply(x, bom.load_parts_list(), variant)
    return x


def _out(args: argparse.Namespace, sub: str = "") -> Path:
    """--out as given, else the project's out/ (out/<sub>)."""
    out = Path(args.out) if args.out else project.current().out_dir / sub
    out.mkdir(parents=True, exist_ok=True)
    return out


def _variant(args: argparse.Namespace) -> str | None:
    return args.variant if args.variant is not None else project.current().variant


def cmd_ingest(args: argparse.Namespace) -> int:
    """The project's PDF -> pages/page-NN.png (lossless, rotation applied)."""
    from . import ingest

    proj = project.current()
    pdf = proj.resolve(args.pdf) if args.pdf else proj.pdf
    if pdf is None or not pdf.exists():
        print(f"PDF not found ({pdf}): put your copy there or pass --pdf")
        return 1
    status = proj.pdf_status() if not args.pdf else "unchecked"
    if status == "mismatch":
        print(f"warning: {pdf} does not match the sha256 in project.yaml (another scan or edition?)")
    for r in ingest.extract_pages(pdf, proj.pages_dir):
        print(f"p{r.page:02d} {r.width}x{r.height} bpc={r.bpc} {r.colorspace} {r.filter} ~{r.dpi}dpi rot={r.rotation}")
    return 0


PROJECT_YAML = """\
# {name}: settings that depend on this service manual. See docs/new-project.md.
name: {name}
pdf:
  path: {pdf}
  sha256: {sha}
  source: Not distributed. Supply your own copy at the path above.
# Sales variant of the unit you model, as the parts list marks rows ([/22B] ...).
variant: null
parts_list:
  pages: []                # PDF pages of the electrical parts list
  part_number: '^\\S+$'    # pattern of the part-number column (Philips: '^4822\\s*\\d{{3}}\\s*\\d{{5}}$')
ocr:
  module_ref_letter: null  # e.g. M when modules use refs like QM11 (an OCR'd QN11 is then QM11)
"""


def cmd_init_project(args: argparse.Namespace) -> int:
    """Scaffold projects/<name>/ (or --dir) for a new manual."""
    import shutil

    root = Path(args.dir) if args.dir else project.ENGINE_ROOT / "projects" / args.name
    if (root / project.FILE).exists():
        print(f"{root / project.FILE} exists already")
        return 1
    for d in ("corrections", "testbenches", "systems", "data"):
        (root / d).mkdir(parents=True, exist_ok=True)
    pdf_rel, sha = "data/manual.pdf", "null"
    if args.pdf:
        src = Path(args.pdf)
        pdf_rel = f"data/{src.name}"
        if src.resolve() != (root / pdf_rel).resolve():
            shutil.copy2(src, root / pdf_rel)
        sha = project.sha256(root / pdf_rel)
    (root / project.FILE).write_text(PROJECT_YAML.format(name=args.name, pdf=pdf_rel, sha=sha))
    (root / "crops.yaml").write_text(
        "# Named crops: <name>: {page: <PDF page>, rect: [x0, y0, x1, y1], note: ...}\n"
        "# rect is in px on pages/page-NN.png. Add scale: <s> if the sheet is drawn s x the\n"
        "# library's scale, n_refs: true if the sheet has real N refs (QN01).\n")
    print(f"created {root}\nnext: schem2spice ingest --project {root}   (then see docs/new-project.md)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the external tools and, if there is one, the current project."""
    import importlib.util
    import shutil
    import subprocess
    import sys

    ok = True

    def line(good: bool, what: str, fix: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'}  {what}" + (f"\n      fix: {fix}" if fix and not good else ""))

    line(sys.version_info >= (3, 11), f"python {sys.version.split()[0]} (>= 3.11)", "install Python 3.11+")
    ng = shutil.which("ngspice")
    ver = ""
    if ng:
        r = subprocess.run([ng, "-v"], capture_output=True, text=True, timeout=30)
        ver = next((s.strip(" *").split(" :")[0] for s in (r.stdout + r.stderr).splitlines() if "ngspice-" in s), "")
    line(bool(ng), f"ngspice {ver or 'not found'}", "make tools  (brew install ngspice / sudo apt-get install ngspice)")
    for mod in ("cv2", "pymupdf", "rapidocr", "onnxruntime", "yaml", "pydantic"):
        line(importlib.util.find_spec(mod) is not None, f"python module {mod}", "make venv")
    if importlib.util.find_spec("rapidocr"):
        models = Path(importlib.util.find_spec("rapidocr").origin).parent / "models"
        n = len(list(models.glob("*.onnx"))) if models.is_dir() else 0
        line(n >= 2, f"RapidOCR models ({n} onnx files)", "make ocr-models (needs network once)")
    proj = project.optional()
    if proj is None:
        print("--    no project (pass --project DIR or set $S2S_PROJECT)")
    else:
        st = proj.pdf_status()
        line(st in ("ok", "unchecked"), f"project {proj.name}: PDF {st} ({proj.pdf})",
             "put your copy of the manual at that path" if st == "missing" else "check you have the same scan")
        n = len(list(proj.pages_dir.glob("page-*.png")))
        line(n > 0 or st == "missing", f"project pages: {n} extracted", f"schem2spice ingest --project {proj.root}")
    return 0 if ok else 1


def normalize_scale(crop, scale: float):
    """Resample a binary scan crop by 1/scale and re-binarize (no grey edges)."""
    import cv2

    if scale == 1.0:
        return crop
    small = cv2.resize(crop, None, fx=1 / scale, fy=1 / scale, interpolation=cv2.INTER_AREA)
    return ((small >= 128) * 255).astype(crop.dtype)


def _inside(b, region) -> bool:
    cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
    return region[0] <= cx < region[2] and region[1] <= cy < region[3]


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS,
                        help=f"project folder (default: ${project.ENV}, else the nearest {project.FILE})")
    p = argparse.ArgumentParser(prog="schem2spice", parents=[common],
                                description="Scanned service-manual schematic -> BOM, SPICE netlist, simulation")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, func, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, parents=[common], help=help)
        sp.set_defaults(func=func)
        return sp

    i = cmd("ingest", cmd_ingest, "the project's PDF -> pages/page-NN.png")
    i.add_argument("--pdf", help="PDF to extract (default: project.yaml pdf.path)")
    s = cmd("sim", cmd_sim, "IR -> netlist -> ngspice .op -> validate")
    s.add_argument("--ir", required=True)
    s.add_argument("--tb", required=True)
    s.add_argument("--name", default="circuit")
    s.add_argument("--out", help="output folder (default: the project's out/)")
    e = cmd("extract", cmd_extract, "page crop -> detect/ocr/trace -> IR, overlay, optional golden diff")
    e.add_argument("--crop", required=True, help="name in the project's crops.yaml")
    e.add_argument("--page", type=int, help="PDF page (default: from crops.yaml)")
    e.add_argument("--golden", help="golden IR to diff against")
    e.add_argument("--score", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1"),
                   help="only score golden items inside this page rect (default: the crop)")
    e.add_argument("--proposed", action="store_true",
                   help="also apply corrections with status: proposed (default: confirmed only)")
    e.add_argument("--out")
    r = cmd("review", cmd_review, "HTML sheet of parts needing a human look, with corrections on file")
    r.add_argument("--crop", required=True)
    r.add_argument("--variant", help="sales variant (default: project.yaml variant)")
    r.add_argument("--out")
    y = cmd("system", cmd_system, "chain boards (systems/*.yaml): DC of the chain + AC spec checks")
    y.add_argument("--file", required=True)
    y.add_argument("--case", nargs="*", help="only these cases (default: all)")
    y.add_argument("--out", help="output folder (default: the project's out/sys)")
    y.add_argument("--build", action="store_true", help="first rebuild the boards' IRs from the scan")
    pl = cmd("plot", cmd_plot, "redraw a system's frequency-response charts from its last run")
    pl.add_argument("--file", required=True)
    pl.add_argument("--out", help="folder of the run (default: the project's out/sys)")
    b = cmd("bom", cmd_bom, "IR -> values from the parts list -> IR + report")
    b.add_argument("--ir", required=True)
    b.add_argument("--variant", help="sales variant code from the parts list, e.g. 22B (default: project.yaml)")
    b.add_argument("--out")
    n = cmd("init-project", cmd_init_project, "scaffold a project folder for a new manual")
    n.add_argument("name")
    n.add_argument("--pdf", help="the manual (copied into the project's data/)")
    n.add_argument("--dir", help="where to create it (default: projects/<name>)")
    cmd("doctor", cmd_doctor, "check external tools and the current project")
    args = p.parse_args(argv)
    if getattr(args, "project", None):
        project.use(args.project)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
