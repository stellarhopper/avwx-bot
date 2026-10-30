"""Nav log: courses, wind correction, ground speed, time and fuel per leg.

Pure arithmetic, so the numbers a member is shown come from tested code and
never from a language model doing sums. A route is split into **flights** at
each landing: whether each hop can be flown on the fuel aboard is the question
that matters, and a total for the whole day doesn't answer it.

Simplifications, stated so they can be judged:
- Climb and descent are flown at cruise TAS. The time is a little short and
  the fuel allowance for the climb covers the difference, as most flight
  planning rules of thumb do.
- Magnetic variation for a leg is the variation at its start point if known,
  else at the nearest point that has one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import geo


class NavLogError(ValueError):
    """The flight can't be planned as given. The message is user-facing."""


@dataclass(frozen=True, slots=True)
class Waypoint:
    ident: str
    lat: float
    lon: float
    elevation_ft: float | None = None
    magvar: float | None = None  # east positive

    @property
    def position(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass(frozen=True, slots=True)
class Leg:
    origin: str
    dest: str
    distance_nm: float
    true_course: float
    magnetic_course: float
    wind_dir: float | None
    wind_kt: float | None
    wind_correction: float  # degrees; positive is a right correction
    magnetic_heading: float
    groundspeed: float
    minutes: float
    fuel_gal: float


@dataclass(frozen=True, slots=True)
class Flight:
    """One takeoff to one landing."""

    origin: str
    dest: str
    departs: datetime
    arrives: datetime
    legs: tuple[Leg, ...]
    distance_nm: float
    minutes: float
    fuel_taxi_climb_gal: float
    fuel_enroute_gal: float
    reserve_gal: float

    @property
    def fuel_required_gal(self) -> float:
        return self.fuel_taxi_climb_gal + self.fuel_enroute_gal + self.reserve_gal

    @property
    def fuel_burned_gal(self) -> float:
        return self.fuel_taxi_climb_gal + self.fuel_enroute_gal


@dataclass
class NavLog:
    tas_kt: float
    burn_gph: float
    cruise_alt_ft: float
    reserve_minutes: float
    flights: list[Flight]
    etas: list[datetime]  # at each waypoint, in route order
    notes: list[str] = field(default_factory=list)

    @property
    def distance_nm(self) -> float:
        return sum(f.distance_nm for f in self.flights)

    @property
    def minutes(self) -> float:
        return sum(f.minutes for f in self.flights)


def wind_triangle(true_course: float, tas: float, wind_from: float, wind_kt: float) -> tuple[float, float]:
    """(wind correction angle, ground speed). Raises if the wind is stronger
    across the course than the aircraft can fly."""
    if wind_kt <= 0:
        return 0.0, tas
    angle = math.radians(wind_from - true_course)
    cross = wind_kt * math.sin(angle)
    if abs(cross) >= tas:
        raise NavLogError(
            f"A {wind_kt:.0f} kt wind from {wind_from:03.0f}° is more crosswind than a "
            f"{tas:.0f} kt TAS can correct for on a {true_course:03.0f}° course."
        )
    wca = math.asin(cross / tas)
    groundspeed = tas * math.cos(wca) - wind_kt * math.cos(angle)
    if groundspeed <= 0:
        raise NavLogError(f"A {wind_kt:.0f} kt headwind leaves no ground speed at {tas:.0f} kt TAS.")
    return math.degrees(wca), groundspeed


def _norm(deg: float) -> float:
    d = deg % 360
    return 360.0 if round(d) == 0 else d


def _variation(points: list[Waypoint], i: int) -> float:
    if points[i].magvar is not None:
        return points[i].magvar
    known = [(abs(j - i), p.magvar) for j, p in enumerate(points) if p.magvar is not None]
    return min(known)[1] if known else 0.0


def build(
    points: list[Waypoint],
    *,
    departure: datetime,
    tas_kt: float,
    burn_gph: float,
    cruise_alt_ft: float,
    winds: list[tuple[float, float] | None],
    stops: set[int] | None = None,
    taxi_gal: float = 0.0,
    climb_gal_per_1000ft: float = 0.0,
    reserve_minutes: float = 30,
    ground_minutes: float = 30,
) -> NavLog:
    """`winds[i]` is (from, kt) for the leg from points[i] to points[i+1].
    `stops` are indices of intermediate points where the aircraft lands; the
    first and last point always are."""
    if len(points) < 2:
        raise NavLogError("A route needs at least two points.")
    if len(winds) != len(points) - 1:
        raise ValueError("one wind per leg")
    if tas_kt <= 0 or burn_gph <= 0:
        raise NavLogError("TAS and fuel burn must be positive.")
    landings = {0, len(points) - 1} | {i for i in (stops or set()) if 0 < i < len(points) - 1}

    flights: list[Flight] = []
    etas: list[datetime] = [departure]
    notes: list[str] = []
    clock = departure
    legs: list[Leg] = []
    start = 0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        distance = geo.distance_nm(a.position, b.position)
        tc = geo.bearing_deg(a.position, b.position)
        var = _variation(points, i)
        wind = winds[i]
        wca, gs = wind_triangle(tc, tas_kt, wind[0], wind[1]) if wind else (0.0, tas_kt)
        minutes = distance / gs * 60
        legs.append(Leg(
            origin=a.ident, dest=b.ident, distance_nm=round(distance, 1),
            true_course=round(_norm(tc)), magnetic_course=round(_norm(tc - var)),
            wind_dir=wind[0] if wind else None, wind_kt=wind[1] if wind else None,
            wind_correction=round(wca, 1), magnetic_heading=round(_norm(tc + wca - var)),
            groundspeed=round(gs), minutes=round(minutes, 1),
            fuel_gal=round(minutes / 60 * burn_gph, 1),
        ))
        clock += timedelta(minutes=minutes)
        etas.append(clock)
        if i + 1 in landings:
            origin = points[start]
            climb_ft = max(0.0, cruise_alt_ft - (origin.elevation_ft or 0))
            if origin.elevation_ft is None and climb_gal_per_1000ft:
                notes.append(f"{origin.ident}: field elevation unknown; climb fuel assumes a sea-level start.")
            flight_minutes = sum(leg.minutes for leg in legs)
            flights.append(Flight(
                origin=origin.ident,
                dest=b.ident,
                departs=etas[start] if start == 0 else etas[start] + timedelta(minutes=ground_minutes),
                arrives=clock,
                legs=tuple(legs),
                distance_nm=round(sum(leg.distance_nm for leg in legs), 1),
                minutes=round(flight_minutes, 1),
                fuel_taxi_climb_gal=round(taxi_gal + climb_gal_per_1000ft * climb_ft / 1000, 1),
                fuel_enroute_gal=round(flight_minutes / 60 * burn_gph, 1),
                reserve_gal=round(reserve_minutes / 60 * burn_gph, 1),
            ))
            legs = []
            start = i + 1
            if i + 1 != len(points) - 1:
                clock += timedelta(minutes=ground_minutes)
    if any(p.magvar is None for p in points) and all(p.magvar is None for p in points):
        notes.append("No magnetic variation known for any point; magnetic courses equal true.")
    return NavLog(tas_kt, burn_gph, cruise_alt_ft, reserve_minutes, flights, etas, notes)
