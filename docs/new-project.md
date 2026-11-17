# Reverse-engineering a new manual

This walks through a new service manual from PDF to a validated simulation. The
Marantz project (`projects/marantz-pm80mk2`) has a worked example of every file
mentioned here. Commands assume `make install` has run; `schem2spice` is
`.venv/bin/schem2spice`, and `--project projects/my-amp` can be dropped when you
run from inside the project folder or set `S2S_PROJECT`.

## What works best

- **A clean scan** of the schematic: 1-bit or high-contrast, 300 ppi or more. The
  Marantz manual is 600 ppi CCITT; `ingest` extracts embedded images losslessly
  (one image per page is expected).
- **Printed DC voltages** on the schematic. They are what every board is
  validated against; without them you can still build a netlist, but not check it.
- **A typed parts list** with a part-number column. Many values on a schematic are
  missing or shown as common-part codes; the list completes them.

## 1. Create the project

```sh
schem2spice init-project my-amp --pdf ~/manuals/my-amp.pdf
schem2spice ingest --project projects/my-amp        # -> pages/page-NN.png
```

Open the page images and note, in the project README, which PDF page holds which
board, the parts list, and the specifications.

Edit `project.yaml`:

| Key | Meaning |
|---|---|
| `variant` | Sales variant of your unit, as parts-list rows mark it (`[/22B/22G]`). Rows for other variants are ignored; with none, conflicting rows are reported, never guessed. |
| `parts_list.pages` | PDF pages of the electrical parts list. |
| `parts_list.part_number` | Regular expression of the part-number column; it anchors each row (Philips: `^4822\s*\d{3}\s*\d{5}$`). |
| `parts_list.headers` | Optional column header words, if not `REF.`/`DESIG.`, `PART NO.`, `DESCRIPTION`. |
| `ocr.module_ref_letter` | When modules use refs like `QM11`, OCR's `QN11` is read `QM11`. Leave null otherwise. |

## 2. Name crops

Each crop is one board or module on one page, in `crops.yaml`:

```yaml
p501:
  page: 4                      # PDF page
  rect: [6250, 650, 9300, 4500] # x0, y0, x1, y1 in px on pages/page-04.png
  scale: 1.0                   # how much larger this sheet is drawn than the library
  note: P501 volume board, both channels.
```

- A crop must contain **every wire** of the nets it covers: board wires often leave
  a region and come back.
- Keep crop edges off box outlines and dashed board borders: an edge running along
  one can short wires to it.
- `scale`: symbol templates and pixel thresholds were set on a sheet drawn at one
  scale (Marantz page 4). If your sheet's symbols are bigger, set `scale` to the
  ratio (Marantz page 6 is 1.26); the crop is resampled before detection.

## 3. Extract and review

```sh
schem2spice extract --crop p501
schem2spice review --crop p501      # -> out/p501_review.html
```

`extract` writes the IR (`out/p501_extracted.json`) and an overlay of the traced
nets (`out/p501_trace.png`). It prints the number of floating pins: aim for 0.
`review` lists every part that needs a look (unread ref, missing value, low
score, floating pin) with a scan thumbnail.

If a symbol is never detected, the library lacks it: cut a template from one copy
on the scan (see `symbols/library.yaml` for the pin conventions), and test it on
another copy that you did not cut it from. New symbols go in the shared
`symbols/` when they are generic, or in the project's own `symbols/` overlay.

## 4. Correct

Fix what the tools misread in `corrections/p501.yaml`, never in the generated
files. See [corrections.md](corrections.md). Re-run `extract` (`--proposed` to
include unchecked items) until review is clean.

## 5. Values from the parts list

```sh
schem2spice bom --ir out/p501_extracted.json      # -> out/p501_bom.json, out/p501_bom.md
```

The report lists parts still without a value or with a conflict. Map every
semiconductor to a SPICE model in `models/map.yaml` (shared, or the project's
`models/` overlay), with the substitute and why.

## 6. Testbench and simulation

A testbench (`testbenches/p501.yaml`) says what is simulated and how it is driven.
Nets are named by a pin on them (`JM11.3`), so one bench drives any IR of the board:

```yaml
ground: R501.2
sources:   [{name: VP, net: JM11.3, dc: 15.1, note: "+rail (printed 15.1 V)"}]
loads:     [{name: RLOAD, net: J502.1, value: 47k}]
cut:       [{refs: [R525, R526], why: "rail feeds, driven by VP/VN"}]
params:    {R529-2: 0.5, S401: 2}        # pot positions 0..1, switch positions
leaves:    [{pin: R530.2, why: "runs off the crop to the headphone jack"}]
tolerance: {rel: 0.10, abs: 0.05}
```

```sh
schem2spice sim --ir out/p501_bom.json --tb testbenches/p501.yaml --name p501
```

`out/p501_validate.md` compares each printed voltage with the simulated one. A
miss is usually one of: a misread value (compare the left and right channel
twins), a voltage label attached to the wrong wire, or a model.

## 7. Chain the boards

`systems/<name>.yaml` links boards through their connectors, adds the speaker
load, and defines cases (switch/pot settings, AC sources) with spec checks
(`gain`, `flat`, `zin`, `relative`, `riaa`). See
`projects/marantz-pm80mk2/systems/chain.yaml`.

```sh
schem2spice system --file systems/chain.yaml --build
```

`--build` rebuilds every board from the scan with proposed corrections included,
into `out/sys/`; the report is `out/sys/chain_report.md`.

Each case is an ngspice AC sweep (1 Hz to 1 MHz) written to
`out/sys/<case>_response.csv`. A `plots:` section in the system file turns them
into SVG charts and `out/sys/<system>_plots.html` (curves relative to another case,
normalised at a frequency, against the ideal RIAA curve, with spec bands and
markers; see the docstring of `src/schem2spice/plot.py`). Redraw without
simulating: `schem2spice plot --file systems/chain.yaml`.
