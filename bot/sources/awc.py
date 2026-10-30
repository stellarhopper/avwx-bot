"""aviationweather.gov Data API (https://aviationweather.gov/data/api/).

Free, keyless, and the source of truth for US aviation products. METARs and
TAFs are worldwide; everything else here (PIREPs, SIGMETs, G-AIRMETs, CWAs,
winds aloft) covers the US.

The JSON is already decoded — wind, visibility, cloud layers, even flight
category — so there's no METAR parser in this bot. The models below only
smooth over its quirks: visibility as "10+", wind direction as "VRB", and
coordinates as strings in some products but numbers in others.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import clock
from ..wx import flightcat
from ..wx.density_alt import FT_PER_M, density_altitude, hpa_to_inhg
from ..wx.runways import RunwayEnd, magnetic_variation, runway_ends
from . import windsaloft
from .http import Http

BASE = "https://aviationweather.gov/api/data"

# How long each product is worth caching, in seconds, from how often it changes.
TTL_METAR = 120
TTL_TAF = 600
TTL_STATION = 24 * 3600
TTL_AIRPORT = 24 * 3600
TTL_PIREP = 300
TTL_SIGMET = 300
TTL_GAIRMET = 900
TTL_CWA = 300
TTL_WINDS = 1800
TTL_FIX = 24 * 3600

# AWC accepts a comma list of IDs; keep each request modest.
ID_CHUNK = 20

_ALTIMETER_IN_RAW = re.compile(r"\bA(\d{4})\b")


def _utc(epoch: int | float | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch is not None else None


def _wind_dir(raw) -> int | None:
    """Degrees true, or None for calm-or-variable ("VRB")."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _float(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _chunks(ids: list[str]) -> Iterable[list[str]]:
    for i in range(0, len(ids), ID_CHUNK):
        yield ids[i : i + ID_CHUNK]


@dataclass(frozen=True, slots=True)
class Metar:
    station: str
    name: str
    raw: str
    observed: datetime | None
    lat: float | None
    lon: float | None
    elevation_ft: float | None
    wind_dir: int | None
    wind_variable: bool
    wind_speed: int | None
    wind_gust: int | None
    visibility_sm: float | None
    visibility_text: str
    clouds: list[dict]
    ceiling_ft: int | None
    temp_c: float | None
    dewpoint_c: float | None
    altimeter_hpa: float | None
    altimeter_inhg: float | None
    weather: str
    flight_category: str
    kind: str  # METAR | SPECI

    @classmethod
    def from_api(cls, d: dict) -> "Metar":
        clouds = [c for c in d.get("clouds") or [] if isinstance(c, dict)]
        vis = flightcat.parse_visibility(d.get("visib"))
        ceil = flightcat.ceiling(clouds, d.get("vertVis"))
        raw = d.get("rawOb") or ""
        hpa = _float(d.get("altim"))
        # The raw report carries the exact setting (A3004); converting the
        # hPa field back loses a hundredth, which a pilot would notice.
        match = _ALTIMETER_IN_RAW.search(raw)
        inhg = int(match.group(1)) / 100 if match else (hpa_to_inhg(hpa) if hpa else None)
        elev_m = _float(d.get("elev"))
        return cls(
            station=d.get("icaoId", "????"),
            name=d.get("name") or "",
            raw=raw,
            observed=_utc(d.get("obsTime")),
            lat=_float(d.get("lat")),
            lon=_float(d.get("lon")),
            elevation_ft=elev_m * FT_PER_M if elev_m is not None else None,
            wind_dir=_wind_dir(d.get("wdir")),
            wind_variable=str(d.get("wdir")).upper() == "VRB",
            wind_speed=d.get("wspd"),
            wind_gust=d.get("wgst"),
            visibility_sm=vis,
            visibility_text=str(d.get("visib")) if d.get("visib") is not None else "",
            clouds=clouds,
            ceiling_ft=ceil,
            temp_c=_float(d.get("temp")),
            dewpoint_c=_float(d.get("dewp")),
            altimeter_hpa=hpa,
            altimeter_inhg=inhg,
            weather=d.get("wxString") or "",
            flight_category=d.get("fltCat") or flightcat.category(ceil, vis),
            kind=d.get("metarType") or "METAR",
        )

    @property
    def position(self) -> tuple[float, float] | None:
        if self.lat is None or self.lon is None:
            return None
        return (self.lat, self.lon)

    @property
    def density_altitude_ft(self) -> float | None:
        if None in (self.elevation_ft, self.altimeter_inhg, self.temp_c):
            return None
        return density_altitude(self.elevation_ft, self.altimeter_inhg, self.temp_c)

    @property
    def spread_c(self) -> float | None:
        if self.temp_c is None or self.dewpoint_c is None:
            return None
        return self.temp_c - self.dewpoint_c

    def age_minutes(self, now: datetime | None = None) -> float | None:
        if self.observed is None:
            return None
        now = now or clock.now()
        return (now - self.observed).total_seconds() / 60


@dataclass(frozen=True, slots=True)
class TafPeriod:
    start: datetime
    end: datetime
    change: str | None  # None (initial), FM, BECMG, TEMPO, PROB
    probability: int | None
    wind_dir: int | None
    wind_speed: int | None
    wind_gust: int | None
    visibility_sm: float | None
    visibility_text: str
    clouds: list[dict]
    ceiling_ft: int | None
    weather: str
    wind_shear: str | None

    @property
    def flight_category(self) -> str:
        return flightcat.category(self.ceiling_ft, self.visibility_sm)

    @property
    def is_conditional(self) -> bool:
        """TEMPO and PROB groups modify, rather than replace, the prevailing forecast."""
        return self.change in ("TEMPO", "PROB") or self.probability is not None


@dataclass(frozen=True, slots=True)
class Taf:
    station: str
    name: str
    raw: str
    issued: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    periods: list[TafPeriod] = field(default_factory=list)

    @classmethod
    def from_api(cls, d: dict) -> "Taf":
        periods = []
        for f in d.get("fcsts") or []:
            clouds = [c for c in f.get("clouds") or [] if isinstance(c, dict)]
            shear = None
            if f.get("wshearHgt") is not None:
                shear = f"WS{int(f['wshearHgt']):03d}/{f.get('wshearDir') or 0:03d}{f.get('wshearSpd') or 0:02d}KT"
            periods.append(
                TafPeriod(
                    start=_utc(f.get("timeFrom")),
                    end=_utc(f.get("timeTo")),
                    change=f.get("fcstChange"),
                    probability=f.get("probability"),
                    wind_dir=_wind_dir(f.get("wdir")),
                    wind_speed=f.get("wspd"),
                    wind_gust=f.get("wgst"),
                    visibility_sm=flightcat.parse_visibility(f.get("visib")),
                    visibility_text=str(f.get("visib")) if f.get("visib") is not None else "",
                    clouds=clouds,
                    ceiling_ft=flightcat.ceiling(clouds, f.get("vertVis")),
                    weather=f.get("wxString") or "",
                    wind_shear=shear,
                )
            )
        return cls(
            station=d.get("icaoId", "????"),
            name=d.get("name") or "",
            raw=d.get("rawTAF") or "",
            issued=_parse_iso(d.get("issueTime")),
            valid_from=_utc(d.get("validTimeFrom")),
            valid_to=_utc(d.get("validTimeTo")),
            periods=periods,
        )

    def prevailing_at(self, when: datetime) -> TafPeriod | None:
        """The base forecast in force at `when`, ignoring TEMPO/PROB groups."""
        found = None
        for p in self.periods:
            if not p.is_conditional and p.start <= when < p.end:
                found = p
        return found

    def conditional_at(self, when: datetime) -> list[TafPeriod]:
        return [p for p in self.periods if p.is_conditional and p.start <= when < p.end]


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Station:
    icao: str
    name: str
    lat: float
    lon: float
    elevation_ft: float
    state: str
    country: str

    @property
    def position(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass(frozen=True, slots=True)
class AirportInfo:
    icao: str
    name: str
    lat: float
    lon: float
    elevation_ft: float
    magvar: float  # degrees, east positive
    runways: list[RunwayEnd]  # paved ends only, true headings


@dataclass(frozen=True, slots=True)
class Navpoint:
    """An FAA fix (VPROL) or a navaid (BJC VOR/DME)."""

    ident: str
    kind: str  # "fix", or the navaid type such as "VOR/DME"
    name: str
    lat: float
    lon: float
    magvar: float | None  # navaids only

    @property
    def position(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass(frozen=True, slots=True)
class Pirep:
    raw: str
    observed: datetime | None
    lat: float | None
    lon: float | None
    flight_level: int | None  # hundreds of feet
    aircraft: str
    turbulence: str
    icing: str
    weather: str
    urgent: bool

    @classmethod
    def from_api(cls, d: dict) -> "Pirep":
        raw = d.get("rawOb") or ""
        return cls(
            raw=raw,
            observed=_utc(d.get("obsTime")),
            lat=_float(d.get("lat")),
            lon=_float(d.get("lon")),
            flight_level=d.get("fltLvl") if isinstance(d.get("fltLvl"), int) else None,
            aircraft=d.get("acType") or "",
            turbulence=d.get("tbInt1") or "",
            icing=d.get("icgInt1") or "",
            weather=d.get("wxString") or "",
            urgent=" UUA " in f" {raw} " or d.get("pirepType") == "Urgent PIREP",
        )

    @property
    def mentions_wave(self) -> bool:
        text = self.raw.upper()
        return "WAVE" in text or "MTN WV" in text or "ROTOR" in text


@dataclass(frozen=True, slots=True)
class Hazard:
    """An area hazard: a SIGMET, a G-AIRMET polygon, or a CWA."""

    source: str  # SIGMET | CONVECTIVE | GAIRMET | CWA
    hazard: str  # CONVECTIVE, TURB-LO, MT_OBSC, ICE, IFR, LLWS, ...
    ident: str
    valid_from: datetime | None
    valid_to: datetime | None
    polygon: list[tuple[float, float]]
    top: str | None
    base: str | None
    severity: str | None
    due_to: str | None
    raw: str
    movement: str | None = None

    @property
    def key(self) -> str:
        start = int(self.valid_from.timestamp()) if self.valid_from else 0
        return f"{self.source}:{self.ident}:{start}"


def _polygon(coords) -> list[tuple[float, float]]:
    out = []
    for c in coords or []:
        lat, lon = _float(c.get("lat")), _float(c.get("lon"))
        if lat is not None and lon is not None:
            out.append((lat, lon))
    return out


def _sigmet(d: dict) -> Hazard:
    hazard = d.get("hazard") or ""
    is_convective = hazard == "CONVECTIVE"
    movement = None
    if d.get("movementDir") is not None and d.get("movementSpd"):
        movement = f"from {d['movementDir']:03d}° at {d['movementSpd']} kt"
    top = d.get("altitudeHi1")
    return Hazard(
        source="CONVECTIVE" if is_convective else d.get("airSigmetType") or "SIGMET",
        hazard=hazard,
        ident=d.get("seriesId") or d.get("alphaChar") or "",
        valid_from=_utc(d.get("validTimeFrom")),
        valid_to=_utc(d.get("validTimeTo")),
        polygon=_polygon(d.get("coords")),
        top=f"FL{top // 100:03d}" if isinstance(top, int) and top else None,
        base=None,
        severity=str(d.get("severity")) if d.get("severity") is not None else None,
        due_to=None,
        raw=d.get("rawAirSigmet") or "",
        movement=movement,
    )


def _gairmet(d: dict) -> Hazard:
    valid = _parse_iso(d.get("validTime"))
    return Hazard(
        source="GAIRMET",
        hazard=d.get("hazard") or "",
        ident=f"{d.get('product', '')}-{d.get('tag', '')}-{d.get('hazard', '')}",
        valid_from=valid,
        valid_to=_utc(d.get("expireTime")),
        polygon=_polygon(d.get("coords")),
        top=d.get("top"),
        base=d.get("base"),
        severity=d.get("severity"),
        due_to=d.get("due_to"),
        raw="",
    )


def _cwa(d: dict) -> Hazard:
    return Hazard(
        source="CWA",
        hazard=d.get("hazard") or "CWA",
        ident=f"{d.get('cwsu', '')}-{d.get('seriesId', '')}",
        valid_from=_utc(d.get("validTimeFrom")),
        valid_to=_utc(d.get("validTimeTo")),
        polygon=_polygon(d.get("coords")),
        top=str(d["top"]) if d.get("top") else None,
        base=str(d["base"]) if d.get("base") else None,
        severity=None,
        due_to=None,
        raw=d.get("cwaText") or "",
    )


class Awc:
    def __init__(self, http: Http) -> None:
        self.http = http

    async def _get(self, product: str, params: dict, ttl: float):
        return await self.http.get_json(f"{BASE}/{product}", {**params, "format": "json"}, ttl=ttl)

    async def metars(self, ids: Iterable[str]) -> dict[str, Metar]:
        wanted = sorted({i.upper() for i in ids})
        out: dict[str, Metar] = {}
        for chunk in _chunks(wanted):
            for d in await self._get("metar", {"ids": ",".join(chunk)}, TTL_METAR) or []:
                m = Metar.from_api(d)
                # The API can return more than one report per station; keep the newest.
                prev = out.get(m.station)
                if prev is None or (m.observed and prev.observed and m.observed > prev.observed):
                    out[m.station] = m
        return out

    async def metars_in(self, box: tuple[float, float, float, float]) -> list[Metar]:
        data = await self._get("metar", {"bbox": ",".join(str(v) for v in box)}, TTL_METAR)
        latest: dict[str, Metar] = {}
        for d in data or []:
            m = Metar.from_api(d)
            prev = latest.get(m.station)
            if prev is None or (m.observed and prev.observed and m.observed > prev.observed):
                latest[m.station] = m
        return list(latest.values())

    async def tafs(self, ids: Iterable[str]) -> dict[str, Taf]:
        wanted = sorted({i.upper() for i in ids})
        out: dict[str, Taf] = {}
        for chunk in _chunks(wanted):
            for d in await self._get("taf", {"ids": ",".join(chunk)}, TTL_TAF) or []:
                t = Taf.from_api(d)
                if t.station not in out or d.get("mostRecent"):
                    out[t.station] = t
        return out

    async def stations(self, ids: Iterable[str]) -> dict[str, Station]:
        wanted = sorted({i.upper() for i in ids})
        out: dict[str, Station] = {}
        for chunk in _chunks(wanted):
            for d in await self._get("stationinfo", {"ids": ",".join(chunk)}, TTL_STATION) or []:
                icao = d.get("icaoId") or d.get("id")
                lat, lon = _float(d.get("lat")), _float(d.get("lon"))
                if not icao or lat is None or lon is None:
                    continue
                out[icao] = Station(
                    icao=icao,
                    name=d.get("site") or "",
                    lat=lat,
                    lon=lon,
                    elevation_ft=(_float(d.get("elev")) or 0) * FT_PER_M,
                    state=d.get("state") or "",
                    country=d.get("country") or "",
                )
        return out

    async def runways(self, ids: Iterable[str]) -> dict[str, list[RunwayEnd]]:
        """Paved runway ends per airport, with true headings. Airports without
        any (or unknown to the database) are absent."""
        wanted = sorted({i.upper() for i in ids})
        out: dict[str, list[RunwayEnd]] = {}
        for chunk in _chunks(wanted):
            for d in await self._get("airport", {"ids": ",".join(chunk)}, TTL_AIRPORT) or []:
                ends = runway_ends(d.get("runways") or [], d.get("magdec"))
                if d.get("icaoId") and ends:
                    out[d["icaoId"]] = ends
        return out

    async def airports(self, ids: Iterable[str]) -> dict[str, AirportInfo]:
        """Position, variation and paved runways. Same request as runways(), so
        asking for both costs one fetch."""
        wanted = sorted({i.upper() for i in ids})
        out: dict[str, AirportInfo] = {}
        for chunk in _chunks(wanted):
            for d in await self._get("airport", {"ids": ",".join(chunk)}, TTL_AIRPORT) or []:
                lat, lon = _float(d.get("lat")), _float(d.get("lon"))
                if not d.get("icaoId") or lat is None or lon is None:
                    continue
                out[d["icaoId"]] = AirportInfo(
                    icao=d["icaoId"],
                    name=(d.get("name") or "").strip(),
                    lat=lat,
                    lon=lon,
                    elevation_ft=(_float(d.get("elev")) or 0) * FT_PER_M,
                    magvar=magnetic_variation(d.get("magdec")),
                    runways=runway_ends(d.get("runways") or [], d.get("magdec")),
                )
        return out

    async def fixes(self, ids: Iterable[str]) -> dict[str, Navpoint]:
        wanted = sorted({i.upper() for i in ids})
        out: dict[str, Navpoint] = {}
        for chunk in _chunks(wanted):
            for d in await self._get("fix", {"ids": ",".join(chunk)}, TTL_FIX) or []:
                lat, lon = _float(d.get("lat")), _float(d.get("lon"))
                if d.get("id") and lat is not None and lon is not None:
                    out[d["id"]] = Navpoint(d["id"], "fix", "", lat, lon, None)
        return out

    async def navaids(self, ids: Iterable[str]) -> dict[str, Navpoint]:
        """US navaids only: identifiers repeat across countries, and a route
        planned here never means the Danish one."""
        wanted = sorted({i.upper() for i in ids})
        out: dict[str, Navpoint] = {}
        for chunk in _chunks(wanted):
            for d in await self._get("navaid", {"ids": ",".join(chunk)}, TTL_FIX) or []:
                lat, lon = _float(d.get("lat")), _float(d.get("lon"))
                if not d.get("id") or lat is None or lon is None or d.get("country") not in (None, "US"):
                    continue
                out[d["id"]] = Navpoint(
                    d["id"], d.get("type") or "navaid", (d.get("name") or "").strip(), lat, lon,
                    magnetic_variation(d.get("mag_dec")),
                )
        return out

    async def pireps(self, near: str, distance_nm: int = 100, age_hours: int = 3) -> list[Pirep]:
        data = await self._get(
            "pirep", {"id": near.upper(), "distance": distance_nm, "age": age_hours}, TTL_PIREP
        )
        return [Pirep.from_api(d) for d in data or []]

    async def sigmets(self) -> list[Hazard]:
        """Domestic SIGMETs and convective SIGMETs. AIRMETs come from gairmets()."""
        data = await self._get("airsigmet", {}, TTL_SIGMET)
        return [_sigmet(d) for d in data or [] if d.get("airSigmetType") == "SIGMET"]

    async def gairmets(self) -> list[Hazard]:
        data = await self._get("gairmet", {}, TTL_GAIRMET)
        return [
            _gairmet(d)
            for d in data or []
            if d.get("geometryType") == "AREA" and d.get("hazard") not in ("M_FZLVL", "FZLVL")
        ]

    async def cwas(self) -> list[Hazard]:
        data = await self._get("cwa", {}, TTL_CWA)
        return [_cwa(d) for d in data or []]

    async def winds_aloft(self, fcst: str = "06") -> windsaloft.Forecast:
        """The low- and high-level FB winds, merged into one table per site."""
        _, low = await self.http.get_text(
            f"{BASE}/windtemp", {"region": "all", "level": "low", "fcst": fcst}, ttl=TTL_WINDS
        )
        _, high = await self.http.get_text(
            f"{BASE}/windtemp", {"region": "all", "level": "high", "fcst": fcst}, ttl=TTL_WINDS
        )
        return windsaloft.parse(low + "\n" + high)
