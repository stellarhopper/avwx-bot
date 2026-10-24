"""Runway designator numerals and letters, from FAA AC 150/5340-1M Figure A-6.

The outlines are the advisory circular's own artwork, lifted from the PDF's
vector paths by scripts/runway_font_from_ac.py (an AC is a US government
work, so they're public domain). Units are feet: each character is 60 tall,
with the origin at its top-left, seen from the approach the way a pilot reads
it. A character is a list of rings: the first is its outline, and any others
are holes (in 0, 6, 8, 9 and R).

Layout follows the figure's notes:

* Numerals sit 15 ft apart, except "11", whose left edges are 27 ft apart.
* A single "1" carries a base bar so it can't be mistaken for the centreline.
* FAA designators drop a leading zero ("9", not "09"). ICAO keeps it, so the
  caller says which convention applies.
"""

from __future__ import annotations

from collections.abc import Callable

Point = tuple[float, float]

HEIGHT = 60.0
SPACING = 15.0  # between the edges of adjacent numerals
ELEVEN_ADVANCE = 27.0  # left edge to left edge, for "11" only

GLYPHS: dict[str, list[list[tuple[float, float]]]] = {
    "1": [
        [(2.68, 60.04), (2.68, 10.19), (0.04, 10.19), (0.04, 1.81), (2.68, -0.03), (8.25, -0.03), (8.25, 60.04)],
    ],
    "2": [
        [(-0.04, 60.04), (-0.04, 42.20), (14.59, 19.54), (14.59, 10.53), (5.69, 10.53), (5.69, 16.38), (-0.04, 16.38), (-0.04, -0.03), (20.26, -0.03), (20.26, 20.40), (5.69, 43.06), (5.69, 50.11), (20.26, 50.11), (20.26, 60.04)],
    ],
    "3": [
        [(-0.02, 60.04), (-0.02, 50.11), (14.67, 50.11), (14.67, 34.22), (2.85, 24.12), (14.67, 13.69), (14.67, 10.53), (-0.02, 10.53), (-0.02, -0.03), (20.29, -0.03), (20.29, 7.03), (20.35, 6.97), (20.35, 16.95), (12.66, 24.12), (20.35, 30.72), (20.35, 60.04)],
    ],
    "4": [
        [(14.27, 60.04), (14.27, 46.27), (-0.02, 46.27), (-0.02, 36.06), (9.22, -0.03), (15.30, -0.03), (6.00, 36.06), (14.27, 36.06), (14.27, 17.76), (19.94, 17.76), (19.94, 36.06), (25.39, 36.06), (25.39, 46.27), (19.94, 46.27), (19.94, 60.04)],
    ],
    "5": [
        [(0.05, 60.04), (0.05, 49.37), (14.85, 49.37), (14.85, 30.26), (0.05, 30.26), (0.05, -0.03), (20.58, -0.03), (20.58, 10.59), (5.78, 10.59), (5.78, 19.59), (20.58, 19.59), (20.58, 60.04)],
    ],
    "6": [
        [(-0.04, 59.96), (-0.04, 12.63), (14.58, -0.04), (15.39, -0.04), (15.39, 6.44), (5.63, 15.62), (5.63, 24.97), (20.26, 24.97), (20.26, 59.96)],
        [(5.63, 49.87), (14.58, 49.87), (14.58, 35.07), (5.63, 35.07)],
    ],
    "7": [
        [(2.11, 59.96), (15.08, 9.88), (0.04, 9.88), (0.04, -0.04), (23.45, -0.04), (23.45, 0.24), (20.93, 9.88), (7.96, 59.96)],
    ],
    "8": [
        [(0.09, 59.96), (0.09, 29.79), (4.91, 25.94), (0.04, 21.76), (0.09, 21.64), (0.09, -0.04), (20.63, -0.04), (20.63, 21.76), (15.81, 25.94), (20.63, 29.79), (20.63, 59.96)],
        [(5.89, 50.04), (14.84, 50.04), (14.84, 31.97), (5.89, 31.97)],
        [(5.89, 21.30), (14.84, 21.30), (14.84, 10.46), (5.89, 10.46)],
    ],
    "9": [
        [(4.83, 59.96), (4.83, 53.19), (14.70, 44.19), (14.70, 35.52), (0.01, 35.52), (0.01, -0.04), (20.38, -0.04), (20.38, 47.57), (5.63, 59.96)],
        [(5.69, 25.60), (14.70, 25.60), (14.70, 9.88), (5.69, 9.88)],
    ],
    "0": [
        [(-0.02, -0.04), (20.41, -0.04), (20.41, 59.96), (-0.02, 59.96)],
        [(5.72, 50.84), (14.67, 50.84), (14.67, 11.37), (5.72, 11.37)],
    ],
    "L": [
        [(-0.01, 60.03), (-0.01, -0.03), (5.67, -0.03), (5.67, 49.65), (20.47, 49.65), (20.47, 60.03)],
    ],
    "R": [
        [(14.71, 60.03), (9.83, 34.27), (5.93, 34.27), (5.93, 60.03), (0.02, 60.03), (0.02, -0.03), (20.68, -0.03), (20.68, 34.27), (15.80, 34.27), (20.68, 60.03)],
        [(5.93, 24.18), (14.71, 24.18), (14.71, 10.24), (5.93, 10.24)],
    ],
    "C": [
        [(-0.01, 60.03), (-0.01, -0.03), (20.47, -0.03), (20.47, 14.31), (14.67, 14.31), (14.67, 10.18), (5.72, 10.18), (5.72, 49.71), (14.67, 49.71), (14.67, 45.52), (20.47, 45.52), (20.47, 60.03)],
    ],
    "1_single": [
        [(-0.00, 60.03), (-0.00, 49.65), (7.17, 49.65), (7.17, 10.18), (4.53, 10.18), (4.53, 1.80), (7.17, -0.03), (12.73, -0.03), (12.73, 49.65), (19.96, 49.65), (19.96, 60.03)],
    ],
}


def _width(name: str) -> float:
    return max(x for ring in GLYPHS[name] for x, _ in ring)


def glyph_names(text: str) -> list[str]:
    """The glyphs to paint for a designator row ("9", "34", "L")."""
    if text == "1":
        return ["1_single"]
    return [ch for ch in text if ch in GLYPHS]


def designator(label: str, *, faa: bool) -> tuple[str, str]:
    """("34", "L") from "34L"; FAA style drops a leading zero ("09" → "9")."""
    digits = label.rstrip("LCR")
    letter = label[len(digits):]
    if faa and digits.isdigit():
        digits = str(int(digits))
    return digits, letter


def row_width(text: str) -> float:
    names = glyph_names(text)
    if not names:
        return 0.0
    if text == "11":
        return ELEVEN_ADVANCE + _width("1")
    return sum(_width(n) for n in names) + SPACING * (len(names) - 1)


def draw(
    polygon: Callable[[list[Point], bool], None],
    text: str,
    place: Callable[[float, float], Point],
    scale: float,
) -> None:
    """Paint one row of characters, centred on x = 0.

    `place(x, y)` maps the row's coordinates (x across, centred; y down from
    the top of the row) onto the canvas, and `scale` is canvas units per foot.
    `polygon(points, is_hole)` fills one ring.
    """
    names = glyph_names(text)
    x = -row_width(text) / 2
    for name in names:
        for i, ring in enumerate(GLYPHS[name]):
            polygon([place((x + gx) * scale, gy * scale) for gx, gy in ring], i > 0)
        x += ELEVEN_ADVANCE if text == "11" else _width(name) + SPACING
