"""Stage 1: extract page images losslessly from the scanned PDF.

Each page of the manual is a single embedded raster image. We pull the image
stream out of the PDF (no re-rendering) and write it as a 1-bit PNG, which is
lossless for CCITT-encoded bilevel scans.

Pages carry a /Rotate flag (e.g. 90 on every page of the Marantz PM-80 Mk II manual). We apply it
with a lossless transpose so downstream stages always see the sheet upright.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pymupdf
from PIL import Image


# PDF /Rotate is clockwise; PIL's ROTATE_* constants are counter-clockwise.
_CLOCKWISE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


@dataclass
class PageImage:
    page: int  # 1-based page number
    path: str
    width: int
    height: int
    bpc: int
    colorspace: str
    filter: str
    dpi: int | None
    rotation: int  # /Rotate applied, degrees clockwise


def extract_pages(pdf: Path, out_dir: Path) -> list[PageImage]:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf)
    results: list[PageImage] = []
    for pno, page in enumerate(doc, start=1):
        images = page.get_images(full=True)
        if len(images) != 1:
            raise ValueError(f"page {pno}: expected 1 image, found {len(images)}")
        xref, _smask, width, height, bpc, colorspace, _alt, _name, filt = images[0][:9]
        pix = pymupdf.Pixmap(doc, xref)
        path = out_dir / f"page-{pno:02d}.png"
        img = pix.pil_image()
        if bpc == 1:
            img = img.convert("1", dither=Image.Dither.NONE)
        transpose = _CLOCKWISE.get(page.rotation % 360)
        if transpose is not None:
            img = img.transpose(transpose)
        img.save(path, optimize=True)
        # Placement width in points -> effective resolution of the scan.
        rect = page.get_image_rects(xref)[0]
        dpi = round(width / (rect.width / 72)) if rect.width else None
        results.append(
            PageImage(pno, str(path), img.width, img.height, bpc, colorspace, filt, dpi,
                      page.rotation)
        )
    (out_dir / "index.json").write_text(json.dumps([asdict(r) for r in results], indent=2))
    return results


if __name__ == "__main__":  # same as `schem2spice ingest`
    import sys

    from .cli import main

    raise SystemExit(main(["ingest", *sys.argv[1:]]))
