"""Tool results for Claude: compact data, and what deserves attention.

Pilots get their full briefing elsewhere, so a reply should lead with what's
out of the ordinary and summarise the rest. Deciding what's out of the
ordinary is done here, in code, rather than left to the model: every result
carries an `attention` list, and the system prompt tells Claude to start from
it. The thresholds are rules of thumb, collected here to be argued about:

* Flight category worse than VFR at a planned time, a thunderstorm, or a
  TEMPO/PROB group that would make it so.
* Gusts of 20 kt or more; a crosswind over the aircraft's demonstrated
  maximum (15 kt if no aircraft is known); any tailwind on the best runway.
* Density altitude of 8,000 ft or more, or 2,000 ft or more above the field.
* Hazards and NWS alerts along the route; notable PIREPs; passes at caution
  or no-go; mountain wave risk elevated or high.
* Cruise less than 2,000 ft above the highest known point on the route.
* Wind at cruise of 30 kt or more.
* A flight needing more fuel, with reserve, than the aircraft carries.
* Takeoff over 50 ft needing 70% or more of the runway, or a climb rate under
  500 fpm; any table result that is only a lower bound.
"""

from __future__ import annotations

from datetime import datetime

from ..flightwx import AirportOutlook, RouteWeather
from ..hazards import label as hazard_label
from ..hazards import weight as hazard_weight
from ..sources.awc import TafPeriod
from ..wx import fmt, runways

GUST_KT = 20
DEFAULT_CROSSWIND_KT = 15
DA_HIGH_FT = 8000
DA_ABOVE_FIELD_FT = 2000
RIDGE_CLEARANCE_FT = 2000
STRONG_CRUISE_WIND_KT = 30
TAKEOFF_RUNWAY_SHARE = 0.7
CLIMB_MIN_FPM = 500


def when(dt: datetime | None, tz) -> str | None:
    """"Sat 08:00 MDT (14:00Z)"."""
    if dt is None:
        return None
    return f"{dt.astimezone(tz).strftime('%a %H:%M %Z')} ({fmt.zulu(dt)})"


def _period(p: TafPeriod | None) -> dict | None:
    if p is None:
        return None
    out = {
        "wind": fmt.wind(p.wind_dir, p.wind_speed, p.wind_gust),
        "visibility": fmt.visibility(p.visibility_sm, p.visibility_text),
        "clouds": fmt.clouds(p.clouds),
        "category": p.flight_category,
    }
    if p.weather:
        out["weather"] = p.weather
    if p.wind_shear:
        out["wind_shear"] = p.wind_shear
    if p.change in ("TEMPO", "PROB") or p.probability:
        out["group"] = f"{p.change or ''}{p.probability or ''}".strip()
    return out


def airport(o: AirportOutlook, tz, *, crosswind_limit: float | None, now: datetime) -> tuple[dict, list[str]]:
    """One airport's outlook at its time, and its attention items."""
    attention: list[str] = []
    out: dict = {"airport": o.ident, "name": o.name, "role": o.role, "time": when(o.eta, tz)}
    if o.elevation_ft is not None:
        out["elevation_ft"] = round(o.elevation_ft)
    m = o.metar
    if m is not None:
        out["metar"] = {"raw": m.raw, "age_min": round(m.age_minutes(now) or 0), "category": m.flight_category}
    if o.taf_period is not None or o.taf_conditional:
        out["taf_at_time"] = {
            "prevailing": _period(o.taf_period),
            "conditional": [_period(p) for p in o.taf_conditional],
        }
    elif o.taf is not None:
        out["taf_note"] = "TAF does not cover this time"
    if o.hourly is not None:
        h = o.hourly
        out["nws_hourly"] = {
            "summary": h.summary, "temp_c": h.temp_c,
            "wind": fmt.wind(round(h.wind_dir) if h.wind_dir is not None else None, h.wind_kt),
            "precip_pct": h.precip_pct,
        }
    if o.wind is not None:
        out["wind_used"] = {"value": fmt.wind(*o.wind), "source": o.wind_source}
    if o.density_alt_ft is not None:
        out["density_altitude_ft"] = round(o.density_alt_ft, -1)
        out["density_altitude_source"] = o.density_source
    if o.runway is not None:
        out["runway"] = runways.describe(o.runway) + f", {o.runway.end.length_ft} ft long"

    name = f"{o.ident} ({o.role}, {when(o.eta, tz)})"
    # Category at the time: a fresh METAR, else the TAF's prevailing group.
    fresh = m is not None and o.wind_source == "METAR"
    cat = m.flight_category if fresh else (o.taf_period.flight_category if o.taf_period else None)
    if cat and cat != "VFR":
        attention.append(f"{name}: {cat} expected")
    wx = (m.weather if fresh else "") + " " + (o.taf_period.weather if o.taf_period else "")
    if "TS" in wx:
        attention.append(f"{name}: thunderstorms")
    for p in o.taf_conditional:
        if p.flight_category != "VFR" or "TS" in (p.weather or ""):
            attention.append(f"{name}: {p.change or 'PROB'}{p.probability or ''} group with {p.flight_category}"
                             + (f" and {p.weather}" if p.weather else ""))
    if o.wind and (o.wind[2] or 0) >= GUST_KT:
        attention.append(f"{name}: gusts {o.wind[2]} kt")
    if o.runway is not None:
        limit = crosswind_limit or DEFAULT_CROSSWIND_KT
        worst_cross = max(o.runway.crosswind, o.runway.gust_crosswind or 0)
        if worst_cross > limit:
            basis = (f"over the {limit:g} kt demonstrated" if crosswind_limit
                     else f"over {limit:g} kt (the POH gives no demonstrated figure)")
            attention.append(f"{name}: crosswind {round(worst_cross)} kt, {basis}")
        if o.runway.headwind < -0.5:
            attention.append(f"{name}: tailwind {round(-o.runway.headwind)} kt on the best runway")
    if o.density_alt_ft is not None and o.elevation_ft is not None and (
        o.density_alt_ft >= DA_HIGH_FT or o.density_alt_ft - o.elevation_ft >= DA_ABOVE_FIELD_FT
    ):
        attention.append(f"{name}: density altitude {round(o.density_alt_ft, -2):,.0f} ft")
    if o.role in ("departure", "stop", "destination", "airport") and o.taf_period is None and o.hourly is None and not fresh:
        attention.append(f"{name}: no forecast covers this time")
    return out, attention


def route(rw: RouteWeather, tz, *, aircraft=None, now: datetime) -> dict:
    attention: list[str] = []
    xwind = aircraft.max_demo_crosswind_kt if aircraft else None
    airports = []
    for o in rw.airports:
        data, items = airport(o, tz, crosswind_limit=xwind, now=now)
        airports.append(data)
        attention.extend(items)

    log = rw.log
    flights = []
    usable = aircraft.limits.usable_gal if aircraft else None
    for f in log.flights:
        flights.append({
            "from": f.origin, "to": f.dest, "departs": when(f.departs, tz), "arrives": when(f.arrives, tz),
            "distance_nm": f.distance_nm, "minutes": round(f.minutes),
            "fuel_gal": {"taxi_and_climb": f.fuel_taxi_climb_gal, "enroute": f.fuel_enroute_gal,
                         "reserve": f.reserve_gal, "required": round(f.fuel_required_gal, 1),
                         "burned": round(f.fuel_burned_gal, 1)},
            "legs": [
                {"to": leg.dest, "nm": leg.distance_nm, "true_course": leg.true_course,
                 "mag_heading": leg.magnetic_heading, "groundspeed": leg.groundspeed,
                 "minutes": round(leg.minutes), "wind": fmt.wind(round(leg.wind_dir), round(leg.wind_kt))
                 if leg.wind_dir is not None else "none"}
                for leg in f.legs
            ],
        })
        if usable is not None and f.fuel_required_gal > usable:
            attention.append(
                f"Fuel: {f.origin}→{f.dest} needs {f.fuel_required_gal:.1f} gal with reserve; "
                f"{usable:g} gal usable"
            )

    if any(w.wind is None for w in rw.leg_winds):
        attention.append("No winds-aloft forecast reaches the flight time: times and fuel assume no wind")
    if rw.performance.assumed:
        attention.append("No aircraft given: times and fuel use a generic 110 kt and 10 gph")
    for w in rw.leg_winds:
        if w.wind and w.wind.speed_kt >= STRONG_CRUISE_WIND_KT:
            attention.append(f"Winds aloft {w.origin}→{w.dest}: {fmt.wind(w.wind.direction, w.wind.speed_kt)} at cruise")
    if rw.highest and rw.cruise_alt_ft < rw.highest[1] + RIDGE_CLEARANCE_FT:
        attention.append(
            f"Cruise {rw.cruise_alt_ft:,.0f} ft is less than {RIDGE_CLEARANCE_FT:,} ft above "
            f"{rw.highest[0]} ({rw.highest[1]:,.0f} ft)"
        )
    if aircraft and aircraft.service_ceiling_ft and rw.cruise_alt_ft > aircraft.service_ceiling_ft:
        attention.append(f"Cruise is above the {aircraft.service_ceiling_ft:,.0f} ft service ceiling")

    hazards = []
    for h in rw.hazards or []:
        item = f"{hazard_label(h)}" + (f" ({h.due_to})" if h.due_to else "") + (
            f", valid until {when(h.valid_to, tz)}" if h.valid_to else "")
        hazards.append(item)
        if hazard_weight(h) >= 2:
            attention.append(f"Along the route: {item}")
    alerts = [f"{a.event}: {a.headline}" for a in rw.alerts or []]
    attention.extend(f"NWS: {a}" for a in alerts)
    pireps = [p.raw for p in (rw.pireps or [])[:6]]
    if pireps:
        attention.append(f"{len(rw.pireps)} notable PIREP(s) near the route (turbulence, icing or wave)")
    passes = [{"pass": p.name, "elevation_ft": p.elevation_ft, "status": p.status, "why": p.why()} for p in rw.passes]
    for p in rw.passes:
        if p.status in ("caution", "no-go"):
            attention.append(f"{p.name}: {p.status} now ({'; '.join(p.why())})")
    if rw.wave and rw.wave.level in ("elevated", "high"):
        attention.append(f"Mountain wave risk {rw.wave.level}: {'; '.join(rw.wave.reasons)}")

    out = {
        "route": " → ".join(p.label() for p in rw.points),
        "departure": when(rw.departure, tz),
        "cruise_altitude_ft": rw.cruise_alt_ft,
        "performance": {"tas_kt": rw.performance.tas_kt, "gph": rw.performance.burn_gph,
                        "assumed": rw.performance.assumed},
        "attention": attention or ["Nothing outside normal parameters found."],
        "flights": flights,
        "total": {"distance_nm": round(log.distance_nm, 1), "flying_minutes": round(log.minutes)},
        "winds_aloft": [
            {"leg": f"{w.origin}→{w.dest}", "site": w.site, "forecast": w.forecast, "use": w.use,
             "covers_leg_time": w.covers,
             "wind": fmt.wind(w.wind.direction, w.wind.speed_kt) if w.wind else None,
             "temp_c": w.wind.temp_c if w.wind else None}
            for w in rw.leg_winds
        ],
        "airports": airports,
        "hazards": hazards,
        "nws_alerts": alerts,
        "pireps": pireps,
        "passes": passes,
    }
    if rw.wave:
        out["mountain_wave"] = {"level": rw.wave.level, "why": rw.wave.reasons}
    if rw.highest:
        out["highest_known_point"] = {"name": rw.highest[0], "elevation_ft": round(rw.highest[1])}
    if rw.afds:
        out["forecast_discussion_aviation"] = [
            {"office": a.office, "issued": when(a.issued, tz), "text": a.aviation[:1500]} for a in rw.afds
        ]
    if log.notes:
        out["notes"] = log.notes
    if rw.problems:
        out["missing_data"] = rw.problems
    return out


def outlook_only(outlooks: list[AirportOutlook], problems: list[str], tz, *, aircraft=None, now: datetime) -> dict:
    attention: list[str] = []
    airports = []
    for o in outlooks:
        data, items = airport(o, tz, crosswind_limit=aircraft.max_demo_crosswind_kt if aircraft else None, now=now)
        airports.append(data)
        attention.extend(items)
    out = {"attention": attention or ["Nothing outside normal parameters found."], "airports": airports}
    if problems:
        out["missing_data"] = problems
    return out
