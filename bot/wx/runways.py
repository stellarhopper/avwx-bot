"""Runway selection and wind components.

aviationweather.gov gives each runway's alignment in degrees **true**, which is
what METAR winds are in too, so the components need no magnetic conversion.
Which end of the runway the alignment refers to has to be worked out: the
numbers are magnetic, and often decades old. KDEN's 16/34 now points 181° true.
So each end's true heading is whichever of `alignment` and `alignment + 180`
is closer to what its number plus today's variation suggests.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# Surface codes from the airport data: asphalt, concrete, "hard" (outside the
# US), bituminous, and paved. Turf, gravel, dirt, water and the rest are left out.
PAVED = {"A", "C", "H", "B", "P"}

_DESIGNATOR = re.compile(r"^(\d{1,2})([LCR]?)$")


@dataclass(frozen=True, slots=True)
class RunwayEnd:
    name: str  # "30R"
    heading: float  # true
    length_ft: int
    opposite: str  # "12L"


@dataclass(frozen=True, slots=True)
class Choice:
    end: RunwayEnd
    headwind: float  # negative is a tailwind
    crosswind: float  # magnitude
    cross_from: str  # "L" | "R" | ""
    gust_headwind: float | None
    gust_crosswind: float | None


def magnetic_variation(raw) -> float:
    """"09E" → +9, "12W" → -12, "0" → 0. East is positive."""
    text = str(raw or "0").strip().upper()
    match = re.match(r"^(\d+(?:\.\d+)?)([EW]?)$", text)
    if not match:
        return 0.0
    value = float(match.group(1))
    return -value if match.group(2) == "W" else value


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def runway_ends(runways: list[dict], magdec) -> list[RunwayEnd]:
    """Both ends of every paved runway, with true headings."""
    var = magnetic_variation(magdec)
    out = []
    for r in runways:
        if (r.get("surface") or "").upper() not in PAVED or r.get("alignment") is None:
            continue
        names = str(r.get("id", "")).split("/")
        if len(names) != 2:
            continue
        m = _DESIGNATOR.match(names[0])
        if not m:
            continue
        align = float(r["alignment"]) % 360
        guess = (int(m.group(1)) * 10 + var) % 360
        first = align if _angle_diff(align, guess) <= _angle_diff((align + 180) % 360, guess) else (align + 180) % 360
        try:
            length = int(str(r.get("dimension", "0x0")).split("x")[0])
        except ValueError:
            length = 0
        out.append(RunwayEnd(names[0], first, length, names[1]))
        out.append(RunwayEnd(names[1], (first + 180) % 360, length, names[0]))
    return out


def components(wind_dir: float, speed: float, heading: float) -> tuple[float, float]:
    """(headwind, crosswind) for a wind from `wind_dir` on a runway heading `heading`.

    Crosswind is positive when the wind comes from the right of the nose.
    """
    angle = math.radians(wind_dir - heading)
    return speed * math.cos(angle), speed * math.sin(angle)


def best(ends: list[RunwayEnd], wind_dir: int | None, speed: int | None, gust: int | None) -> Choice | None:
    """The end most into the wind; least crosswind breaks ties, then length.

    None when there's nothing to choose by: no paved runway, calm, or variable.
    """
    if not ends or wind_dir is None or not speed:
        return None

    def score(e: RunwayEnd):
        head, cross = components(wind_dir, speed, e.heading)
        return (round(head), -round(abs(cross)), e.length_ft)

    end = max(ends, key=score)
    head, cross = components(wind_dir, speed, end.heading)
    g_head = g_cross = None
    if gust:
        g_head, g_cross = components(wind_dir, gust, end.heading)
    side = "" if round(abs(cross)) == 0 else ("R" if cross > 0 else "L")
    return Choice(
        end=end,
        headwind=head,
        crosswind=abs(cross),
        cross_from=side,
        gust_headwind=g_head,
        gust_crosswind=abs(g_cross) if g_cross is not None else None,
    )


def describe(choice: Choice) -> str:
    """"RWY 30R: headwind 8, crosswind 6 from the left (G 14)"."""
    head = round(choice.headwind)
    head_text = f"headwind {head}" if head >= 0 else f"tailwind {-head}"
    cross = round(choice.crosswind)
    side = f" from the {'right' if choice.cross_from == 'R' else 'left'}" if cross else ""
    gust = f" (gusts {round(choice.gust_crosswind)})" if choice.gust_crosswind is not None and cross else ""
    return f"RWY {choice.end.name}: {head_text} kt, crosswind {cross} kt{side}{gust}"
