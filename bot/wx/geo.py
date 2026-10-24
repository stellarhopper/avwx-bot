"""Just enough geometry to ask "does this hazard touch our airports?".

Hazard areas (SIGMETs, G-AIRMETs, NWS warning polygons) are a few hundred
kilometres across at most, so a flat-earth treatment of each polygon is fine.
Distances use the haversine formula because radii are set in nautical miles
and pilots will notice if 25 nm is really 22.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

EARTH_RADIUS_NM = 3440.065

Point = tuple[float, float]  # (lat, lon)


def distance_nm(a: Point, b: Point) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(min(1.0, h)))


def bearing_deg(a: Point, b: Point) -> float:
    """Initial true bearing from a to b."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray casting. The polygon may or may not repeat its first vertex."""
    lat, lon = point
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        if (lat_i > lat) != (lat_j > lat):
            cross = (lon_j - lon_i) * (lat - lat_i) / (lat_j - lat_i) + lon_i
            if lon < cross:
                inside = not inside
        j = i
    return inside


def _segment_distance_nm(p: Point, a: Point, b: Point) -> float:
    """Approximate distance from p to segment ab, projecting locally."""
    scale = math.cos(math.radians(p[0]))
    ax, ay = (a[1] - p[1]) * scale, a[0] - p[0]
    bx, by = (b[1] - p[1]) * scale, b[0] - p[0]
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(cx, cy) * 60  # degrees of latitude → nm


def distance_to_polygon_nm(point: Point, polygon: Sequence[Point]) -> float:
    """0 inside the polygon, otherwise the distance to its nearest edge."""
    if len(polygon) >= 3 and point_in_polygon(point, polygon):
        return 0.0
    if len(polygon) == 1:
        return distance_nm(point, polygon[0])
    return min(
        _segment_distance_nm(point, polygon[i], polygon[(i + 1) % len(polygon)])
        for i in range(len(polygon))
    )


def near_any(points: Iterable[Point], polygon: Sequence[Point], radius_nm: float) -> bool:
    return any(distance_to_polygon_nm(p, polygon) <= radius_nm for p in points)


def bbox(points: Iterable[Point], pad_nm: float) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon) around the points, padded."""
    pts = list(points)
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    pad_lat = pad_nm / 60
    mid = math.radians(sum(lats) / len(lats))
    pad_lon = pad_nm / (60 * max(0.2, math.cos(mid)))
    return (
        round(min(lats) - pad_lat, 3),
        round(min(lons) - pad_lon, 3),
        round(max(lats) + pad_lat, 3),
        round(max(lons) + pad_lon, 3),
    )


def compass(deg: float) -> str:
    points = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return points[int((deg % 360 + 22.5) // 45) % 8]
