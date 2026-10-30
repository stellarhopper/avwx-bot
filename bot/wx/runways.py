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


# Runways whose heading is within this many degrees of the best-aligned one
# count as equally good, and the longest of them wins. 5° changes the
# crosswind by under 1 kt in a 10 kt wind, which nobody would pick a shorter
# runway for.
SAME_WIND_DEG = 5
# At or below this (steady or gust, whichever is higher), wind doesn't decide
# the runway: the longest one does.
LIGHT_WIND_KT = 3
# Below this, the longest runway is worth showing alongside the best-aligned
# one: the wind is light enough that it's a real option.
SHOW_LONGEST_BELOW_KT = 10


def total_wind(speed: int | None, gust: int | None) -> int:
    return max(speed or 0, gust or 0)


def _choice(end: RunwayEnd, wind_dir: int | None, speed: int | None, gust: int | None) -> Choice:
    if wind_dir is None or not speed:
        return Choice(end, 0.0, 0.0, "", None, None)
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


def for_end(end: RunwayEnd, wind_dir: int | None, speed: int | None, gust: int | None) -> Choice:
    """Wind components on a runway end the pilot has chosen."""
    return _choice(end, wind_dir, speed, gust)


def longest(ends: list[RunwayEnd], wind_dir: int | None, speed: int | None, gust: int | None) -> Choice | None:
    """The longest runway, used in whichever direction is more into the wind."""
    if not ends:
        return None
    top = max(e.length_ft for e in ends)
    pool = [e for e in ends if e.length_ft == top]
    if wind_dir is None or not speed:
        return _choice(pool[0], None, None, None)
    return _choice(min(pool, key=lambda e: _angle_diff(wind_dir, e.heading)), wind_dir, speed, gust)


def best(ends: list[RunwayEnd], wind_dir: int | None, speed: int | None, gust: int | None) -> Choice | None:
    """The runway to use.

    In light wind (LIGHT_WIND_KT or less, calm or variable included) that's
    simply the longest. Otherwise it's the longest of the ends within
    SAME_WIND_DEG of the most into-wind, then the better-aligned of those if
    lengths tie. None when the wind is variable and stronger than light, or
    there's no paved runway.
    """
    if not ends:
        return None
    if total_wind(speed, gust) <= LIGHT_WIND_KT:
        return longest(ends, wind_dir, speed, gust)
    if wind_dir is None:
        return None
    offset = {e: _angle_diff(wind_dir, e.heading) for e in ends}
    closest = min(offset.values())
    pool = [e for e in ends if offset[e] <= closest + SAME_WIND_DEG]
    end = max(pool, key=lambda e: (e.length_ft, -offset[e]))
    return _choice(end, wind_dir, speed, gust)


def options(ends: list[RunwayEnd], wind_dir: int | None, speed: int | None, gust: int | None) -> list[Choice]:
    """What to show: the chosen runway, plus the longest when the wind is
    under SHOW_LONGEST_BELOW_KT and the longest is a different runway."""
    choice = best(ends, wind_dir, speed, gust)
    if choice is None:
        return []
    out = [choice]
    if total_wind(speed, gust) < SHOW_LONGEST_BELOW_KT:
        long = longest(ends, wind_dir, speed, gust)
        if long and long.end.length_ft > choice.end.length_ft:
            out.append(long)
    return out


def describe(choice: Choice) -> str:
    """"RWY 30R: headwind 8 kt, crosswind 6 kt from the left (gusts 14)"."""
    if not choice.headwind and not choice.crosswind:
        return f"RWY {choice.end.name}: the longest runway, as the wind is calm or light"
    head = round(choice.headwind)
    head_text = f"headwind {head}" if head >= 0 else f"tailwind {-head}"
    cross = round(choice.crosswind)
    side = f" from the {'right' if choice.cross_from == 'R' else 'left'}" if cross else ""
    gust = f" (gusts {round(choice.gust_crosswind)})" if choice.gust_crosswind is not None and cross else ""
    return f"RWY {choice.end.name}: {head_text} kt, crosswind {cross} kt{side}{gust}"


def arrows(choice: Choice) -> str:
    """"↓6 ←8 G16": arrows show which way the wind blows relative to the aircraft.

    ↓ headwind (blowing at you), ↑ tailwind; → a crosswind from the left
    (blowing to the right), ← one from the right, ↔ none.
    """
    head = round(choice.headwind)
    cross = round(choice.crosswind)
    head_text = f"↓{head}" if head >= 0 else f"↑{-head}"
    cross_arrow = {"L": "→", "R": "←"}.get(choice.cross_from, "↔")
    cross_text = f"{cross_arrow}{cross}"
    if choice.gust_crosswind is not None and round(choice.gust_crosswind) > cross:
        cross_text += f" G{round(choice.gust_crosswind)}"
    return f"{head_text}  {cross_text}"
