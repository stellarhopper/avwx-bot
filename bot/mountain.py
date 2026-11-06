"""Mountain-flying assessment: passes, ridge-level winds, wave risk, density altitude.

These are **heuristics for awareness**, built from the rules of thumb taught in
mountain-flying courses. Passes are rated Looks OK, Marginal or Adverse: words
for the conditions, never for the decision, which is the pilot in command's.
The thresholds are collected here so they're easy to find and argue about:

* **Ridge-level wind.** Wind at mountaintop level of 25 kt or more is adverse
  for light aircraft; 15–24 kt is marginal (expect turbulence and downdrafts).
  "Mountaintop level" is the first winds-aloft level at or above the pass.
* **Mountain wave.** Wind blowing across the ridge (the region's
  `cross_from_deg`–`cross_to_deg` sector) at 12,000–18,000 ft. From 25 kt
  the risk is elevated; from 40 kt it is high. Several things also raise the
  risk to at least elevated: a G-AIRMET Tango for mountain wave, a PIREP of wave
  or moderate-or-worse turbulence in the region, and a forecast discussion
  that mentions wave or rotor.
* **Pass weather.** The station on the pass decides most of it: IFR or LIFR, or
  a thunderstorm, is adverse; MVFR or snow is marginal. A Sierra (mountains
  obscured) or IFR G-AIRMET over the pass is adverse. IFR on either approach
  side is marginal, because that is where you'd turn back to.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import clock
from . import hazards as hz
from .sources.awc import Awc, Hazard, Metar, Pirep
from .sources.http import SourceError
from .sources.nws import Nws
from .sources.windsaloft import Wind
from .wx import geo

RIDGE_MARGINAL_KT = 15
RIDGE_ADVERSE_KT = 25
WAVE_ELEVATED_KT = 25
WAVE_HIGH_KT = 40
OBSCURATION_RADIUS_NM = 5
HAZARD_RADIUS_NM = 30
# Station reports older than this don't count. AWC hands back a station's
# last report however old it is, and a station that died weeks ago (KC07 on
# Rabbit Ears, in Nov 2026) would otherwise rate its pass on that.
MAX_OBS_AGE_MIN = 180

STATUS_ORDER = ("ok", "marginal", "adverse")
STATUS_LABELS = {"ok": "Looks OK", "marginal": "Marginal", "adverse": "Adverse", "unknown": "No data"}

_WAVE_WORDS = re.compile(r"\b(mountain wave|mtn wave|rotors?|lenticular|wave clouds?)\b", re.IGNORECASE)


@dataclass
class PassStatus:
    id: str
    name: str
    elevation_ft: int
    status: str = "unknown"
    reasons: list[tuple[str, str]] = field(default_factory=list)  # (status, why)
    on_pass: Metar | None = None
    sides: dict[str, Metar | None] = field(default_factory=dict)
    ridge_wind: Wind | None = None

    def worsen(self, status: str, reason: str) -> None:
        if self.status == "unknown" or STATUS_ORDER.index(status) > STATUS_ORDER.index(self.status):
            self.status = status
        self.reasons.append((status, reason))

    def why(self, limit: int = 2) -> list[str]:
        """The reasons behind the status, worst first: what made it adverse, not what was fine."""
        worst = [r for st, r in self.reasons if st == self.status]
        return worst[:limit]


@dataclass
class WaveRisk:
    level: str  # low | elevated | high | unknown
    reasons: list[str]


@dataclass
class DensityAltitude:
    station: str
    elevation_ft: float
    temp_c: float | None
    density_alt_ft: float | None


@dataclass
class MountainBoard:
    region_id: str
    region_name: str
    generated: datetime
    passes: list[PassStatus]
    ridge_winds: dict[str, dict[int, Wind]]
    wave: WaveRisk
    density: list[DensityAltitude]
    weathercams_url: str | None
    problems: list[str] = field(default_factory=list)


def method(region: dict) -> dict:
    """How the board is worked out, in words, from the same constants and
    region data the code uses, so an explanation can't drift from what it
    does. The chat explains from it, and the pass board's legend shows it."""
    ridge = region.get("ridge") or {}
    start, end = ridge.get("cross_from_deg", 240), ridge.get("cross_to_deg", 320)
    levels = " and ".join(f"{a:,} ft" for a in ridge.get("levels_ft", [12000, 18000]))
    return {
        "nature": "Rules of thumb from mountain-flying courses, describing conditions for awareness; the "
                  "decision is the pilot in command's. Each pass gets the worst status any rule gives it.",
        "statuses": [
            {"status": "ok", "label": STATUS_LABELS["ok"], "conditions": [
                "None of the conditions below, with current data for the pass",
            ]},
            {"status": "marginal", "label": STATUS_LABELS["marginal"], "conditions": [
                f"The station on the pass reports MVFR, snow or freezing precipitation, or gusts of "
                f"{RIDGE_ADVERSE_KT} kt or more",
                "An airport on either approach side (the way you'd turn back) reports IFR or LIFR",
                f"{RIDGE_MARGINAL_KT}–{RIDGE_ADVERSE_KT - 1} kt at pass level",
            ]},
            {"status": "adverse", "label": STATUS_LABELS["adverse"], "conditions": [
                "The station on the pass reports IFR, LIFR or a thunderstorm",
                f"A mountain-obscuration or IFR G-AIRMET within {OBSCURATION_RADIUS_NM} nm, now or in the "
                "next 3 hours",
                f"{RIDGE_ADVERSE_KT} kt or more at pass level",
            ]},
            {"status": "unknown", "label": STATUS_LABELS["unknown"], "conditions": [
                f"No report under {MAX_OBS_AGE_MIN // 60} hours old from the pass or either approach side, "
                "and none of the conditions above",
            ]},
        ],
        "pass_level_wind": "The first winds-aloft forecast level at or above the pass, from the forecast "
                           "site the region file assigns it.",
        "wave_risk_rules": [
            f"Wind from {start:03d}-{end:03d}° (across the ridge) at {levels} at the region's winds-aloft "
            f"sites: {WAVE_ELEVATED_KT} kt or more is elevated, {WAVE_HIGH_KT} kt or more is high.",
            "A G-AIRMET Tango for turbulence due to mountain wave raises it to at least elevated.",
            f"PIREPs within {region.get('pirep_radius_nm', 120)} nm of {region.get('pirep_center', 'KDEN')} in "
            "the last 3 hours: severe turbulence makes it high; wave reports or moderate turbulence, elevated.",
            "The NWS forecast discussion mentioning mountain wave, rotor or lenticulars raises it to elevated.",
        ],
        "data_sources": [
            "METARs from the stations on and either side of each pass (aviationweather.gov)",
            "Winds and temperatures aloft, the FB forecast (aviationweather.gov)",
            "G-AIRMETs (aviationweather.gov)",
            "PIREPs (aviationweather.gov)",
            "The NWS Area Forecast Discussion (weather.gov)",
        ],
        "region": region.get("name", region.get("id")),
    }


def in_sector(direction: int | None, start: int, end: int) -> bool:
    if direction is None:
        return False
    d = direction % 360
    if start <= end:
        return start <= d <= end
    return d >= start or d <= end


def ridge_level(levels: dict[int, Wind], elevation_ft: int) -> Wind | None:
    """The first forecast level at or above the pass."""
    for alt in sorted(levels):
        if alt >= elevation_ft:
            return levels[alt]
    return None


def assess_pass(p: PassStatus, *, obscured_by: list[str], ridge: Wind | None) -> PassStatus:
    m = p.on_pass
    if m is not None:
        cat = m.flight_category
        wx = m.weather.upper()
        if cat in ("IFR", "LIFR"):
            p.worsen("adverse", f"{m.station} {cat}")
        elif cat == "MVFR":
            p.worsen("marginal", f"{m.station} MVFR")
        else:
            p.worsen("ok", f"{m.station} {cat}")
        if "TS" in wx:
            p.worsen("adverse", f"thunderstorm at {m.station}")
        elif "SN" in wx or "FZ" in wx:
            p.worsen("marginal", f"{wx.strip()} at {m.station}")
        if m.wind_gust and m.wind_gust >= RIDGE_ADVERSE_KT:
            p.worsen("marginal", f"gusts {m.wind_gust} kt on the pass")
    for side, sm in p.sides.items():
        if sm is not None and sm.flight_category in ("IFR", "LIFR"):
            p.worsen("marginal", f"{side} side {sm.station} {sm.flight_category}")
        elif sm is not None and p.status == "unknown":
            p.worsen("ok", f"{side} side {sm.station} {sm.flight_category}")
    for what in obscured_by:
        p.worsen("adverse", what)
    p.ridge_wind = ridge
    if ridge is not None and not ridge.light_variable:
        if ridge.speed_kt >= RIDGE_ADVERSE_KT:
            p.worsen("adverse", f"{ridge.speed_kt} kt at {ridge.altitude_ft // 1000}k")
        elif ridge.speed_kt >= RIDGE_MARGINAL_KT:
            p.worsen("marginal", f"{ridge.speed_kt} kt at {ridge.altitude_ft // 1000}k")
    return p


def assess_wave(
    region: dict,
    ridge_winds: dict[str, dict[int, Wind]],
    *,
    tango: list[Hazard],
    pireps: list[Pirep],
    discussion: str | None,
) -> WaveRisk:
    ridge = region.get("ridge") or {}
    start, end = ridge.get("cross_from_deg", 240), ridge.get("cross_to_deg", 320)
    levels = ridge.get("levels_ft", [12000, 18000])
    reasons: list[str] = []
    level = "low" if ridge_winds else "unknown"

    def raise_to(new: str) -> None:
        nonlocal level
        order = ("unknown", "low", "elevated", "high")
        if order.index(new) > order.index(level):
            level = new

    strongest: tuple[int, str, int, int] | None = None
    for site, winds in ridge_winds.items():
        for alt in levels:
            w = winds.get(alt)
            if w is None or not in_sector(w.direction, start, end):
                continue
            if strongest is None or w.speed_kt > strongest[0]:
                strongest = (w.speed_kt, site, alt, w.direction)
    if strongest:
        speed, site, alt, direction = strongest
        if speed >= WAVE_HIGH_KT:
            raise_to("high")
        elif speed >= WAVE_ELEVATED_KT:
            raise_to("elevated")
        if speed >= WAVE_ELEVATED_KT:
            reasons.append(f"{direction:03d}° {speed} kt across the ridge at {alt // 1000}k ({site})")

    wave_tango = [h for h in tango if h.hazard == "TURB-LO" and h.due_to and "WAVE" in h.due_to.upper()]
    if wave_tango:
        raise_to("elevated")
        reasons.append("G-AIRMET Tango for mountain wave")
    severe = [p for p in pireps if "SEV" in p.turbulence or "EXTRM" in p.turbulence]
    wave_reps = [p for p in pireps if p.mentions_wave]
    moderate = [p for p in pireps if "MOD" in p.turbulence]
    if severe:
        raise_to("high")
        reasons.append(f"{len(severe)} PIREP{'s' if len(severe) > 1 else ''} of severe turbulence")
    if wave_reps:
        raise_to("elevated")
        reasons.append(f"{len(wave_reps)} PIREP{'s' if len(wave_reps) > 1 else ''} mentioning wave")
    elif moderate:
        raise_to("elevated")
        reasons.append(f"{len(moderate)} PIREP{'s' if len(moderate) > 1 else ''} of moderate turbulence")
    if discussion and _WAVE_WORDS.search(discussion):
        raise_to("elevated")
        word = _WAVE_WORDS.search(discussion).group(0).lower()
        reasons.append(f"forecast discussion mentions {word}")
    if level == "low" and not reasons:
        reasons.append("no strong cross-ridge flow, wave turbulence or reports")
    return WaveRisk(level, reasons)


def obscurations(p: dict, hazards: list[Hazard]) -> list[str]:
    point = (p["lat"], p["lon"])
    out = []
    for h in hazards:
        if h.hazard not in ("MT_OBSC", "IFR") or len(h.polygon) < 3:
            continue
        if geo.distance_to_polygon_nm(point, h.polygon) <= OBSCURATION_RADIUS_NM:
            label = "mountains obscured (G-AIRMET)" if h.hazard == "MT_OBSC" else "IFR (G-AIRMET)"
            if label not in out:
                out.append(label)
    return out


async def _guard(problems: list[str], what: str, coro, default):
    try:
        return await coro
    except SourceError as exc:
        problems.append(f"{what}: {exc}")
        return default


async def build(awc: Awc, nws: Nws, region: dict, cfg: dict, now: datetime | None = None) -> MountainBoard:
    now = now or clock.now()
    wanted = set(cfg.get("mountain", {}).get("passes") or [])
    passes = [p for p in region.get("passes", []) if not wanted or p["id"] in wanted]
    problems: list[str] = []

    stations = {s for p in passes for s in (*p.get("on_pass", []), *p.get("east", []), *p.get("west", []))}
    stations.update(region.get("mountain_airports", []))
    offices = region.get("nws_offices") or []

    metars, forecast, gairmets, pireps, discussion = await asyncio.gather(
        _guard(problems, "METARs", awc.metars(stations), {}),
        _guard(problems, "Winds aloft", awc.winds_aloft(), None),
        _guard(problems, "G-AIRMETs", awc.gairmets(), []),
        _guard(problems, "PIREPs", awc.pireps(region.get("pirep_center", "KDEN"), region.get("pirep_radius_nm", 120), 3), []),
        _guard(problems, "Forecast discussion", nws.discussion(offices[0]), None) if offices else _none(),
    )

    metars = {k: m for k, m in metars.items() if (m.age_minutes(now) or 0) <= MAX_OBS_AGE_MIN}

    ridge_winds = {}
    if forecast is not None:
        for site in region.get("winds_aloft_sites", []):
            levels = forecast.site(site)
            if levels:
                ridge_winds[site] = levels

    points = [(p["lat"], p["lon"]) for p in passes]
    local_hazards = hz.near(gairmets, points, HAZARD_RADIUS_NM, now, hours=3) if points else []

    statuses = []
    for p in passes:
        status = PassStatus(p["id"], p["name"], p["elevation_ft"])
        status.on_pass = next((metars[s] for s in p.get("on_pass", []) if s in metars), None)
        status.sides = {
            "east": next((metars[s] for s in p.get("east", []) if s in metars), None),
            "west": next((metars[s] for s in p.get("west", []) if s in metars), None),
        }
        site_winds = ridge_winds.get(p.get("winds", ""), {})
        statuses.append(assess_pass(status, obscured_by=obscurations(p, local_hazards),
                                    ridge=ridge_level(site_winds, p["elevation_ft"])))

    wave = assess_wave(
        region,
        ridge_winds,
        tango=[h for h in local_hazards if h.hazard.startswith("TURB")],
        pireps=pireps,
        discussion=discussion.text if discussion else None,
    )
    density = []
    for station in region.get("mountain_airports", []):
        m = metars.get(station)
        if m is None or m.elevation_ft is None:
            continue
        density.append(DensityAltitude(station, m.elevation_ft, m.temp_c, m.density_altitude_ft))

    return MountainBoard(
        region_id=region["id"],
        region_name=region["name"],
        generated=now,
        passes=statuses,
        ridge_winds=ridge_winds,
        wave=wave,
        density=density,
        weathercams_url=region.get("weathercams_url"),
        problems=problems,
    )


async def _none():
    return None
