"""Everything about the weather for one planned flight.

`briefing.gather` answers "what's the weather here now". This answers "what
will it be along this route when I'm there": each airport at its estimated
arrival time, winds aloft at cruise for each leg from whichever FB forecast
covers that time, and hazards along a corridor rather than around a point.

It runs in three steps, because each needs the one before:

1. Winds aloft for each leg, from a rough arrival time at TAS.
2. The nav log with those winds, which gives the real arrival times.
3. Weather at each airport at its arrival time, hazards over the whole flight,
   and the pass board for any pass the route crosses.

Like the brief, each section is fetched on its own and a failure is recorded in
`problems` rather than raised. A plan without PIREPs beats no plan.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import briefing, clock, hazards, mountain, regions
from .route import Point
from .sources.awc import AirportInfo, Awc, Hazard, Metar, Pirep, Taf, TafPeriod
from .sources.http import SourceError
from .sources.nws import Alert, HourlyPeriod, Nws
from .sources.windsaloft import Forecast, Wind, interpolate
from .wx import geo, navlog, runways
from .wx.density_alt import density_altitude

FB_FORECASTS = ("06", "12", "24")
CORRIDOR_NM = 10  # hazards, alerts: how far either side of the route counts
SAMPLE_NM = 10  # spacing of the points the corridor is checked at
PASS_NEAR_NM = 5  # a pass this close to the route is "on" it
PIREP_RADIUS_NM = 60
PIREP_AGE_HOURS = 3
STANDARD_ALTIMETER = 29.92
# A METAR still describes conditions this close to now.
METAR_FRESH = timedelta(minutes=90)


@dataclass(frozen=True, slots=True)
class Performance:
    """What the nav log needs from the aircraft. `assumed` when no aircraft was
    given and generic numbers stand in."""

    tas_kt: float
    burn_gph: float
    taxi_gal: float = 0.0
    climb_gal_per_1000ft: float = 0.0
    assumed: bool = False


GENERIC = Performance(tas_kt=110, burn_gph=10, taxi_gal=1.5, climb_gal_per_1000ft=0.3, assumed=True)


@dataclass(frozen=True, slots=True)
class LegWind:
    origin: str
    dest: str
    site: str | None
    forecast: str | None  # "06", "12", "24"
    use: str | None  # "0200–0900Z"
    covers: bool  # the forecast's use window includes the leg time
    wind: Wind | None  # at cruise


@dataclass
class AirportOutlook:
    ident: str
    name: str
    role: str  # departure | stop | destination | waypoint
    eta: datetime
    elevation_ft: float | None
    metar: Metar | None = None
    taf: Taf | None = None
    taf_period: TafPeriod | None = None
    taf_conditional: list[TafPeriod] = field(default_factory=list)
    hourly: HourlyPeriod | None = None
    wind: tuple[int | None, int | None, int | None] | None = None  # dir, kt, gust
    wind_source: str = ""
    density_alt_ft: float | None = None
    density_source: str = ""
    runway: runways.Choice | None = None
    runway_ends: list = field(default_factory=list)


@dataclass
class RouteWeather:
    points: list[Point]
    departure: datetime
    cruise_alt_ft: float
    performance: Performance
    log: navlog.NavLog
    leg_winds: list[LegWind]
    airports: list[AirportOutlook]
    hazards: list[Hazard] | None = None
    alerts: list[Alert] | None = None
    pireps: list[Pirep] | None = None
    afds: list[briefing.Afd] = field(default_factory=list)
    passes: list[mountain.PassStatus] = field(default_factory=list)
    wave: mountain.WaveRisk | None = None
    highest: tuple[str, float] | None = None
    problems: list[str] = field(default_factory=list)


async def _guard(problems: list[str], what: str, coro, default=None):
    try:
        return await coro
    except SourceError as exc:
        problems.append(f"{what}: {exc}")
        return default


def use_window(forecast: Forecast, near: datetime) -> tuple[datetime, datetime] | None:
    """The FB's "for use" period as datetimes. The product gives only
    DDHHMM and HHMM, so the month comes from `near`."""
    if not (forecast.valid and forecast.use_from and forecast.use_to):
        return None
    try:
        day, hh, mm = int(forecast.valid[:2]), int(forecast.valid[2:4]), int(forecast.valid[4:6])
        valid = None
        for months_back in (0, 1, -1):
            month = near.month - months_back
            year = near.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            try:
                candidate = datetime(year, month, day, hh, mm, tzinfo=timezone.utc)
            except ValueError:
                continue
            if abs(candidate - near) < timedelta(days=15):
                valid = candidate
                break
        if valid is None:
            return None
        start = valid.replace(hour=int(forecast.use_from[:2]) % 24, minute=int(forecast.use_from[2:]))
        if start > valid:
            start -= timedelta(days=1)
        end_h = int(forecast.use_to[:2])
        end = valid.replace(hour=end_h % 24, minute=int(forecast.use_to[2:]))
        if end_h == 24 or end < valid:
            end += timedelta(days=1)
        return start, end
    except (ValueError, IndexError):
        return None


def _pick_forecast(forecasts: dict[str, Forecast], when: datetime) -> tuple[str, Forecast, bool] | None:
    best = None
    for key, fc in forecasts.items():
        window = use_window(fc, when)
        if window is None:
            continue
        start, end = window
        if start <= when <= end:
            return key, fc, True
        gap = min(abs(when - start), abs(when - end))
        if best is None or gap < best[0]:
            best = (gap, key, fc)
    return (best[1], best[2], False) if best else None


def _samples(points: list[Point]) -> list[tuple[float, float]]:
    """Points every SAMPLE_NM along the route, for corridor checks."""
    out = [points[0].position]
    for a, b in zip(points, points[1:]):
        steps = max(1, int(geo.distance_nm(a.position, b.position) // SAMPLE_NM))
        for k in range(1, steps + 1):
            f = k / steps
            out.append((a.lat + f * (b.lat - a.lat), a.lon + f * (b.lon - a.lon)))
    return out


def _roles(points: list[Point], stops: set[int]) -> list[str]:
    last = len(points) - 1
    return [
        "departure" if i == 0 else "destination" if i == last else "stop" if i in stops else "waypoint"
        for i in range(len(points))
    ]


async def plan(
    awc: Awc,
    nws: Nws,
    points: list[Point],
    *,
    departure: datetime,
    cruise_alt_ft: float,
    performance: Performance = GENERIC,
    stops: set[int] | None = None,
    reserve_minutes: float = 30,
    now: datetime | None = None,
) -> RouteWeather:
    now = now or clock.now()
    problems: list[str] = []
    stops = {i for i in (stops or set()) if 0 < i < len(points) - 1}
    airport_ids = sorted({p.ident for p in points if p.kind == "airport"})

    info, *fbs = await asyncio.gather(
        _guard(problems, "Airport data", awc.airports(airport_ids), {}),
        *(_guard(problems, f"Winds aloft ({h} h)", awc.winds_aloft(h)) for h in FB_FORECASTS),
    )
    info: dict[str, AirportInfo] = info or {}
    forecasts = {h: fc for h, fc in zip(FB_FORECASTS, fbs) if fc is not None}

    waypoints = [
        navlog.Waypoint(
            p.ident, p.lat, p.lon,
            info[p.ident].elevation_ft if p.ident in info else p.elevation_ft,
            info[p.ident].magvar if p.ident in info else None,
        )
        for p in points
    ]

    # 1. Winds for each leg, at a rough time from TAS alone.
    leg_winds: list[LegWind] = []
    rough = departure
    for a, b in zip(points, points[1:]):
        mid = ((a.lat + b.lat) / 2, (a.lon + b.lon) / 2)
        leg_hours = geo.distance_nm(a.position, b.position) / performance.tas_kt
        leg_time = rough + timedelta(hours=leg_hours / 2)  # the middle of the leg
        rough += timedelta(hours=leg_hours)
        chosen = _pick_forecast(forecasts, leg_time)
        if chosen is None:
            leg_winds.append(LegWind(a.ident, b.ident, None, None, None, False, None))
            continue
        key, fc, covers = chosen
        site = hazards.nearest_fb_site(mid, fc.sites)
        wind = interpolate(fc.site(site), cruise_alt_ft) if site else None
        use = f"{fc.use_from}–{fc.use_to}Z" if fc.use_from else None
        leg_winds.append(LegWind(a.ident, b.ident, site, key, use, covers, wind))
    if forecasts and not all(w.covers for w in leg_winds):
        problems.append(
            "Winds aloft: the flight is outside the period the latest FB forecasts cover; "
            "the nearest one is used."
        )

    # 2. The nav log.
    log = navlog.build(
        waypoints,
        departure=departure,
        tas_kt=performance.tas_kt,
        burn_gph=performance.burn_gph,
        cruise_alt_ft=cruise_alt_ft,
        winds=[(w.wind.direction, w.wind.speed_kt) if w.wind and w.wind.direction else None for w in leg_winds],
        stops=stops,
        taxi_gal=performance.taxi_gal,
        climb_gal_per_1000ft=performance.climb_gal_per_1000ft,
        reserve_minutes=reserve_minutes,
    )

    # 3. Weather at each airport at its ETA, and along the route.
    roles = _roles(points, stops)
    samples = _samples(points)
    end = log.etas[-1]
    flight_hours = max(1.0, (end - departure).total_seconds() / 3600 + 1)
    states = {s for s in (_state_of(p, info) for p in points) if s}

    metars, tafs, hourlies, hazard_list, alerts, pireps, afds, board = await asyncio.gather(
        _guard(problems, "METARs", awc.metars(airport_ids), {}),
        _guard(problems, "TAFs", awc.tafs(airport_ids), {}),
        _hourlies(nws, points, problems),
        _guard(problems, "AIRMETs/SIGMETs", _route_hazards(awc, samples, departure, flight_hours)),
        _guard(problems, "NWS alerts", briefing.us_alerts(nws, states, samples, CORRIDOR_NM)) if states else _none(),
        _guard(problems, "PIREPs", _pireps(awc, points)),
        _afds(nws, points, problems),
        _passes(awc, nws, samples, problems, now),
    )

    outlooks = []
    for i, p in enumerate(points):
        if p.kind != "airport":
            continue
        a = info.get(p.ident)
        outlooks.append(_outlook(
            p, roles[i], log.etas[i], a, (metars or {}).get(p.ident), (tafs or {}).get(p.ident),
            hourlies.get(i), now,
        ))

    known = [(p.ident if p.kind != "pass" else p.label(), w.elevation_ft) for p, w in zip(points, waypoints)
             if w.elevation_ft is not None]
    return RouteWeather(
        points=points,
        departure=departure,
        cruise_alt_ft=cruise_alt_ft,
        performance=performance,
        log=log,
        leg_winds=leg_winds,
        airports=outlooks,
        hazards=hazard_list,
        alerts=alerts,
        pireps=pireps,
        afds=afds,
        passes=board.passes if board else [],
        wave=board.wave if board else None,
        highest=max(known, key=lambda k: k[1]) if known else None,
        problems=problems + (board.problems if board else []),
    )


def _state_of(p: Point, info: dict[str, AirportInfo]) -> str | None:
    # Route points carry the state in their note ("Granby, CO", or just "CO").
    if p.kind == "airport" and p.note:
        tail = p.note.rsplit(",", 1)[-1].strip()
        if len(tail) == 2 and tail.isalpha():
            return tail
    return None


async def _none():
    return None


async def _hourlies(nws: Nws, points: list[Point], problems: list[str]) -> dict[int, list[HourlyPeriod]]:
    out: dict[int, list[HourlyPeriod]] = {}
    indices = [i for i, p in enumerate(points) if p.kind == "airport"]
    results = await asyncio.gather(
        *(_guard(problems, f"NWS forecast for {points[i].ident}", nws.hourly(points[i].lat, points[i].lon), [])
          for i in indices)
    )
    for i, periods in zip(indices, results):
        out[i] = periods or []
    return out


async def _route_hazards(awc: Awc, samples, departure: datetime, hours: float) -> list[Hazard]:
    sigmets, gairmets, cwas = await asyncio.gather(awc.sigmets(), awc.gairmets(), awc.cwas())
    return hazards.near([*sigmets, *gairmets, *cwas], samples, CORRIDOR_NM, departure, hours=hours)


async def _pireps(awc: Awc, points: list[Point]) -> list[Pirep]:
    """Notable PIREPs near the first, middle and last airports."""
    airports = [p for p in points if p.kind == "airport"]
    picks = {a.ident: a for a in (airports[:1] + airports[len(airports) // 2 : len(airports) // 2 + 1] + airports[-1:])}
    found = await asyncio.gather(*(awc.pireps(ident, PIREP_RADIUS_NM, PIREP_AGE_HOURS) for ident in picks))
    seen, out = set(), []
    for p in briefing.notable_pireps([p for batch in found for p in batch]):
        if p.raw not in seen:
            seen.add(p.raw)
            out.append(p)
    return out


async def _afds(nws: Nws, points: list[Point], problems: list[str]) -> list[briefing.Afd]:
    """The aviation discussion from each forecast office along the route."""
    airports = [p for p in points if p.kind == "airport"]
    offices: dict[str, Point] = {}
    for p in airports:
        office = await _guard(problems, "Forecast office lookup", nws.office_for(p.lat, p.lon))
        if office and office not in offices:
            offices[office] = p
    afds = await asyncio.gather(
        *(_guard(problems, f"Forecast discussion ({o})", briefing.afd_for(nws, p.position)) for o, p in offices.items())
    )
    return [a for a in afds if a]


async def _passes(awc: Awc, nws: Nws, samples, problems: list[str], now: datetime):
    """The pass board, limited to passes the route goes over."""
    for region in regions.all_regions().values():
        crossed = [
            p["id"] for p in region.get("passes", [])
            if min(geo.distance_nm((p["lat"], p["lon"]), s) for s in samples) <= PASS_NEAR_NM
        ]
        if crossed:
            board = await _guard(
                problems, "Mountain data",
                mountain.build(awc, nws, region, {"mountain": {"passes": crossed}}, now),
            )
            return board
    return None


def _hourly_at(periods: list[HourlyPeriod], when: datetime) -> HourlyPeriod | None:
    return next((h for h in periods if h.start <= when < h.end), None)


def _outlook(
    p: Point, role: str, eta: datetime, info: AirportInfo | None, metar: Metar | None,
    taf: Taf | None, hourly: list[HourlyPeriod] | None, now: datetime,
) -> AirportOutlook:
    out = AirportOutlook(
        ident=p.ident,
        name=info.name if info else p.name,
        role=role,
        eta=eta,
        elevation_ft=info.elevation_ft if info else p.elevation_ft,
        metar=metar,
        taf=taf,
        runway_ends=info.runways if info else [],
    )
    fresh = metar is not None and metar.observed is not None and abs(eta - now) <= METAR_FRESH
    if taf is not None and taf.valid_to and taf.valid_from and taf.valid_from <= eta < taf.valid_to:
        out.taf_period = taf.prevailing_at(eta)
        out.taf_conditional = taf.conditional_at(eta)
    out.hourly = _hourly_at(hourly or [], eta)

    if fresh and metar.wind_speed is not None:
        out.wind = (metar.wind_dir, metar.wind_speed, metar.wind_gust)
        out.wind_source = "METAR"
    elif out.taf_period is not None and out.taf_period.wind_speed is not None:
        tp = out.taf_period
        out.wind = (tp.wind_dir, tp.wind_speed, tp.wind_gust)
        out.wind_source = "TAF"
    elif out.hourly is not None and out.hourly.wind_kt is not None:
        h = out.hourly
        out.wind = (round(h.wind_dir) if h.wind_dir is not None else None, h.wind_kt, None)
        out.wind_source = "NWS hourly forecast"

    if out.wind and out.runway_ends:
        out.runway = runways.best(out.runway_ends, *out.wind)

    altimeter = metar.altimeter_inhg if metar and metar.altimeter_inhg else STANDARD_ALTIMETER
    if out.elevation_ft is not None:
        if fresh and metar.temp_c is not None:
            out.density_alt_ft = density_altitude(out.elevation_ft, altimeter, metar.temp_c)
            out.density_source = "METAR temperature and altimeter"
        elif out.hourly is not None and out.hourly.temp_c is not None:
            out.density_alt_ft = density_altitude(out.elevation_ft, altimeter, out.hourly.temp_c)
            out.density_source = (
                "forecast temperature, "
                + ("current altimeter" if metar and metar.altimeter_inhg else "standard altimeter")
            )
    return out
