"""Turn a Power BI PDF export into the dashboard screenshots used by the README.

In Power BI Desktop choose File > Export > Export to PDF, then run:

    python export_screenshots.py <path to the exported pdf>

Each PDF page becomes one PNG in powerbi/screenshots/, named after the report
page, with the white margin Power BI adds around the page cropped away. Pages
are matched by position, so the PDF must contain the five pages in report order.
"""
import sys
from pathlib import Path

import pymupdf

NAMES = [
    "01-temporada",
    "02-partidos",
    "03-jugadores",
    "04-en-vivo-y-prediccion",
    "05-salud-del-pipeline",
]
OUT = Path(__file__).resolve().parent.parent / "screenshots"
ZOOM = 2.5  # 996 x 576 pt per page -> about 2490 x 1440 px
WHITE = 250  # a channel value above this counts as the white margin


def content_box(pixmap):
    """Bounding box of everything that is not the white page margin."""
    width, height, samples = pixmap.width, pixmap.height, pixmap.samples
    step = 4  # sampling every 4th pixel is plenty to find an edge

    def dark(x, y):
        i = (y * width + x) * 3
        return min(samples[i], samples[i + 1], samples[i + 2]) < WHITE

    xs = [x for x in range(0, width, step) for y in range(0, height, step * 8) if dark(x, y)]
    ys = [y for y in range(0, height, step) for x in range(0, width, step * 8) if dark(x, y)]
    return min(xs), min(ys), max(xs) + step, max(ys) + step


def main(pdf_path):
    document = pymupdf.open(pdf_path)
    if len(document) != len(NAMES):
        sys.exit(f"Expected {len(NAMES)} pages, the PDF has {len(document)}")
    OUT.mkdir(exist_ok=True)
    for page, name in zip(document, NAMES):
        full = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=False)
        left, top, right, bottom = content_box(full)
        clip = pymupdf.Rect(left, top, right, bottom) / ZOOM
        cropped = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), clip=clip, alpha=False)
        target = OUT / f"{name}.png"
        cropped.save(target)
        print(f"{target.name}: {cropped.width} x {cropped.height}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
