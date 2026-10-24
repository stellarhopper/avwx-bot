"""Flight categories, ceilings, and visibility parsing.

The FAA definitions, taking the worse of ceiling and visibility:

    LIFR  ceiling < 500 ft     or visibility < 1 SM
    IFR   ceiling < 1000 ft    or visibility < 3 SM
    MVFR  ceiling <= 3000 ft   or visibility <= 5 SM
    VFR   otherwise

A ceiling is the lowest broken or overcast layer, or a vertical visibility
into an obscuration.
"""

from __future__ import annotations

from collections.abc import Iterable

CATEGORIES = ("VFR", "MVFR", "IFR", "LIFR")
CEILING_COVERS = {"BKN", "OVC", "OVX"}


def parse_visibility(raw) -> float | None:
    """Statute miles from AWC's visibility field: 10, "10+", "6+", "1/2", "1 1/2"."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().rstrip("+").removeprefix("P").removeprefix("M")
    text = text.removesuffix("SM").strip()
    try:
        total = 0.0
        for part in text.split():
            if "/" in part:
                num, den = part.split("/", 1)
                total += float(num) / float(den)
            else:
                total += float(part)
        return total
    except (ValueError, ZeroDivisionError):
        return None


def ceiling(clouds: Iterable[dict] | None, vert_vis: int | None = None) -> int | None:
    """Feet AGL of the lowest BKN/OVC layer or vertical visibility, else None."""
    bases = [
        layer["base"]
        for layer in clouds or ()
        if layer.get("cover") in CEILING_COVERS and layer.get("base") is not None
    ]
    if vert_vis is not None:
        bases.append(vert_vis)
    return min(bases) if bases else None


def category(ceiling_ft: int | None, visibility_sm: float | None) -> str:
    """The flight category. Unknown inputs count as unrestricting."""
    rank = 0
    if ceiling_ft is not None:
        if ceiling_ft < 500:
            rank = max(rank, 3)
        elif ceiling_ft < 1000:
            rank = max(rank, 2)
        elif ceiling_ft <= 3000:
            rank = max(rank, 1)
    if visibility_sm is not None:
        if visibility_sm < 1:
            rank = max(rank, 3)
        elif visibility_sm < 3:
            rank = max(rank, 2)
        elif visibility_sm <= 5:
            rank = max(rank, 1)
    return CATEGORIES[rank]


def worst(categories: Iterable[str]) -> str:
    ranks = [CATEGORIES.index(c) for c in categories if c in CATEGORIES]
    return CATEGORIES[max(ranks)] if ranks else "VFR"
