"""Which area hazards and NWS alerts matter to a set of airports.

Shared by the brief, the alert watcher and the mountain board, so "near" means
the same thing everywhere: a hazard polygon within the radius of any airport,
and in force now or within the look-ahead window.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from functools import cache

from .config import ROOT
from .sources.awc import Hazard
from .sources.nws import Alert
from .wx import geo

LABELS = {
    "CONVECTIVE": "Convective SIGMET",
    "TURB": "Severe turbulence SIGMET",
    "ICE": "Icing",
    "IFR": "IFR conditions",
    "MT_OBSC": "Mountains obscured",
    "TURB-LO": "Turbulence below FL180",
    "TURB-HI": "Turbulence above FL180",
    "LLWS": "Low-level wind shear",
    "SFC_WND": "Surface wind 30 kt+",
    "CWA": "Center Weather Advisory",
    "ASH": "Volcanic ash SIGMET",
    "IFR_MT_OBSC": "IFR / mountains obscured",
}

# How bad each kind is, for sorting and colour. 3 is the top of the scale.
WEIGHT = {
    "CONVECTIVE": 3,
    "TURB": 3,
    "ASH": 3,
    "CWA": 2,
    "LLWS": 2,
    "ICE": 2,
    "TURB-LO": 2,
    "MT_OBSC": 2,
    "IFR": 1,
    "TURB-HI": 1,
    "SFC_WND": 1,
}


def label(h: Hazard) -> str:
    if h.source == "CONVECTIVE":
        return LABELS["CONVECTIVE"]
    if h.source == "CWA":
        return LABELS["CWA"]
    if h.source == "SIGMET" and h.hazard in ("TURB", "ICE", "ASH"):
        return f"{h.hazard.title()} SIGMET"
    return LABELS.get(h.hazard, h.hazard.replace("_", " ").title())


def weight(h: Hazard) -> int:
    if h.source in ("CONVECTIVE", "SIGMET"):
        return 3
    return WEIGHT.get(h.hazard, 1)


def detail(h: Hazard) -> str:
    bits = []
    if h.severity and h.source == "GAIRMET":
        bits.append(h.severity)
    if h.base and h.top:
        bits.append(f"{_level(h.base)}–{_level(h.top)}")
    elif h.top:
        # Convective SIGMETs give tops only; they don't have a base.
        bits.append(f"tops {_level(h.top)}")
    elif h.base:
        bits.append(f"above {_level(h.base)}")
    if h.due_to:
        bits.append(h.due_to.lower())
    if h.movement:
        bits.append(f"moving {h.movement}")
    return ", ".join(bits)


def _level(value: str) -> str:
    """G-AIRMET levels come as hundreds of feet ("170"), or words (SFC, FZL)."""
    if value.isdigit():
        n = int(value)
        return f"FL{n:03d}" if n >= 180 else f"{n * 100:,} ft"
    return value


def in_window(h: Hazard, now: datetime, hours: float) -> bool:
    start = h.valid_from or now
    end = h.valid_to or (now + timedelta(hours=hours))
    return start <= now + timedelta(hours=hours) and end >= now


def near(
    hazards: Iterable[Hazard],
    points: list[tuple[float, float]],
    radius_nm: float,
    now: datetime,
    *,
    hours: float = 6,
) -> list[Hazard]:
    """Hazards touching any point's radius, in force within `hours`, worst first.

    G-AIRMETs come as snapshots at several forecast hours; only the first
    matching snapshot of each hazard is kept, so one turbulence area isn't
    listed three times.
    """
    seen: set[tuple[str, str, str]] = set()
    out = []
    for h in sorted(hazards, key=lambda h: h.valid_from or now):
        if len(h.polygon) < 3 or not in_window(h, now, hours):
            continue
        if not geo.near_any(points, h.polygon, radius_nm):
            continue
        key = (h.source, h.hazard, h.due_to or "")
        if h.source == "GAIRMET" and key in seen:
            continue
        seen.add(key)
        out.append(h)
    return sorted(out, key=lambda h: (-weight(h), label(h)))


def alert_near(alert: Alert, points: list[tuple[float, float]], radius_nm: float, polygons=None) -> bool:
    for polygon in polygons if polygons is not None else alert.polygons:
        if geo.near_any(points, polygon, radius_nm):
            return True
    return False


# ------------------------------------------------------------- winds aloft


@cache
def fb_sites() -> dict[str, tuple[float, float]]:
    data = json.loads((ROOT / "data" / "fb_sites.json").read_text(encoding="utf-8"))
    return {k: (v[0], v[1]) for k, v in data.items()}


def nearest_fb_site(point: tuple[float, float], available: Iterable[str] | None = None) -> str | None:
    sites = fb_sites()
    pool = [s for s in (available if available is not None else sites) if s in sites]
    if not pool:
        return None
    return min(pool, key=lambda s: geo.distance_nm(point, sites[s]))
