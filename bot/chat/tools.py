"""The tools Claude may call, and the code behind them.

Every tool is **read-only** and reaches only fixed sources: the weather APIs
this bot already uses, the club's own files, and the card renderers. There is
no tool that fetches a URL, reads an arbitrary path, posts a message or looks
at another thread, so text that tries to steer the model (a planted PIREP
remark, a line in a manual, a member's "ignore your instructions") has nothing
useful to reach. Inputs are validated again here even though the schemas are
strict: the schema is a promise from the API, this is the bot keeping its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import flightwx, hazards, mountain, regions
from .. import route as route_mod
from ..fleet import Aircraft, Club, FleetCache
from ..render import cards, theme
from ..render import mountain as mountain_render
from ..render import winds as winds_render
from ..sources.awc import Awc
from ..sources.http import SourceError
from ..sources.nws import Nws
from ..wx import fmt, navlog, perf, runways, wb
from ..wx.density_alt import density_altitude, pressure_altitude
from . import manuals, summary

log = logging.getLogger(__name__)

MAX_ROUTE_POINTS = 20
MAX_AIRPORTS = 6
MAX_CARDS_PER_TURN = 2
TAKEOFF_SHARE = summary.TAKEOFF_RUNWAY_SHARE
PLAN_AHEAD = timedelta(days=7)
STANDARD_ALTIMETER = 29.92
_TIME = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})\s*$")
_TEXT_ITEM = re.compile(r"^[\w\s.,°'/()+-]{1,60}$")


class ToolError(ValueError):
    """Bad input, or nothing to answer with. The message goes back to Claude."""


@dataclass
class Attachment:
    filename: str
    data: bytes
    alt: str


@dataclass
class ToolContext:
    awc: Awc
    nws: Nws
    fleet: FleetCache
    config: dict  # the guild's effective config
    now: Callable[[], datetime]
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def tz(self):
        return fmt.zone(self.config.get("timezone"))

    @property
    def units(self) -> fmt.Units:
        return fmt.Units.from_config(self.config)


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_TAIL = {"type": "string", "description": "Club aircraft tail number, e.g. N648F."}
_LOCAL = {"type": "string", "description": "Local time at the departure, as YYYY-MM-DD HH:MM."}
_POINTS = {
    "type": "array", "items": {"type": "string"},
    "description": "Route points in order, as the member said them: identifiers (KBJC, SBS, VPROL), "
                   "airport or town names (Granby), pass names (Rollins Pass), or coordinates (39.93N 105.68W).",
}

DEFINITIONS = [
    _tool(
        "resolve_route",
        "Turn route points as the member said them into places, without planning anything. Each point comes "
        "back resolved, ambiguous (with candidates) or unresolved. Use it when a route has names rather than "
        "identifiers, then ask the member about anything ambiguous or unresolved before planning. Never guess.",
        {"points": _POINTS},
        ["points"],
    ),
    _tool(
        "flight_plan",
        "Plan a flight: resolves the route, builds a nav log (courses, wind correction, ground speed, times, "
        "fuel per flight with the club's reserve) using winds aloft at cruise, and gathers the weather at each "
        "airport at its arrival time, hazards and NWS alerts along the route, notable PIREPs, and the status "
        "of any mountain pass crossed. Returns an `attention` list of everything outside normal parameters. "
        "Without a tail number it uses generic speeds and says so. Refuses while route points are unclear.",
        {
            "route": _POINTS,
            "departure_local": _LOCAL,
            "cruise_altitude_ft": {"type": "integer", "description": "Planned cruise altitude, MSL."},
            "tail": _TAIL,
            "power_pct": {"type": "number", "description": "Cruise power setting in percent; default is the aircraft's."},
            "stops": {"type": "array", "items": {"type": "string"},
                      "description": "Intermediate route points where the aircraft lands. Others are flown over."},
        },
        ["route", "departure_local", "cruise_altitude_ft"],
    ),
    _tool(
        "airport_weather",
        "Weather at up to six airports at one time: the METAR, the TAF group in force (and any TEMPO/PROB), "
        "the NWS hourly forecast where the TAF doesn't reach, wind on the best runway, and density altitude. "
        "Returns an `attention` list. Leave at_local out for now.",
        {
            "airports": {"type": "array", "items": {"type": "string"}, "description": "Identifiers or names."},
            "at_local": _LOCAL,
            "tail": {**_TAIL, "description": "Optional: compares crosswinds with this aircraft's demonstrated limit."},
        },
        ["airports"],
    ),
    _tool(
        "weight_and_balance",
        "Weight and CG at ramp, takeoff and landing for a club aircraft, checked against every limit and the "
        "CG envelope. Station ids come from the `aircraft` tool. burn_gal is the fuel the flight burns, from "
        "flight_plan.",
        {
            "tail": _TAIL,
            "loads": {"type": "array", "items": {
                "type": "object",
                "properties": {"station": {"type": "string"}, "lb": {"type": "number"}},
                "required": ["station", "lb"], "additionalProperties": False,
            }},
            "fuel_gal": {"type": "number", "description": "Fuel at engine start."},
            "burn_gal": {"type": "number", "description": "Fuel burned in flight, excluding taxi."},
        },
        ["tail", "loads", "fuel_gal", "burn_gal"],
    ),
    _tool(
        "takeoff_landing",
        "Takeoff and landing distances and climb rate from the aircraft's POH tables, for an airport at a "
        "time: pressure altitude, temperature and headwind come from the weather. Results past the edge of a "
        "table come back as lower bounds, never extrapolated. Compares the distances with the runway.",
        {
            "tail": _TAIL,
            "airport": {"type": "string"},
            "weight_lb": {"type": "number"},
            "at_local": _LOCAL,
            "runway": {"type": "string", "description": "Runway end, e.g. 27. Default: the best for the wind."},
            "surface": {"type": "string", "enum": ["paved", "grass"]},
        },
        ["tail", "airport", "weight_lb"],
    ),
    _tool(
        "aircraft",
        "A club aircraft's profile: type, equipment, limits, loading stations, speeds, cruise performance, "
        "the performance tables it has, and its manuals with their section index.",
        {"tail": _TAIL},
        ["tail"],
    ),
    _tool(
        "search_manual",
        "Keyword search over a club aircraft's manuals (POH, AFMS supplements, avionics guides). Returns "
        "matching pages with snippets, and matching sections from the manual index. Scanned manuals have no "
        "text to search: use their section index with read_manual_pages.",
        {"tail": _TAIL, "query": {"type": "string"}},
        ["tail", "query"],
    ),
    _tool(
        "read_manual_pages",
        f"Read up to {manuals.MAX_PAGES} pages of a club aircraft's manual, as the pages themselves "
        "(tables and diagrams included). Pages are counted from 1 in the PDF.",
        {"tail": _TAIL, "manual": {"type": "string"}, "first_page": {"type": "integer"},
         "last_page": {"type": "integer"}},
        ["tail", "manual", "first_page", "last_page"],
    ),
    _tool(
        "attach_card",
        "Attach one of the bot's weather images to your reply: a METAR or TAF card for up to four airports, "
        "the winds aloft near an airport, or the mountain pass board. Use it when a picture helps; don't "
        "repeat the card's contents in text.",
        {"kind": {"type": "string", "enum": ["metar", "taf", "winds", "mountain"]},
         "airports": {"type": "array", "items": {"type": "string"}}},
        ["kind", "airports"],
    ),
]


# ----------------------------------------------------------------- inputs


def _text(args: dict, key: str, *, required: bool = True, limit: int = 60) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ToolError(f"`{key}` must be a short piece of text.")
    return value.strip()


def _items(args: dict, key: str, *, most: int, required: bool = True) -> list[str]:
    value = args.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list) or not value or len(value) > most:
        raise ToolError(f"`{key}` must be a list of 1 to {most} items.")
    out = []
    for v in value:
        if not isinstance(v, str) or not _TEXT_ITEM.match(v.strip()):
            raise ToolError(f"`{str(v)[:30]}` in `{key}` isn't a place I can look up.")
        out.append(v.strip())
    return out


def _number(args: dict, key: str, lo: float, hi: float, *, required: bool = True) -> float | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise ToolError(f"`{key}` must be a number from {lo:g} to {hi:g}.")
    return float(value)


def _time(ctx: ToolContext, args: dict, key: str) -> datetime:
    raw = args.get(key)
    now = ctx.now()
    if raw is None:
        return now
    m = _TIME.match(str(raw))
    if not m:
        raise ToolError(f"`{key}` must look like 2026-10-31 08:00 (local time).")
    try:
        t = datetime(*(int(g) for g in m.groups()), tzinfo=ctx.tz)
    except ValueError:
        raise ToolError(f"`{raw}` isn't a real date and time.") from None
    if t < now - timedelta(hours=2) or t > now + PLAN_AHEAD:
        raise ToolError(f"Times must be between now and {PLAN_AHEAD.days} days ahead; forecasts don't reach further.")
    return t


def _aircraft(ctx: ToolContext, args: dict, *, required: bool = True) -> Aircraft | None:
    raw = args.get("tail")
    if raw is None and not required:
        return None
    club: Club = ctx.fleet.get()
    plane = club.get(str(raw or ""))
    if plane is None:
        tails = ", ".join(club.aircraft) or "none set up yet"
        raise ToolError(f"`{str(raw)[:12]}` isn't a club aircraft I have a profile for. Club aircraft: {tails}.")
    return plane


async def _resolve(ctx: ToolContext, items: list[str]) -> list[route_mod.Resolved]:
    return await route_mod.resolve(ctx.awc, items)


def _unclear(resolved: list[route_mod.Resolved]) -> dict | None:
    unclear = [r.as_dict() for r in resolved if r.status != "resolved"]
    if not unclear:
        return None
    return {"status": "needs the member's help", "unclear_points": unclear,
            "next": "Ask the member which place they meant for each of these, then call again."}


# ------------------------------------------------------------------ tools


async def resolve_route(ctx: ToolContext, args: dict) -> dict:
    items = _items(args, "points", most=MAX_ROUTE_POINTS)
    return {"points": [r.as_dict() for r in await _resolve(ctx, items)]}


async def flight_plan(ctx: ToolContext, args: dict) -> dict:
    items = _items(args, "route", most=MAX_ROUTE_POINTS)
    if len(items) < 2:
        raise ToolError("A route needs at least a departure and a destination.")
    departure = _time(ctx, args, "departure_local")
    cruise = _number(args, "cruise_altitude_ft", 500, 25000)
    plane = _aircraft(ctx, args, required=False)
    power = _number(args, "power_pct", 40, 100, required=False)
    stop_names = {s.upper() for s in _items(args, "stops", most=MAX_ROUTE_POINTS, required=False)}

    resolved = await _resolve(ctx, items)
    unclear = _unclear(resolved)
    if unclear:
        return unclear
    points = [r.point for r in resolved]
    stops = {i for i, (r, p) in enumerate(zip(resolved, points))
             if r.text.upper() in stop_names or p.ident.upper() in stop_names}

    if plane:
        tas, gph, note = plane.cruise_at(cruise, power)
        performance = flightwx.Performance(tas, gph, plane.taxi_gal, plane.climb_gal_per_1000ft)
    else:
        performance, note = flightwx.GENERIC, "no aircraft given: generic 110 kt, 10 gph"
    reserve = ctx.fleet.get().reserve_minutes
    rw = await flightwx.plan(
        ctx.awc, ctx.nws, points, departure=departure, cruise_alt_ft=cruise,
        performance=performance, stops=stops, reserve_minutes=reserve, now=ctx.now(),
    )
    out = summary.route(rw, ctx.tz, aircraft=plane, now=ctx.now())
    out["performance"]["basis"] = note
    out["reserve_minutes"] = reserve
    if plane:
        out["aircraft"] = f"{plane.tail}, {plane.type}"
    return out


async def airport_weather(ctx: ToolContext, args: dict) -> dict:
    items = _items(args, "airports", most=MAX_AIRPORTS)
    at = _time(ctx, args, "at_local")
    plane = _aircraft(ctx, args, required=False)
    resolved = await _resolve(ctx, items)
    unclear = _unclear(resolved)
    if unclear:
        return unclear
    points = [r.point for r in resolved if r.point.kind == "airport"]
    if not points:
        raise ToolError("None of those are airports.")
    outlooks, problems = await flightwx.airports_at(ctx.awc, ctx.nws, points, at=at, now=ctx.now())
    return summary.outlook_only(outlooks, problems, ctx.tz, aircraft=plane, now=ctx.now())


async def weight_and_balance(ctx: ToolContext, args: dict) -> dict:
    plane = _aircraft(ctx, args)
    loads: dict[str, float] = {}
    raw = args.get("loads")
    if not isinstance(raw, list) or len(raw) > 12:
        raise ToolError("`loads` must be a list of {station, lb}.")
    for item in raw:
        if not isinstance(item, dict):
            raise ToolError("Each load is {station, lb}.")
        station = _text(item, "station", limit=30)
        loads[station] = loads.get(station, 0) + _number(item, "lb", 0, 1500)
    fuel = _number(args, "fuel_gal", 0, 200)
    burn = _number(args, "burn_gal", 0, 200)
    try:
        load = wb.compute(plane.limits, loads, fuel, taxi_gal=plane.taxi_gal, burn_gal=burn)
    except wb.WbError as exc:
        raise ToolError(str(exc)) from None
    return {
        "aircraft": f"{plane.tail}, {plane.type}",
        "attention": load.problems or ["Within all weight limits and the CG envelope."],
        **{
            c.label: {"weight_lb": round(c.weight_lb), "cg_in": round(c.cg_in, 2), "fuel_gal": round(c.fuel_gal, 1),
                      "in_envelope": wb.in_envelope(plane.limits.envelope, c.cg_in, c.weight_lb)}
            for c in (load.ramp, load.takeoff, load.landing)
        },
        "limits": {"max_takeoff_lb": plane.limits.max_takeoff_lb, "max_landing_lb": plane.limits.max_landing_lb,
                   "usable_fuel_gal": plane.limits.usable_gal},
    }


async def takeoff_landing(ctx: ToolContext, args: dict) -> dict:
    plane = _aircraft(ctx, args)
    weight = _number(args, "weight_lb", 500, 20000)
    at = _time(ctx, args, "at_local")
    wanted_runway = _text(args, "runway", required=False, limit=4)
    grass = args.get("surface") == "grass"
    resolved = await _resolve(ctx, [_text(args, "airport")])
    unclear = _unclear(resolved)
    if unclear:
        return unclear
    point = resolved[0].point
    if point.kind != "airport":
        raise ToolError(f"{point.ident} isn't an airport.")
    (o,), problems = await flightwx.airports_at(ctx.awc, ctx.nws, [point], at=at, now=ctx.now())
    if o.elevation_ft is None:
        raise ToolError(f"I don't know {point.ident}'s elevation.")

    notes = []
    fresh = o.wind_source == "METAR"
    altimeter = o.metar.altimeter_inhg if o.metar and o.metar.altimeter_inhg else None
    if altimeter is None:
        altimeter = STANDARD_ALTIMETER
        notes.append("No altimeter setting; standard pressure assumed.")
    if fresh and o.metar.temp_c is not None:
        temp, temp_source = o.metar.temp_c, "METAR"
    elif o.hourly is not None and o.hourly.temp_c is not None:
        temp, temp_source = o.hourly.temp_c, "NWS hourly forecast"
    else:
        temp, temp_source = None, None
    pa = pressure_altitude(o.elevation_ft, altimeter)
    if temp is None:
        temp = perf.isa_temp_c(pa)
        temp_source = "standard temperature (no observation or forecast for this time)"
        notes.append("Temperature unknown; standard assumed, which may be optimistic.")

    choice = o.runway
    if wanted_runway:
        end = next((e for e in o.runway_ends if e.name == wanted_runway.upper()), None)
        if end is None:
            raise ToolError(f"No paved runway {wanted_runway} at {point.ident}. Ends: "
                            f"{', '.join(e.name for e in o.runway_ends) or 'none known'}.")
        choice = runways.for_end(end, *(o.wind or (None, None, None)))
    headwind = choice.headwind if choice else 0.0
    if choice is None:
        notes.append("No runway or wind known; calm wind assumed.")

    conditions = {"weight_lb": weight, "pressure_alt_ft": pa, "temp_c": temp, "headwind_kt": headwind,
                  "grass": 1 if grass else 0}
    attention: list[str] = []
    results = {}
    for name, table in plane.tables.items():
        try:
            r = perf.lookup(table, **conditions)
        except perf.PerfError as exc:
            results[name] = {"error": str(exc)}
            continue
        results[name] = {
            "conditions": table.conditions,
            "values": r.values,
            "units": r.units,
            "steps": [{"step": s.what, **s.values} for s in r.steps],
            **({"lower_bound": True} if r.lower_bound else {}),
            **({"warnings": r.warnings} if r.warnings else {}),
        }
        if r.lower_bound:
            attention.append(f"{name}: beyond the POH table, so the real figure is worse than shown")
    length = choice.end.length_ft if choice else None
    take = results.get("takeoff", {}).get("values", {})
    if length and take.get("over_50ft", 0) >= TAKEOFF_SHARE * length:
        attention.append(f"Takeoff over 50 ft needs {take['over_50ft']:.0f} ft of a {length} ft runway")
    land = results.get("landing", {}).get("values", {})
    if length and land.get("over_50ft", 0) >= TAKEOFF_SHARE * length:
        attention.append(f"Landing over 50 ft needs {land['over_50ft']:.0f} ft of a {length} ft runway")
    climb = results.get("climb", {}).get("values", {})
    if climb and min(climb.values()) < summary.CLIMB_MIN_FPM:
        attention.append(f"Climb rate {min(climb.values()):.0f} fpm")
    da = density_altitude(o.elevation_ft, altimeter, temp)
    if da >= summary.DA_HIGH_FT or da - o.elevation_ft >= summary.DA_ABOVE_FIELD_FT:
        attention.append(f"Density altitude {round(da, -2):,.0f} ft")
    if choice is not None:
        xwind = max(choice.crosswind, choice.gust_crosswind or 0)
        if xwind > (plane.max_demo_crosswind_kt or summary.DEFAULT_CROSSWIND_KT):
            attention.append(f"Crosswind {round(xwind)} kt")
        if choice.headwind < -0.5:
            attention.append(f"Tailwind {round(-choice.headwind)} kt")
    if weight > plane.limits.max_takeoff_lb:
        attention.append(f"{weight:.0f} lb is over the {plane.limits.max_takeoff_lb:.0f} lb max takeoff weight")

    out = {
        "aircraft": f"{plane.tail}, {plane.type}",
        "airport": point.ident,
        "time": summary.when(at, ctx.tz),
        "attention": attention or ["Nothing outside normal parameters found."],
        "inputs": {
            "weight_lb": weight, "pressure_altitude_ft": round(pa), "temp_c": round(temp, 1),
            "temp_source": temp_source, "altimeter_inhg": altimeter, "density_altitude_ft": round(da, -1),
            "runway": runways.describe(choice) if choice else None, "runway_length_ft": length,
            "headwind_kt": round(headwind, 1), "wind_source": o.wind_source or None, "surface": "grass" if grass else "paved",
        },
        "results": results,
    }
    if notes:
        out["notes"] = notes
    if problems:
        out["missing_data"] = problems
    return out



async def aircraft(ctx: ToolContext, args: dict) -> dict:
    plane = _aircraft(ctx, args)
    lim = plane.limits
    return {
        "tail": plane.tail, "type": plane.type, "year": plane.year, "fuel_type": plane.fuel_type,
        "equipment": list(plane.equipment), "notes": plane.notes,
        "weights_lb": {"empty": lim.empty_lb, "max_ramp": lim.max_ramp_lb, "max_takeoff": lim.max_takeoff_lb,
                       "max_landing": lim.max_landing_lb},
        "fuel": {"usable_gal": lim.usable_gal, "lb_per_gal": lim.lb_per_gal, "taxi_gal": plane.taxi_gal},
        "stations": [{"id": s.id, "name": s.name, **({"max_lb": s.max_lb} if s.max_lb else {})} for s in lim.stations],
        "speeds_kias": plane.speeds,
        "service_ceiling_ft": plane.service_ceiling_ft,
        "cruise": [{"pressure_alt_ft": r.pressure_alt_ft, "power_pct": r.power_pct, "tas_kt": r.tas_kt, "gph": r.gph}
                   for r in plane.cruise],
        "performance_tables": {n: t.conditions for n, t in plane.tables.items()},
        "manuals": [{"id": m.id, "title": m.title, "kind": m.kind,
                     "sections": {k: (f"{a}-{b}" if b != a else str(a)) for k, (a, b) in m.sections.items()}}
                    for m in plane.manuals],
    }


async def search_manual(ctx: ToolContext, args: dict) -> dict:
    plane = _aircraft(ctx, args)
    query = _text(args, "query", limit=200)
    try:
        return await asyncio.to_thread(manuals.search, plane, query)
    except manuals.ManualError as exc:
        raise ToolError(str(exc)) from None


async def read_manual_pages(ctx: ToolContext, args: dict) -> list[dict]:
    plane = _aircraft(ctx, args)
    manual = _text(args, "manual", limit=40)
    first = int(_number(args, "first_page", 1, 5000))
    last = int(_number(args, "last_page", 1, 5000))
    try:
        return await asyncio.to_thread(manuals.read_pages, plane, manual, first, last)
    except manuals.ManualError as exc:
        raise ToolError(str(exc)) from None


async def attach_card(ctx: ToolContext, args: dict) -> dict:
    kind = args.get("kind")
    if kind not in ("metar", "taf", "winds", "mountain"):
        raise ToolError("`kind` must be metar, taf, winds or mountain.")
    if len(ctx.attachments) >= MAX_CARDS_PER_TURN:
        raise ToolError(f"At most {MAX_CARDS_PER_TURN} cards per reply.")
    items = _items(args, "airports", most=4, required=kind != "mountain")
    tz, units = ctx.tz, ctx.units
    ids: list[str] = []
    if items:
        resolved = await _resolve(ctx, items)
        ids = [r.point.ident for r in resolved if r.point and r.point.kind == "airport"]
        if not ids and kind != "mountain":
            raise ToolError("None of those resolved to an airport.")

    if kind == "metar":
        found = await ctx.awc.metars(ids)
        metars = [found[i] for i in ids if i in found]
        if not metars:
            raise ToolError(f"No current METAR for {', '.join(ids)}.")
        try:
            ends = await ctx.awc.runways(m.station for m in metars)
        except SourceError:
            ends = {}
        image = await asyncio.to_thread(lambda: theme.stack([
            cards.metar_card(m, units=units, tz=tz, runways=ends.get(m.station)) for m in metars]))
        alt = " | ".join(cards.metar_alt(m, units, ends.get(m.station)) for m in metars)
    elif kind == "taf":
        found = await ctx.awc.tafs(ids)
        tafs = [found[i] for i in ids if i in found]
        if not tafs:
            raise ToolError(f"No TAF for {', '.join(ids)}.")
        image = await asyncio.to_thread(lambda: theme.stack([cards.taf_card(t, tz=tz) for t in tafs]))
        alt = " | ".join(cards.taf_alt(t) for t in tafs)
    elif kind == "winds":
        forecast = await ctx.awc.winds_aloft()
        stations = await ctx.awc.stations(ids[:1])
        point = stations[ids[0]].position if ids and ids[0] in stations else None
        site = hazards.nearest_fb_site(point, forecast.sites) if point else None
        if not site:
            raise ToolError("No winds-aloft site near there.")
        levels = forecast.site(site)
        use = f"{forecast.use_from}–{forecast.use_to}Z" if forecast.use_from else None
        image = await asyncio.to_thread(winds_render.winds_card, site, forecast.valid, use, levels, units=units)
        alt = winds_render.winds_alt(site, levels)
    else:
        region = regions.get(ctx.config.get("mountain", {}).get("region")) or next(iter(regions.all_regions().values()), None)
        if region is None:
            raise ToolError("No mountain region is set up.")
        board = await mountain.build(ctx.awc, ctx.nws, region, ctx.config, ctx.now())
        image = await asyncio.to_thread(mountain_render.render, board, tz=tz, units=units)
        alt = mountain_render.board_alt(board)
    ctx.attachments.append(Attachment(f"{kind}.png", image, alt))
    return {"attached": f"{kind} card", "note": "It will appear under your reply. Don't repeat its contents."}


HANDLERS = {
    "resolve_route": resolve_route,
    "flight_plan": flight_plan,
    "airport_weather": airport_weather,
    "weight_and_balance": weight_and_balance,
    "takeoff_landing": takeoff_landing,
    "aircraft": aircraft,
    "search_manual": search_manual,
    "read_manual_pages": read_manual_pages,
    "attach_card": attach_card,
}


async def run(ctx: ToolContext, name: str, args) -> tuple[str | list[dict], bool]:
    """(content for the tool_result, is_error). Never raises."""
    handler = HANDLERS.get(name)
    if handler is None:
        return f"There is no tool called {name}.", True
    if not isinstance(args, dict):
        return "Tool input must be an object.", True
    try:
        result = await handler(ctx, args)
    except ToolError as exc:
        return str(exc), True
    except (SourceError, navlog.NavLogError) as exc:
        return f"{exc} Say which data is missing rather than guessing.", True
    except Exception:  # noqa: BLE001 - a bug in one tool mustn't end the conversation
        log.exception("chat tool %s failed", name)
        return "That tool failed on my side. Carry on without it and say what's missing.", True
    if isinstance(result, list):
        return result, False
    return json.dumps(result, separators=(",", ":"), ensure_ascii=False, default=str), False
