"""A project: one service manual and everything read off it.

A project folder holds `project.yaml` (settings that depend on the manual) and the data
the tools read and write, all at fixed places relative to the folder:

    project.yaml       name, PDF, sales variant, parts-list format, OCR rules
    crops.yaml         named page regions to extract
    corrections/       human fixes per crop (proposed / confirmed)
    testbenches/       rails, cuts and switch positions per board
    systems/           board chains (system command)
    golden/            hand-built reference IRs (optional)
    symbols/ models/   optional overlays of the shared symbol library and SPICE models
    data/ pages/ out/  the PDF, extracted page images, generated output (not in git)

Paths written inside project files (an IR's `meta.source`, a system file's `ir:` and
`tb:`) are relative to the project folder.

The current project comes from `use()` (the CLI's --project), else $S2S_PROJECT, else
the nearest folder holding project.yaml at or above the working directory.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENGINE_ROOT = Path(__file__).resolve().parents[2]  # shared symbols/ and models/
FILE = "project.yaml"
ENV = "S2S_PROJECT"


@dataclass
class Project:
    root: Path
    cfg: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        root = Path(path).resolve()
        if root.name == FILE:
            root = root.parent
        f = root / FILE
        if not f.exists():
            raise FileNotFoundError(f"{f}: not a schem2spice project (no {FILE})")
        return cls(root, yaml.safe_load(f.read_text()) or {})

    # --- settings -------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.cfg.get("name", self.root.name)

    @property
    def variant(self) -> str | None:
        """Sales variant code of the unit being modelled (parts-list rows like [/22B])."""
        return self.cfg.get("variant")

    @property
    def parts_pages(self) -> list[int]:
        return list(self.cfg.get("parts_list", {}).get("pages", []))

    @property
    def part_number(self) -> re.Pattern | None:
        """Pattern of the manufacturer's part-number column, which anchors parts-list rows."""
        p = self.cfg.get("parts_list", {}).get("part_number")
        return re.compile(p) if p else None

    @property
    def module_ref_letter(self) -> str | None:
        """Letter of module refs (Marantz "QM11"): an OCR'd "QN13" is read as "QM13"."""
        return self.cfg.get("ocr", {}).get("module_ref_letter")

    # --- paths ----------------------------------------------------------------
    @property
    def pdf(self) -> Path | None:
        p = self.cfg.get("pdf", {}).get("path")
        return self.root / p if p else None

    @property
    def pdf_sha256(self) -> str | None:
        return self.cfg.get("pdf", {}).get("sha256")

    pages_dir = property(lambda self: self.root / "pages")
    out_dir = property(lambda self: self.root / "out")
    crops_file = property(lambda self: self.root / "crops.yaml")
    corrections_dir = property(lambda self: self.root / "corrections")
    testbenches_dir = property(lambda self: self.root / "testbenches")
    systems_dir = property(lambda self: self.root / "systems")
    cache_dir = property(lambda self: self.root / "out" / "cache")

    @property
    def symbol_dirs(self) -> list[Path]:
        """Project overlay first, then the shared library."""
        return [d for d in (self.root / "symbols", ENGINE_ROOT / "symbols") if d.is_dir()]

    @property
    def model_dirs(self) -> list[Path]:
        return [d for d in (self.root / "models", ENGINE_ROOT / "models") if d.is_dir()]

    def page(self, n: int) -> Path:
        return self.pages_dir / f"page-{n:02d}.png"

    def path(self, rel: str | Path) -> Path:
        """A path written in a project file, relative to the project folder."""
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def resolve(self, rel: str | Path) -> Path:
        """A path given on the command line: as typed if it exists, else in the project."""
        p = Path(rel)
        return p if p.is_absolute() or p.exists() else self.root / p

    def crops(self) -> dict[str, Any]:
        if not self.crops_file.exists():
            return {}
        return yaml.safe_load(self.crops_file.read_text()) or {}

    def read_page(self, n: int):
        import cv2

        img = cv2.imread(str(self.page(n)), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"{self.page(n)}: run `schem2spice ingest --project {self.root}` first")
        return img

    def pdf_status(self) -> str:
        """'ok', 'missing', 'unchecked' (no hash on file) or 'mismatch'."""
        if self.pdf is None or not self.pdf.exists():
            return "missing"
        if not self.pdf_sha256:
            return "unchecked"
        return "ok" if sha256(self.pdf) == self.pdf_sha256 else "mismatch"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find(start: Path | None = None) -> Path | None:
    d = (start or Path.cwd()).resolve()
    for p in (d, *d.parents):
        if (p / FILE).exists():
            return p
    return None


_current: Project | None = None


def use(path: str | Path | Project) -> Project:
    global _current
    _current = path if isinstance(path, Project) else Project.load(path)
    return _current


def current() -> Project:
    global _current
    if _current is None:
        where = os.environ.get(ENV) or find()
        if where is None:
            raise RuntimeError(
                f"no project: pass --project DIR, set ${ENV}, or run inside a folder with {FILE} "
                f"(new one: `schem2spice init-project NAME --pdf FILE`)")
        _current = Project.load(where)
    return _current


def optional() -> Project | None:
    """The current project, or None when there is none (shared resources only)."""
    try:
        return current()
    except RuntimeError:
        return None


def symbol_dirs() -> list[Path]:
    p = optional()
    return p.symbol_dirs if p else [ENGINE_ROOT / "symbols"]


def model_dirs() -> list[Path]:
    p = optional()
    return p.model_dirs if p else [ENGINE_ROOT / "models"]


def model_file(name: str) -> Path:
    """A SPICE library by file name: the project's overlay first, then the shared one."""
    for d in model_dirs():
        if (d / name).exists():
            return d / name
    raise FileNotFoundError(f"model library {name!r} not in {[str(d) for d in model_dirs()]}")


def cache_dir(kind: str) -> Path:
    """Disk cache (OCR results...): in the project's out/cache, else in the user's cache."""
    p = optional()
    return (p.cache_dir if p else Path.home() / ".cache" / "schem2spice") / kind
