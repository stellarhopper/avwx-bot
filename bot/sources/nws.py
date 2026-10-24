"""api.weather.gov: NWS alerts and Area Forecast Discussions.

Alerts are fetched once per state and filtered per server, so ten Front Range
servers cost one request a minute rather than ten. A warning without its own
polygon (most watches, some advisories) is located by its forecast zones,
whose outlines are cached for a day since they practically never change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .http import Http

BASE = "https://api.weather.gov"
GEOJSON = "application/geo+json"

TTL_ALERTS = 60
TTL_AFD_LIST = 600
TTL_AFD = 3600
TTL_ZONE = 24 * 3600

_SECTION = re.compile(r"^\.([A-Z][A-Z /]+?)(?:\s*/[^\n]*?)?\.\.\.", re.MULTILINE)


def _iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Alert:
    id: str
    event: str
    headline: str
    description: str
    instruction: str
    area: str
    severity: str
    urgency: str
    certainty: str
    message_type: str  # Alert | Update | Cancel
    sent: datetime | None
    onset: datetime | None
    expires: datetime | None
    ends: datetime | None
    sender: str
    references: tuple[str, ...]
    polygons: tuple[tuple[tuple[float, float], ...], ...]
    zones: tuple[str, ...]

    @property
    def until(self) -> datetime | None:
        """When the hazard itself ends — `ends` if given, else the message's expiry."""
        return self.ends or self.expires

    @classmethod
    def from_feature(cls, f: dict) -> "Alert":
        p = f.get("properties") or {}
        return cls(
            id=p.get("id") or f.get("id") or "",
            event=p.get("event") or "",
            headline=p.get("headline") or "",
            description=p.get("description") or "",
            instruction=p.get("instruction") or "",
            area=p.get("areaDesc") or "",
            severity=p.get("severity") or "",
            urgency=p.get("urgency") or "",
            certainty=p.get("certainty") or "",
            message_type=p.get("messageType") or "Alert",
            sent=_iso(p.get("sent")),
            onset=_iso(p.get("onset")),
            expires=_iso(p.get("expires")),
            ends=_iso(p.get("ends")),
            sender=p.get("senderName") or "",
            references=tuple(r.get("identifier", "") for r in p.get("references") or []),
            polygons=_polygons(f.get("geometry")),
            zones=tuple(p.get("affectedZones") or ()),
        )


def _ring(ring) -> tuple[tuple[float, float], ...]:
    # GeoJSON is [lon, lat]; everything else in this bot is (lat, lon).
    return tuple((float(pt[1]), float(pt[0])) for pt in ring)


def _polygons(geometry: dict | None) -> tuple[tuple[tuple[float, float], ...], ...]:
    if not geometry:
        return ()
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return (_ring(coords[0]),) if coords else ()
    if kind == "MultiPolygon":
        return tuple(_ring(poly[0]) for poly in coords if poly)
    return ()


def aviation_section(product_text: str) -> str | None:
    """The `.AVIATION...` section of an AFD, without its header or the `&&` trailer."""
    matches = list(_SECTION.finditer(product_text))
    for i, match in enumerate(matches):
        if match.group(1).strip().startswith("AVIATION"):
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(product_text)
            body = product_text[start:end].split("&&", 1)[0]
            lines = body.strip().splitlines()
            if lines and lines[0].lower().startswith("issued at"):
                lines = lines[1:]
            text = "\n".join(line.rstrip() for line in lines).strip()
            return text or None
    return None


@dataclass(frozen=True, slots=True)
class Discussion:
    office: str
    issued: datetime | None
    text: str

    @property
    def aviation(self) -> str | None:
        return aviation_section(self.text)


class Nws:
    def __init__(self, http: Http) -> None:
        self.http = http

    async def alerts(self, state: str) -> list[Alert]:
        data = await self.http.get_json(
            f"{BASE}/alerts/active", {"area": state.upper()}, ttl=TTL_ALERTS, accept=GEOJSON
        )
        return [Alert.from_feature(f) for f in (data or {}).get("features") or []]

    async def zone_polygons(self, zone_url: str) -> tuple[tuple[tuple[float, float], ...], ...]:
        if not zone_url.startswith(BASE):
            return ()
        data = await self.http.get_json(zone_url, ttl=TTL_ZONE, accept=GEOJSON)
        return _polygons((data or {}).get("geometry"))

    async def discussion(self, office: str) -> Discussion | None:
        listing = await self.http.get_json(
            f"{BASE}/products/types/AFD/locations/{office.upper()}", ttl=TTL_AFD_LIST
        )
        graph = (listing or {}).get("@graph") or []
        if not graph:
            return None
        latest = graph[0]
        product = await self.http.get_json(f"{BASE}/products/{latest['id']}", ttl=TTL_AFD)
        if not product:
            return None
        return Discussion(
            office=office.upper(),
            issued=_iso(product.get("issuanceTime")),
            text=product.get("productText") or "",
        )
