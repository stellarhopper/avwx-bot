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
TTL_POINT = 7 * 24 * 3600
TTL_HOURLY = 1800

# The hourly forecast gives wind as a compass point and "10 mph" or "5 to 10 mph".
_COMPASS = {d: i * 22.5 for i, d in enumerate(
    "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split()
)}
_MPH = re.compile(r"(\d+)\s*mph")
KT_PER_MPH = 0.868976

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
    vtec: tuple[str, ...] = ()
    nws_headline: str = ""
    max_gust: str = ""
    max_hail: str = ""
    motion: str = ""

    @property
    def until(self) -> datetime | None:
        """When the hazard itself ends — `ends` if given, else the message's expiry."""
        return self.ends or self.expires

    @classmethod
    def from_feature(cls, f: dict) -> "Alert":
        p = f.get("properties") or {}
        params = p.get("parameters") or {}
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
            vtec=tuple(params.get("VTEC") or ()),
            nws_headline=_first(params.get("NWSheadline")),
            max_gust=_first(params.get("maxWindGust")),
            max_hail=_first(params.get("maxHailSize")),
            motion=_first(params.get("eventMotionDescription")),
        )


def _first(values) -> str:
    """NWS parameters are lists of strings; nearly always exactly one."""
    if isinstance(values, list) and values:
        return str(values[0])
    return ""


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


@dataclass(frozen=True, slots=True)
class HourlyPeriod:
    """One hour of the NWS gridpoint forecast. Covers airports without a TAF,
    and times past the end of one."""

    start: datetime
    end: datetime
    temp_c: float | None
    dewpoint_c: float | None
    wind_dir: float | None  # degrees true, from a 16-point compass
    wind_kt: int | None  # the upper figure when given as a range
    precip_pct: int | None
    summary: str

    @classmethod
    def from_api(cls, d: dict) -> "HourlyPeriod | None":
        start, end = _iso(d.get("startTime")), _iso(d.get("endTime"))
        if start is None or end is None:
            return None
        temp = d.get("temperature")
        if isinstance(temp, dict):
            temp = temp.get("value")
        if temp is not None and d.get("temperatureUnit", "F") == "F":
            temp = (temp - 32) * 5 / 9
        dew = (d.get("dewpoint") or {}).get("value")
        speeds = [int(m) for m in _MPH.findall(d.get("windSpeed") or "")]
        if not speeds and str(d.get("windSpeed", "")).strip().isdigit():
            speeds = [int(d["windSpeed"])]
        return cls(
            start=start,
            end=end,
            temp_c=round(temp, 1) if temp is not None else None,
            dewpoint_c=round(dew, 1) if dew is not None else None,
            wind_dir=_COMPASS.get(str(d.get("windDirection") or "").upper()),
            wind_kt=round(max(speeds) * KT_PER_MPH) if speeds else None,
            precip_pct=(d.get("probabilityOfPrecipitation") or {}).get("value"),
            summary=d.get("shortForecast") or "",
        )


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

    async def office_for(self, lat: float, lon: float) -> str | None:
        """The forecast office (e.g. BOU) responsible for a point."""
        # More than four decimal places gets a redirect; fewer keeps the cache key stable.
        data = await self.http.get_json(f"{BASE}/points/{lat:.4f},{lon:.4f}", ttl=TTL_POINT, accept=GEOJSON)
        return ((data or {}).get("properties") or {}).get("cwa")

    async def hourly(self, lat: float, lon: float) -> list[HourlyPeriod]:
        """The gridpoint hourly forecast for a point, about a week ahead."""
        point = await self.http.get_json(f"{BASE}/points/{lat:.4f},{lon:.4f}", ttl=TTL_POINT, accept=GEOJSON)
        url = ((point or {}).get("properties") or {}).get("forecastHourly")
        if not url or not url.startswith(BASE):
            return []
        data = await self.http.get_json(url, ttl=TTL_HOURLY, accept=GEOJSON)
        periods = ((data or {}).get("properties") or {}).get("periods") or []
        return [p for p in (HourlyPeriod.from_api(d) for d in periods) if p is not None]

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
