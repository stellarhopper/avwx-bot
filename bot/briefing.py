"""Gathering everything a brief shows.

Each section is fetched on its own and a failure in one is recorded, not
raised: a brief that went out at 06:30 without PIREPs is far more useful than
no brief because PIREPs timed out. The renderer shows which sections are
missing and why.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import hazards
from .sources.awc import Awc, Hazard, Metar, Pirep, Station, Taf
from .sources.http import SourceError
from .sources.nws import Alert, Nws
from .sources.windsaloft import Wind
from .wx import fmt

PIREP_RADIUS_NM = 100
PIREP_AGE_HOURS = 3
HAZARD_RADIUS_NM = 30
HAZARD_HOURS = 6


@dataclass(frozen=True, slots=True)
class AirportBrief:
    station: str
    metar: Metar | None
    taf: Taf | None


@dataclass(frozen=True, slots=True)
class WindsAloft:
    site: str
    valid: str | None
    use: str | None
    levels: dict[int, Wind]


@dataclass(frozen=True, slots=True)
class Afd:
    office: str
    issued: datetime | None
    aviation: str


@dataclass
class BriefData:
    title: str
    generated: datetime
    tz: object
    units: fmt.Units
    airports: list[AirportBrief]
    stations: dict[str, Station] = field(default_factory=dict)
    hazards: list[Hazard] | None = None
    alerts: list[Alert] | None = None
    pireps: list[Pirep] | None = None
    winds: WindsAloft | None = None
    afd: Afd | None = None
    mountain: object | None = None  # bot.mountain.MountainBoard, when included
    problems: list[str] = field(default_factory=list)

    @property
    def points(self) -> list[tuple[float, float]]:
        return [s.position for s in self.stations.values()]


async def _guard(problems: list[str], what: str, coro):
    try:
        return await coro
    except SourceError as exc:
        problems.append(f"{what}: {exc}")
        return None


async def winds_for(awc: Awc, point: tuple[float, float] | None, site: str | None) -> WindsAloft | None:
    forecast = await awc.winds_aloft()
    chosen = site or (hazards.nearest_fb_site(point, forecast.sites) if point else None)
    if not chosen or not forecast.site(chosen):
        return None
    use = f"{forecast.use_from}–{forecast.use_to}Z" if forecast.use_from else None
    return WindsAloft(chosen, forecast.valid, use, forecast.site(chosen))


async def us_alerts(nws: Nws, states: set[str], points, radius_nm: float) -> list[Alert]:
    """Active NWS alerts near the points, locating zone-only alerts by their zones."""
    out: list[Alert] = []
    for state in sorted(states):
        for alert in await nws.alerts(state):
            if alert.message_type == "Cancel":
                continue
            polygons = alert.polygons
            if not polygons and alert.zones:
                zone_polys = []
                for zone in alert.zones[:12]:
                    try:
                        zone_polys.extend(await nws.zone_polygons(zone))
                    except SourceError:
                        continue
                polygons = tuple(zone_polys)
            if hazards.alert_near(alert, points, radius_nm, polygons):
                out.append(alert)
    seen, unique = set(), []
    for a in out:
        if a.id not in seen:
            seen.add(a.id)
            unique.append(a)
    return unique


async def afd_for(nws: Nws, point: tuple[float, float]) -> Afd | None:
    office = await nws.office_for(*point)
    if not office:
        return None
    discussion = await nws.discussion(office)
    if discussion is None or not discussion.aviation:
        return None
    return Afd(office, discussion.issued, discussion.aviation)


async def gather(
    awc: Awc,
    nws: Nws,
    *,
    title: str,
    airports: list[str],
    config: dict,
    include: dict,
    now: datetime | None = None,
    mountain=None,
) -> BriefData:
    now = now or datetime.now(timezone.utc)
    problems: list[str] = []
    brief_cfg = config.get("brief", {})
    radius = config.get("alerts", {}).get("area", {}).get("radius_nm", HAZARD_RADIUS_NM)

    stations = await _guard(problems, "Airport lookup", awc.stations(airports)) or {}
    metars, tafs = await asyncio.gather(
        _guard(problems, "METARs", awc.metars(airports)),
        _guard(problems, "TAFs", awc.tafs(airports)) if include.get("tafs", True) else _none(),
    )
    data = BriefData(
        title=title,
        generated=now,
        tz=fmt.zone(config.get("timezone")),
        units=fmt.Units.from_config(config),
        airports=[AirportBrief(a, (metars or {}).get(a), (tafs or {}).get(a)) for a in airports],
        stations=stations,
        problems=problems,
    )
    points = data.points
    first = stations.get(airports[0]).position if airports and airports[0] in stations else None
    us_states = {s.state for s in stations.values() if s.country == "US" and s.state}

    tasks = {}
    if include.get("airmets_sigmets", True) and points:
        tasks["hazards"] = _hazards(awc, points, radius, now)
    if include.get("pireps", True) and airports:
        tasks["pireps"] = awc.pireps(airports[0], PIREP_RADIUS_NM, PIREP_AGE_HOURS)
    if include.get("winds_aloft", True):
        tasks["winds"] = winds_for(awc, first, brief_cfg.get("winds_aloft_station"))
    if include.get("nws_alerts", True) and us_states and points:
        tasks["alerts"] = us_alerts(nws, us_states, points, radius)
    if include.get("afd_aviation", True) and first and us_states:
        tasks["afd"] = afd_for(nws, first)
    if mountain is not None:
        tasks["mountain"] = mountain
    names = {
        "hazards": "AIRMETs/SIGMETs",
        "pireps": "PIREPs",
        "winds": "Winds aloft",
        "alerts": "NWS alerts",
        "afd": "Forecast discussion",
        "mountain": "Mountain data",
    }
    results = await asyncio.gather(*(_guard(problems, names[k], c) for k, c in tasks.items()))
    for key, value in zip(tasks, results):
        if key == "hazards" and value is not None:
            data.hazards = value
        elif key == "pireps" and value is not None:
            data.pireps = value
        elif key == "winds":
            data.winds = value
        elif key == "alerts" and value is not None:
            data.alerts = value
        elif key == "afd":
            data.afd = value
        elif key == "mountain":
            data.mountain = value
    return data


async def _none():
    return None


async def _hazards(awc: Awc, points, radius, now) -> list[Hazard]:
    sigmets, gairmets, cwas = await asyncio.gather(awc.sigmets(), awc.gairmets(), awc.cwas())
    return hazards.near([*sigmets, *gairmets, *cwas], points, radius, now, hours=HAZARD_HOURS)


def notable_pireps(pireps: list[Pirep]) -> list[Pirep]:
    """Urgent reports, moderate-or-worse turbulence, any icing, and mountain wave."""
    def score(p: Pirep) -> int:
        s = 0
        if p.urgent:
            s += 10
        if any(w in p.turbulence for w in ("SEV", "EXTRM")):
            s += 8
        elif "MOD" in p.turbulence:
            s += 4
        if p.icing and p.icing not in ("NEG", "NEGclr"):
            s += 3
        if p.mentions_wave:
            s += 3
        return s

    def newest(p: Pirep) -> float:
        return p.observed.timestamp() if p.observed else 0

    ranked = sorted(pireps, key=lambda p: (-score(p), -newest(p)))
    return [p for p in ranked if score(p) > 0]
