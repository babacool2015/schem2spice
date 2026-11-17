# Corrections

The tools misread things: a ref OCR could not read, a value read "89" for "68", a
voltage label attached to the wire next to the right one. Every fix lives in
`corrections/<crop>.yaml` of the project and is re-applied on each `extract`, so a
generated file is never edited by hand.

## Status

Each item has a `status`:

- **`proposed`**: read off the scan by a tool or an assistant, not checked by a person.
- **`confirmed`**: checked by a person against the scan (or the real board).

`extract` applies confirmed items only; `extract --proposed` and `system --build`
apply both. Confirm an item by changing its status; keep the `why`.

## Components

Items match a detection by symbol type and page position (within 40 px of `at`,
in page pixels of `pages/page-NN.png`):

```yaml
components:
  - {page: 4, at: [6392, 3507], type: potentiometer, ref: R529-4, value: 250k, status: confirmed,
     why: 'Label "R529-4" left of the symbol; OCR read "R529".'}
```

Settable: `ref`, `value`, `tolerance`. A value that contradicts the parts list is
overridden by the list unless the item carries `over_list: <evidence>` (for
example printed voltages that only the schematic value explains, or a measured
part). `bom` then keeps it and warns.

`include: [other-crop]` at the top pulls in another crop's items first, so a wider
crop reuses the checked fixes of a narrower one.

## Voltage labels

Printed voltages attach to the nearest wire. When one lands on the wrong wire, the
net shows two printed voltages that cannot both be right; `review` and ERC flag it.

```yaml
annotations:
  - {page: 4, at: [4353, 3425], voltage: 0.0, net_pin: Q401-2.+, status: proposed,
     why: 'The "0V" marks the + input, not the -17.2 V supply pin beside it.'}
  - {page: 10, at: [3154, 2688], voltage: 20.0, delete: true, status: proposed,
     why: '"20.0V" is a zener rating, not a node voltage.'}
```

`net_pin` moves the label to that pin's net, `set_voltage` fixes its value, and
`delete` removes it.

## Checking

- Compare channel twins: a misread common-part value can still pass a ±10 %
  voltage check, but the left and right channels must simulate alike.
- Prefer evidence from printed voltages over assumptions; record it in `why`.
