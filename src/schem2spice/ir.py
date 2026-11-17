"""Canonical IR: pydantic schema plus load/save. Every stage reads and writes this."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

BBox = tuple[int, int, int, int]  # x, y, w, h in page pixels (upright page)


class Component(BaseModel):
    ref: str
    type: str  # key into symbols/library.yaml
    value: str
    pins: dict[str, str]  # symbol pin name -> net name
    bbox: BBox | None = None
    confidence: float = 1.0
    tolerance: str | None = None
    note: str | None = None
    # Set by a correction whose schematic value beats the parts list (the list is for
    # another revision; the printed voltages back the schematic). Holds the reason.
    value_over_list: str | None = None


class Net(BaseModel):
    name: str
    label: str | None = None
    pins: list[str] = Field(default_factory=list)  # "REF.PIN"
    bbox: BBox | None = None
    confidence: float = 1.0


class Connector(BaseModel):
    ref: str
    to: str | None = None
    pins: dict[str, str]  # pin number -> net name
    bbox: BBox | None = None
    confidence: float = 1.0
    note: str | None = None


class Annotation(BaseModel):
    net: str
    voltage: float
    unit: str = "V"
    bbox: BBox | None = None
    confidence: float = 1.0
    note: str | None = None


class IR(BaseModel):
    meta: dict[str, Any] = Field(default_factory=dict)
    components: list[Component] = Field(default_factory=list)
    nets: list[Net] = Field(default_factory=list)
    connectors: list[Connector] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)

    def component(self, ref: str) -> Component:
        for c in self.components:
            if c.ref == ref:
                return c
        raise KeyError(ref)


def derive_nets(ir: IR) -> IR:
    """Rebuild `nets[].pins` from component and connector pin maps, keeping labels/bboxes."""
    old = {n.name: n for n in ir.nets}
    pins: dict[str, list[str]] = {}
    for c in ir.components:
        for pin, net in c.pins.items():
            pins.setdefault(net, []).append(f"{c.ref}.{pin}")
    for j in ir.connectors:
        for pin, net in j.pins.items():
            pins.setdefault(net, []).append(f"{j.ref}.{pin}")
    nets = []
    for name in sorted(pins):
        base = old.get(name, Net(name=name))
        nets.append(base.model_copy(update={"pins": sorted(pins[name])}))
    return ir.model_copy(update={"nets": nets})


def load(path: Path | str) -> IR:
    return IR.model_validate_json(Path(path).read_text())


def save(ir: IR, path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = ir.model_dump(exclude_none=True)
    Path(path).write_text(json.dumps(data, indent=2) + "\n")
