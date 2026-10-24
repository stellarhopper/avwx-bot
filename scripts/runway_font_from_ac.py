#!/usr/bin/env python3
"""Extract the runway designator outlines from FAA AC 150/5340-1M, Figure A-6.

The figure (page A-6, PDF page 124) is vector artwork. This converts that page
to SVG with pdftocairo, picks out the filled glyph shapes by position, and
prints them as polygon data in feet: 60 tall with the origin at the
glyph's top-left. The output is pasted into bot/render/runway_font.py.

    python scripts/runway_font_from_ac.py ~/Downloads/150-5340-1M-Chg-1-Airport-Markings.pdf

Advisory circulars are US government works, so the shapes are public domain.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

PAGE = 124
# Each glyph's left edge on the page (PDF points) and its row's top. Rows are
# the figure's three lines of characters; positions read from the SVG.
LAYOUT = {
    "1": (154.8, 137.2),
    "2": (219.0, 137.2),
    "3": (284.0, 137.2),
    "4": (377.9, 137.2),
    "5": (453.4, 137.2),
    "6": (164.7, 282.1),
    "7": (224.3, 282.1),
    "8": (296.2, 282.1),
    "9": (382.4, 282.1),
    "0": (456.1, 282.1),
    "L": (210.2, 416.0),
    "R": (281.2, 416.0),
    "C": (351.6, 416.0),
    "1_single": (449.1, 416.0),
}
ROW_HEIGHT = 64.1  # points per 60 ft, measured from the figure


def subpaths(d: str) -> list[list[tuple[float, float]]]:
    tokens = re.findall(r"[MLCZ]|-?[\d.]+", d)
    out, cur, i = [], None, 0
    while i < len(tokens):
        t = tokens[i]
        if t == "M":
            cur = [(float(tokens[i + 1]), float(tokens[i + 2]))]
            out.append(cur)
            i += 3
        elif t == "L":
            cur.append((float(tokens[i + 1]), float(tokens[i + 2])))
            i += 3
        elif t == "C":
            raise SystemExit("unexpected curve in a glyph outline")
        else:
            i += 1
    return [s for s in out if len(s) >= 3]


def main(pdf: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svg_path = Path(tmp) / "page.svg"
        subprocess.run(
            ["pdftocairo", "-svg", "-f", str(PAGE), "-l", str(PAGE), pdf, str(svg_path)], check=True
        )
        svg = svg_path.read_text()
    shapes = []
    for attrs in re.findall(r"<path ([^>]*)/>", svg):
        d = re.search(r' d="([^"]*)"', attrs)
        if d is None or 'fill="none"' in attrs:
            continue
        subs = subpaths(d.group(1))
        if not subs:
            continue
        xs = [x for s in subs for x, _ in s]
        ys = [y for s in subs for _, y in s]
        if max(ys) - min(ys) > 60 and max(xs) - min(xs) > 5 and max(xs) - min(xs) < 30:
            shapes.append((min(xs), min(ys), subs))

    scale = 60 / ROW_HEIGHT
    print("GLYPHS: dict[str, list[list[tuple[float, float]]]] = {")
    for name, (left, top) in LAYOUT.items():
        match = [s for s in shapes if abs(s[0] - left) < 0.5 and abs(s[1] - top) < 0.5]
        if len(match) != 1:
            raise SystemExit(f"{name}: found {len(match)} shapes at ({left}, {top})")
        _, _, subs = match[0]
        print(f"    {name!r}: [")
        for s in subs:
            pts = ", ".join(f"({(x - left) * scale:.2f}, {(y - top) * scale:.2f})" for x, y in s)
            print(f"        [{pts}],")
        print("    ],")
    print("}")


if __name__ == "__main__":
    main(sys.argv[1])
