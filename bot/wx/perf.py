"""Takeoff, landing and climb performance from POH tables.

Each table is a grid over whichever axes its POH uses. A 1964 Cessna Owner's
Manual gives takeoff distance by weight × headwind × altitude at standard
temperature, plus a percentage rule for heat. A modern POH gives pressure
altitude × temperature at max gross, with rules for wind. Both are data
here: a table declares its axes and lists its corrections, and this module
interpolates and applies them in the order given, showing each step.

**It never extrapolates.** High density altitude at max gross is exactly where
a table runs out (the 182G's stops at 7,500 ft; Granby is at 8,200). Past the
edge in the adverse direction (higher, heavier, hotter, less headwind) the
edge value is returned marked as a *lower bound*: the real distance is longer
by an unknown amount, and the pilot is told so. Past the edge in the
favourable direction, the edge value is merely conservative.
"""

from __future__ import annotations

from dataclasses import dataclass, field

AXES = ("weight_lb", "pressure_alt_ft", "temp_c", "headwind_kt")
# Which way along each axis performance gets worse.
ADVERSE_HIGH = {"weight_lb": True, "pressure_alt_ft": True, "temp_c": True, "headwind_kt": False}
C_PER_F = 5 / 9


class PerfError(ValueError):
    """A table can't be used as given. The message is user-facing."""


@dataclass(frozen=True, slots=True)
class Correction:
    kind: str  # temp_above_standard | tailwind | headwind | grass | percent
    percent: float
    per: float = 1.0  # per this many °F (temperature) or kt (wind)
    of: str | None = None  # a percentage of another output, e.g. "over_50ft"
    note: str = ""


@dataclass(frozen=True, slots=True)
class Table:
    name: str  # takeoff | landing | climb
    conditions: str
    axes: tuple[str, ...]
    breakpoints: dict[str, tuple[float, ...]]
    outputs: dict[str, object]  # output name → nested lists, axes order
    units: str
    corrections: tuple[Correction, ...] = ()


@dataclass
class Step:
    what: str
    values: dict[str, float]


@dataclass
class Result:
    table: str
    conditions: str
    inputs: dict[str, float]
    values: dict[str, float]
    units: str
    steps: list[Step] = field(default_factory=list)
    lower_bound: bool = False  # the real values are worse than these
    warnings: list[str] = field(default_factory=list)


def isa_temp_c(pressure_alt_ft: float) -> float:
    return 15 - 1.98 * pressure_alt_ft / 1000


def _locate(points: tuple[float, ...], x: float) -> tuple[int, float]:
    """Index of the cell's lower corner and the fraction across it."""
    if len(points) == 1:
        return 0, 0.0
    for i in range(len(points) - 1):
        if points[i] <= x <= points[i + 1]:
            return i, (x - points[i]) / (points[i + 1] - points[i])
    raise AssertionError("clamped value outside the grid")


def _interp(grid, axes: list[tuple[tuple[float, ...], float]]):
    if not axes:
        return float(grid)
    (points, x), rest = axes[0], axes[1:]
    i, f = _locate(points, x)
    lo = _interp(grid[i], rest)
    if f == 0:
        return lo
    return lo + f * (_interp(grid[i + 1], rest) - lo)


def lookup(table: Table, **conditions: float) -> Result:
    """Interpolate a table, then apply its corrections.

    `conditions` holds pressure_alt_ft, temp_c, weight_lb and headwind_kt
    (negative for a tailwind), plus `grass=1` for a turf runway.
    """
    missing = [a for a in table.axes if a not in conditions]
    if missing:
        raise PerfError(f"The {table.name} table needs {', '.join(missing)}.")
    result = Result(table.name, table.conditions, dict(conditions), {}, table.units)

    located = []
    for axis in table.axes:
        points = table.breakpoints[axis]
        x = conditions[axis]
        clamped = min(max(x, points[0]), points[-1])
        if clamped != x:
            beyond_high = x > points[-1]
            adverse = beyond_high == ADVERSE_HIGH[axis]
            edge = points[-1] if beyond_high else points[0]
            if axis == "headwind_kt" and x < 0 and any(c.kind == "tailwind" for c in table.corrections):
                pass  # the table starts at calm; the tailwind rule covers the rest
            elif adverse:
                result.lower_bound = True
                result.warnings.append(
                    f"{_label(axis)} {x:g} is beyond the table, which ends at {edge:g}. "
                    "The figures below are for the table's edge: the real ones are worse, by an unknown amount."
                )
            else:
                result.warnings.append(f"{_label(axis)} {x:g} is outside the table; used {edge:g}, which is conservative.")
        located.append((points, clamped))

    if conditions.get("grass") and not any(c.kind == "grass" for c in table.corrections):
        result.lower_bound = True
        result.warnings.append(
            "This table is for a hard surface and gives no correction for grass. "
            "On grass the real figures are longer, by an unknown amount."
        )

    values = {name: round(_interp(grid, located), 1) for name, grid in table.outputs.items()}
    result.steps.append(Step("table", dict(values)))

    pa = conditions.get("pressure_alt_ft", 0.0)
    for c in table.corrections:
        pct = 0.0
        if c.kind == "temp_above_standard" and "temp_c" not in table.axes and "temp_c" in conditions:
            above_f = (conditions["temp_c"] - isa_temp_c(pa)) / C_PER_F
            if above_f > 0:
                pct = c.percent * above_f / c.per
        elif c.kind == "tailwind" and conditions.get("headwind_kt", 0) < 0:
            pct = c.percent * -conditions["headwind_kt"] / c.per
        elif c.kind == "headwind" and "headwind_kt" not in table.axes and conditions.get("headwind_kt", 0) > 0:
            pct = -c.percent * conditions["headwind_kt"] / c.per
        elif c.kind == "grass" and conditions.get("grass"):
            pct = c.percent
        elif c.kind == "percent":
            pct = c.percent
        if not pct:
            continue
        base = values[c.of] if c.of else None
        values = {
            k: round(v + (base if base is not None else v) * pct / 100, 1) for k, v in values.items()
        }
        result.steps.append(Step(f"{c.kind} {pct:+.0f}%" + (f" of {c.of}" if c.of else "") + (f" ({c.note})" if c.note else ""), dict(values)))

    result.values = values
    return result


def _label(axis: str) -> str:
    return {"weight_lb": "Weight (lb)", "pressure_alt_ft": "Pressure altitude (ft)",
            "temp_c": "Temperature (°C)", "headwind_kt": "Headwind (kt)"}[axis]


def check_shape(table: Table) -> None:
    """Raise if a table's grids don't match its breakpoints. Run at load time,
    so a typo in a profile fails then, not mid-conversation."""
    for axis in table.axes:
        if axis not in AXES:
            raise PerfError(f"{table.name}: unknown axis {axis!r}")
        points = table.breakpoints.get(axis)
        if not points or list(points) != sorted(points) or len(set(points)) != len(points):
            raise PerfError(f"{table.name}: breakpoints for {axis} must be strictly increasing")

    def walk(grid, depth: int, name: str) -> None:
        if depth == len(table.axes):
            if not isinstance(grid, (int, float)):
                raise PerfError(f"{table.name}.{name}: expected a number at the innermost level")
            return
        size = len(table.breakpoints[table.axes[depth]])
        if not isinstance(grid, list) or len(grid) != size:
            raise PerfError(
                f"{table.name}.{name}: expected {size} entries along {table.axes[depth]}"
            )
        for g in grid:
            walk(g, depth + 1, name)

    for name, grid in table.outputs.items():
        walk(grid, 0, name)
    for c in table.corrections:
        if c.of and c.of not in table.outputs:
            raise PerfError(f"{table.name}: correction refers to unknown output {c.of!r}")
