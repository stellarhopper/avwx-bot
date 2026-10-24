"""Runway designator numerals and letters, drawn as strokes.

There's no freely licensed font for runway markings, but the shapes are
simple. FAA and ICAO designators are tall and narrow, about three times as
high as they are wide, with a uniform stroke and no serifs. So each character
here is a few centre-line strokes on a 1×3 box, drawn with a thick pen.

Coordinates: x across (0 left, 1 right), y down (0 top, 3 bottom), seen from
the approach the way the pilot reads them.
"""

from __future__ import annotations

import math
from collections.abc import Callable

Point = tuple[float, float]
Stroke = list[Point]

STROKE = 0.33  # pen width, in glyph units (glyph is 1 wide)
GAP = 0.45  # between the two digits of a number
L_, R_, T_, B_ = 0.14, 0.86, 0.14, 2.86  # stroke centre-line bounds, inset by half a pen


def _arc(cx: float, cy: float, rx: float, ry: float, a0: float, a1: float, n: int = 14) -> Stroke:
    """Points along an ellipse from angle a0 to a1 (degrees; 0 = right, 90 = down)."""
    return [
        (cx + rx * math.cos(math.radians(a0 + (a1 - a0) * i / n)), cy + ry * math.sin(math.radians(a0 + (a1 - a0) * i / n)))
        for i in range(n + 1)
    ]


def _rot180(strokes: list[Stroke]) -> list[Stroke]:
    return [[(1 - x, 3 - y) for x, y in s] for s in strokes]


_RX = (R_ - L_) / 2
# Closed loops run 20° past their start: Pillow cuts stroke ends square, and
# two square ends meeting at an angle leave a visible notch.
_SIX = [
    _arc(0.5, 0.95, _RX, 0.81, 315, 180) + [(L_, 2.1)],
    _arc(0.5, 2.1, _RX, 0.76, 0, 380, 26),
]

GLYPHS: dict[str, list[Stroke]] = {
    "0": [_arc(0.5, T_ + _RX, _RX, _RX, 180, 360) + _arc(0.5, B_ - _RX, _RX, _RX, 0, 180) + _arc(0.5, T_ + _RX, _RX, _RX, 180, 220, 3)],
    "1": [[(0.22, T_ + 0.4), (0.62, T_), (0.62, B_)]],
    "2": [_arc(0.5, T_ + _RX, _RX, _RX, 190, 360) + [(R_, 0.75), (L_, B_), (R_, B_)]],
    "3": [_arc(0.5, 0.76, _RX, 0.62, 200, 450) + _arc(0.5, 2.1, _RX, 0.76, 270, 520)],
    "4": [[(0.66, B_), (0.66, T_), (L_, 2.05), (R_, 2.05)]],
    "5": [[(R_, T_), (L_, T_), (L_, 1.38)] + _arc(0.5, 2.1, _RX, 0.76, 235, 510)],
    "6": _SIX,
    "7": [[(L_, T_), (R_, T_), (0.36, B_)]],
    "8": [_arc(0.5, 0.78, 0.32, 0.64, 0, 380, 26), _arc(0.5, 2.1, _RX, 0.76, 0, 380, 26)],
    "9": _rot180(_SIX),
    "L": [[(L_, T_), (L_, B_), (R_, B_)]],
    "C": [_arc(0.5, 1.5, _RX, 1.36, 320, 40, 24)],
    "R": [[(L_, B_), (L_, T_), (0.52, T_)] + _arc(0.52, 0.82, 0.34, 0.68, 270, 450) + [(L_, 1.5)], [(0.45, 1.5), (R_, B_)]],
}


def width(text: str) -> float:
    """Width of a run of characters, in glyph units."""
    return len(text) + GAP * (len(text) - 1) if text else 0.0


def draw(
    line: Callable[[list[Point], float], None],
    text: str,
    place: Callable[[float, float], Point],
    scale: float,
) -> None:
    """Draw `text` as one row of glyphs, centred on x = 0.

    `place(x, y)` maps the row's coordinates (x across, centred; y down from
    the top of the row) onto the canvas. `scale` is canvas units per glyph
    unit, and `line(points, width)` draws one stroke.
    """
    x = -width(text) / 2
    for ch in text:
        for stroke in GLYPHS.get(ch, []):
            line([place((x + gx) * scale, gy * scale) for gx, gy in stroke], STROKE * scale)
        x += 1 + GAP
