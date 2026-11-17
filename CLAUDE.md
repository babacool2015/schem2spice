# schem2spice

Tools to turn a **clean scanned service-manual schematic** into a BOM, a SPICE netlist and a simulation, validated against the voltages printed on the schematic. General purpose: each manual is a **project** under `projects/`. The reference project is the Marantz PM-80 Mk II (`projects/marantz-pm80mk2/`, its README holds the manual's facts, board commands, results and status).

## Layout
```
src/schem2spice/   engine: ingest detect ocr trace ir review erc bom netlist sim validate system project cli
symbols/           shared symbol library (library.yaml) + templates/<type>.png
models/            shared SPICE libraries + map.yaml (part value -> model, with the substitute and why)
projects/<name>/   project.yaml, crops.yaml, corrections/, testbenches/, systems/, golden/,
                   optional symbols/ and models/ overlays; data/ pages/ out/ are git-ignored
tests/             engine tests + reference-project tests (skip without its PDF); conftest selects the project
docs/              new-project.md, corrections.md
Makefile           install (ngspice, venv, OCR models), test, test-all, ingest, chain, clean
```
- The current project comes from `--project DIR`, `$S2S_PROJECT`, or the nearest `project.yaml` above the working directory (`project.current()`). Paths written in project files (IR `meta.source`, system `ir:`/`tb:`) are relative to the project folder; paths on the command line resolve as typed if they exist, else in the project.
- Manual-specific settings live in `project.yaml`, never in code: PDF path + sha256, sales `variant`, `parts_list.pages`, `parts_list.part_number` (regex), optional `parts_list.headers`, `ocr.module_ref_letter`.
- A project's `symbols/library.yaml` and `templates/` and its `models/map.yaml` and libs overlay the shared ones (same name replaces).
- Never commit a manual, page images or outputs (copyright). `make doctor` checks tools and the project's PDF hash.

## Pipeline
1. `ingest`: extract page images losslessly (PyMuPDF, one embedded image per page, `/Rotate` applied by lossless transpose) -> `pages/page-NN.png`. Never re-render through a lossy path.
2. `stitch`: assemble multi-page sheets, drop duplicated regions (not needed so far).
3. `detect`: symbols by template matching, with bounding boxes and pin locations.
4. `ocr`: refs, values, printed voltages (RapidOCR), associated to symbols by proximity.
5. `trace`: wires, junction dots vs crossings, connectors -> nets.
6. `ir`: canonical JSON graph (below). Every stage reads/writes it.
7. `review`: HTML sheet of parts needing a look; fixes go to `corrections/<crop>.yaml`.
8. `erc`: floating pins, unconnected nets, conflicting printed voltages.
9. `bom`: values from the parts list, normalized (4k7 -> 4.7k), variants, issues.
10. `netlist`: ngspice netlist from IR + model map + testbench.
11. `sim` / `validate`: `.op`, compare with printed voltages. `system`: boards chained, AC spec checks.

## IR (JSON)
```json
{
  "meta": {"source": "pages/page-04.png", "pdf_page": 4, "crop": "p501", "rect": [x0, y0, x1, y1], "scale": 1.0},
  "components": [{"ref": "QM11", "type": "njfet", "value": "2SK369", "pins": {"D": "n3", "G": "n1", "S": "n5"},
                  "bbox": [x, y, w, h], "confidence": 0.93}],
  "nets": [{"name": "n3", "label": null, "pins": ["QM11.D", "RM13.1"]}],
  "connectors": [{"ref": "J496", "pins": {"1": "n10"}}],
  "annotations": [{"net": "n3", "voltage": 17.2, "unit": "V"}]
}
```
Every item keeps a `bbox` (page px) and `confidence` so `review` can overlay it. Pin names follow the symbol library, never visual order guessed per instance.

## Model policy
- Discrete transistors and diodes: real or close-equivalent models; `models/map.yaml` lists the substitute used for each part and why.
- Op-amps: documented macromodel with matching gain-bandwidth; note the substitution. Output limiting behavioral, no path between supply pins (rail-referenced clamps gave false collapsed-rail operating points).
- No model available (protection ICs, relays, transformers): behavioral block, or cut the circuit and drive the pins with sources. Never silently drop a part.
- Switch and pot positions are explicit testbench parameters.

## Conventions
- Python 3.11+, type hints, `pytest`. Each stage a pure function: IR in, IR out. Vision/OCR cached on disk by input hash (`out/cache/`); record engine and version in IR `meta`.
- Never fix a netlist by hand without recording it in `corrections/`. Items there are `proposed` (read off the scan by Claude or a tool, unchecked) or `confirmed` (checked by the user); only the user confirms. `extract` applies confirmed only, `--proposed` and `system --build` both. See docs/corrections.md.
- Prefer classical methods (connected components, H/V line tracing) for wires and template matching / OCR for symbols and text; justify deviations in a comment.
- Commands: `schem2spice {init-project, ingest, extract, review, bom, sim, system, doctor}` (`.venv/bin/schem2spice`). `make test` = engine tests; `make test-all` needs the reference PDF.
- Each new symbol template is cut from one copy on a scan and tested on an unseen twin; new thresholds are checked for new false hits on every existing crop. Record both in the library/detect comments.
- Validation tolerance is `max(rel * |printed|, abs)` (testbench `tolerance`), so near-0 V printed values get an absolute floor. Contradictory printed voltages on one net are a warning (a label on the wrong wire): simulate anyway and let validation show which label fails.
- A misread common-part value can still pass +/-10 %: compare channel twins (pair parts by sheet offset or by ref parity, require a consistent net mapping).

## Crops and scale
- Crops are named in the project's `crops.yaml` (rect on the upright PDF page). A crop must contain every wire of the nets it scores: board wires often leave a region and come back. Keep crop edges off box outlines and dashed borders (they short pin wires).
- Sheets are not all drawn at one scale. Templates and pixel thresholds were set at the reference scale (Marantz PDF 4); a crop's `scale:` shrinks it to that before detect/ocr/trace, and `to_ir` maps boxes back to page px, so corrections stay in page coordinates.
- `n_refs: true` on a crop keeps real N refs (QN01) when the project sets `ocr.module_ref_letter`.

## Testbenches and systems
- Testbenches name nets by pin (`net: JM11.3`), set `ground: <pin>`, list `sources`, `loads`, `cut:` groups (refs + why, or `region: [x0, y0, x1, y1]` page px with optional `keep:`), `bind:` (a pin the crop does not show, e.g. an op-amp section's supply drawn on its sibling), `leaves:` (a wire running off the crop: net left open and noted, not an ERC error), `params`, `tolerance`. `netlist.prepare` binds them to an IR, so one bench drives a golden and an extracted IR. Cuts and printed voltages left without a simulated net are reported, never silent.
- Netlist: a node reached only through capacitors gets 1 Gohm to ground (`RDC_<node>`, commented); `sim.run_op` raises when ngspice reports that source stepping failed.
- Systems (`systems/*.yaml`): boards (IR + own testbench), `links` (`board:pin` = `board:pin`, merged with a DSU, ground wins), extra `ground` pins, `loads`, `outputs`, `cases` (params, AC sources, checks `gain`, `flat`, `zin`, `relative`, `riaa`). Nets and refs are prefixed with the board (`p501_n12`, `Rp501_R501`; source names get a suffix since the first letter is the SPICE element type). Board sources/loads on linked nets are dropped and reported. The chain's DC point is validated against every board's printed voltages.

## Values (bom)
- The typed parts list beats schematic OCR; common parts (R***/C***) keep the schematic value; a ganged pot row ("250KΩ x2 + 2KΩ x2") only confirms a schematic value. Semiconductor values carry the rank only when the list gives one rank ("2SK369 (BL)" -> `2SK369-BL`), matching `models/map.yaml` keys. Bare schematic capacitor numbers: decimal = uF, integer = pF, electrolytic = uF.
- Rows that differ per sales variant are resolved by `project.yaml variant`; without one they are reported as errors, never guessed.
- A correction may keep a schematic value against the list with `over_list: <reason>`, only with evidence (printed voltages, specs, a measured or photographed part); bom then warns instead of overriding.
- Parts list parsing: tables found by header words; rows anchored by the part-number regex; a ref cell may carry the safety mark ("▲ G851"), stripped; "A***" is the R*** common-parts marker misread.

## OCR
- Read in tiles of <= 1800 px (RapidOCR shrinks anything above 2000 px, which made recall crop-dependent), at scale 0.75 on 600 ppi scans.
- Fused labels ("-0.04V 10/50V") are split into words; "0V" is a voltage, other integer volts are capacitor ratings. A ref read with a leading "0" ("0705") may be a diode or a transistor; the symbol decides.

## Symbols (symbols/library.yaml)
- Pins per type with `template_pins` (lead ends in the unrotated template), `swappable` pins named by page position, `optional_pins`, `may_float`, `pin_reach`/`rim` for terminal circles, `drawings:` for a second way a symbol is drawn (`<type>@2.png`, its own pins), `spice:` letter + pin order or an `expand:` (potentiometer, switch, rotary, center_tap, series_rc).
- Non-components: `ground`, `connector_pin` (stacked into connectors, numbered from the top/left; `back:` wall lets a wire pass through), `rca_jack` (sockets of one block become one connector T1/S1/T2/S2), `warning` safety mark (erased, straight wires through it kept).
- `CONTAINERS` in detect (led, darlington, photocoupler, resistor_ct...): a detection lying mostly inside one is dropped. A potentiometer that clears its threshold beats an overlapping resistor (`NMS_BONUS`).
- Op-amp polarity is read from the drawn signs, not the match score (`trace.fix_opamp_polarity`); the drawn supply pin is named V+/V- from the sign of the printed voltage on its net (`name_supplies`). One section of a dual draws one supply pin; the other comes from the sibling section.
- Switch poles: param `<base ref>: drawn | other` (spdt), `1..6 | drawn` (rotary). Terminal circles own only wires that END at them.

## Trace rules (each from a real failure)
- A pin's lead running straight on past a perpendicular wire with no dot crosses it (`_drop_crossings`), as does a wire end whose stroke continues across a through wire in the scan (`_hidden_crossing`).
- Pins of two parts drawn lead to lead within 30 px on one stroke are joined (`_touching_pins`); collinear wires <= 6 px apart are one wire (`DROP_TINY`, hairline print breaks).
- Two wires meet only if any gap between them wider than 3 px is inked (`_inked_meeting`).
- Print dropouts are bridged only between two collinear pieces each >= 100 px with ink specks inside the gap, no crossing wire and no symbol inside (`_dropout_bridges`).
- A connector pin joins every stack it sits beside; stacks are sorted along their axis. A header with wires on both sides passes each pin through (`_through_headers`, wire end within 8 px of the bracket's back wall).

## Detection thresholds
Set on the reference scan, each lowering checked for new hits on every existing crop: resistor 0.72, capacitor 0.70, capacitor_polarized 0.70, connector_pin 0.70 (comments in `detect.py` give the evidence).

## Open decisions
- Vision backend: local (template matching + RapidOCR); revisit an API model if a manual defeats it.
- Symbol library format (own YAML vs KiCad symbols).
- `stitch` for sheets split across pages (not needed for the reference project).
