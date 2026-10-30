"""Weight and balance at takeoff and at landing.

Landing matters as much as takeoff: fuel burns off, and in most singles the CG
moves forward with it, sometimes out of the envelope on a heavy front-seat
load. Both are checked against every weight limit and the CG envelope from
the aircraft's profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class WbError(ValueError):
    """The loading can't be computed as given. The message is user-facing."""


@dataclass(frozen=True, slots=True)
class Station:
    id: str
    name: str
    arm: float
    max_lb: float | None = None


@dataclass(frozen=True, slots=True)
class Limits:
    empty_lb: float
    empty_arm: float
    max_ramp_lb: float
    max_takeoff_lb: float
    max_landing_lb: float
    fuel_arm: float
    usable_gal: float
    lb_per_gal: float
    envelope: tuple[tuple[float, float], ...]  # (cg_in, weight_lb), in order around the outline
    stations: tuple[Station, ...]


@dataclass(frozen=True, slots=True)
class Condition:
    label: str  # ramp | takeoff | landing
    weight_lb: float
    moment: float
    fuel_gal: float

    @property
    def cg_in(self) -> float:
        return self.moment / self.weight_lb


@dataclass
class Loading:
    ramp: Condition
    takeoff: Condition
    landing: Condition
    problems: list[str] = field(default_factory=list)  # each a limit exceeded

    @property
    def ok(self) -> bool:
        return not self.problems


def _on_segment(p, a, b, tolerance=0.01) -> bool:
    """Within `tolerance` of segment a–b, in the envelope's own units."""
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length_sq))
    return ((x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2) ** 0.5 <= tolerance


def in_envelope(envelope, cg_in: float, weight_lb: float) -> bool:
    """Inside the outline, or on it: exactly max gross at the aft limit is legal."""
    p = (cg_in, weight_lb)
    pts = list(envelope)
    if any(_on_segment(p, a, b) for a, b in zip(pts, pts[1:] + pts[:1])):
        return True
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > weight_lb) != (y2 > weight_lb):
            x_cross = x1 + (weight_lb - y1) * (x2 - x1) / (y2 - y1)
            if cg_in < x_cross:
                inside = not inside
    return inside


def compute(limits: Limits, loads: dict[str, float], fuel_gal: float, *, taxi_gal: float, burn_gal: float) -> Loading:
    """`loads` maps station id → pounds. `fuel_gal` is fuel at engine start."""
    known = {s.id: s for s in limits.stations}
    unknown = sorted(set(loads) - set(known))
    if unknown:
        raise WbError(f"Unknown loading station(s): {', '.join(unknown)}. This aircraft has: {', '.join(known)}.")
    if fuel_gal < 0 or any(v < 0 for v in loads.values()):
        raise WbError("Weights and fuel can't be negative.")

    problems = []
    if fuel_gal > limits.usable_gal:
        problems.append(f"Fuel {fuel_gal:g} gal is more than the {limits.usable_gal:g} gal usable.")
    for sid, lb in loads.items():
        s = known[sid]
        if s.max_lb is not None and lb > s.max_lb:
            problems.append(f"{s.name}: {lb:g} lb is over its {s.max_lb:g} lb limit.")

    payload_w = sum(loads.values())
    payload_m = sum(lb * known[sid].arm for sid, lb in loads.items())
    base_w = limits.empty_lb + payload_w
    base_m = limits.empty_lb * limits.empty_arm + payload_m

    def at(label: str, gal: float) -> Condition:
        gal = max(gal, 0.0)
        fuel_lb = gal * limits.lb_per_gal
        return Condition(label, base_w + fuel_lb, base_m + fuel_lb * limits.fuel_arm, gal)

    ramp = at("ramp", fuel_gal)
    takeoff = at("takeoff", fuel_gal - taxi_gal)
    landing = at("landing", fuel_gal - taxi_gal - burn_gal)
    if fuel_gal - taxi_gal - burn_gal < 0:
        problems.append(f"The flight burns {taxi_gal + burn_gal:.1f} gal but only {fuel_gal:g} gal is aboard.")

    for cond, limit in ((ramp, limits.max_ramp_lb), (takeoff, limits.max_takeoff_lb), (landing, limits.max_landing_lb)):
        if cond.weight_lb > limit + 1e-6:
            problems.append(f"{cond.label.capitalize()} weight {cond.weight_lb:.0f} lb is over the {limit:.0f} lb limit by {cond.weight_lb - limit:.0f} lb.")
    for cond in (takeoff, landing):
        if not in_envelope(limits.envelope, cond.cg_in, cond.weight_lb):
            problems.append(f"{cond.label.capitalize()} CG {cond.cg_in:.1f} in at {cond.weight_lb:.0f} lb is outside the envelope.")
    return Loading(ramp, takeoff, landing, problems)
