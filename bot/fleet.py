"""The club's aircraft: profiles and manuals, from files on the Pi.

Layout, under CLUB_DIR (default /var/lib/avwx-bot/club):

    club.toml                       club name, reserve policy, rules and SOPs
    aircraft/<TAIL>/aircraft.toml   one profile per aircraft
    aircraft/<TAIL>/manuals/*.pdf   the POH, supplements, pilot's guides

It lives outside the checkout because deployment resets the checkout, and
because a club's manuals and weighing records are the club's business, not
the repository's.

Every number in a profile is copied from the aircraft's own documents and
checked by a person. So loading **never fills in a missing value**: a profile
without its empty weight, envelope or tables is rejected with a message naming
the field, and the plane is left out until it's fixed. A wrong default would
be worse than no answer.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .wx import perf, wb

log = logging.getLogger(__name__)

MANUAL_KINDS = {"poh", "afms", "guide", "checklist", "wb", "other"}


class ProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Manual:
    id: str
    title: str
    kind: str
    path: Path
    sections: dict[str, tuple[int, int]]  # section → (first page, last page), 1-based


@dataclass(frozen=True, slots=True)
class CruiseRow:
    pressure_alt_ft: float
    power_pct: float
    tas_kt: float
    gph: float
    rpm: int | None = None
    map_in: float | None = None


@dataclass(frozen=True)
class Aircraft:
    tail: str
    type: str
    year: int | None
    fuel_type: str
    equipment: tuple[str, ...]
    notes: str
    limits: wb.Limits
    taxi_gal: float
    climb_gal_per_1000ft: float
    speeds: dict[str, float]
    max_demo_crosswind_kt: float | None
    service_ceiling_ft: float | None
    cruise: tuple[CruiseRow, ...]
    default_power_pct: float
    tables: dict[str, perf.Table]
    manuals: tuple[Manual, ...]
    directory: Path

    def manual(self, manual_id: str) -> Manual | None:
        return next((m for m in self.manuals if m.id == manual_id), None)

    def cruise_at(self, pressure_alt_ft: float, power_pct: float | None = None) -> tuple[float, float, str]:
        """(TAS, gph, note) at an altitude, interpolated between the profile's
        rows for one power setting. Clamped to the rows' altitude range, with
        a note saying so."""
        power = power_pct if power_pct is not None else self.default_power_pct
        powers = sorted({r.power_pct for r in self.cruise})
        chosen = min(powers, key=lambda p: abs(p - power))
        rows = sorted((r for r in self.cruise if r.power_pct == chosen), key=lambda r: r.pressure_alt_ft)
        note = f"{chosen:g}% power" + ("" if chosen == power else f" (nearest to the {power:g}% asked for)")
        if pressure_alt_ft <= rows[0].pressure_alt_ft or len(rows) == 1:
            r = rows[0]
            if pressure_alt_ft < r.pressure_alt_ft:
                note += f"; lowest row is {r.pressure_alt_ft:g} ft"
            return r.tas_kt, r.gph, note
        if pressure_alt_ft >= rows[-1].pressure_alt_ft:
            r = rows[-1]
            if pressure_alt_ft > r.pressure_alt_ft:
                note += f"; highest row is {r.pressure_alt_ft:g} ft, so TAS may be lower than shown"
            return r.tas_kt, r.gph, note
        for lo, hi in zip(rows, rows[1:]):
            if lo.pressure_alt_ft <= pressure_alt_ft <= hi.pressure_alt_ft:
                f = (pressure_alt_ft - lo.pressure_alt_ft) / (hi.pressure_alt_ft - lo.pressure_alt_ft)
                return (
                    round(lo.tas_kt + f * (hi.tas_kt - lo.tas_kt), 1),
                    round(lo.gph + f * (hi.gph - lo.gph), 1),
                    note,
                )
        raise AssertionError("unreachable")


@dataclass
class Club:
    name: str
    rules: str
    reserve_minutes: float
    aircraft: dict[str, Aircraft]
    problems: list[str] = field(default_factory=list)  # profiles that didn't load, and why

    def get(self, tail: str) -> Aircraft | None:
        return self.aircraft.get(normalize_tail(tail))


def normalize_tail(tail: str) -> str:
    t = tail.strip().upper().replace("-", "")
    return t if t.startswith("N") or not t[:1].isdigit() else "N" + t


def _req(d: dict, key: str, where: str):
    if key not in d:
        raise ProfileError(f"{where}: missing `{key}`")
    return d[key]


def _num(d: dict, key: str, where: str) -> float:
    v = _req(d, key, where)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ProfileError(f"{where}: `{key}` must be a number")
    return float(v)


def _opt_num(d: dict, key: str, where: str) -> float | None:
    return _num(d, key, where) if key in d else None


def _pages(text: str, where: str) -> tuple[int, int]:
    try:
        first, _, last = str(text).partition("-")
        a, b = int(first), int(last or first)
    except ValueError:
        raise ProfileError(f"{where}: pages must look like \"12\" or \"12-18\"") from None
    if a < 1 or b < a:
        raise ProfileError(f"{where}: bad page range {text!r}")
    return a, b


def load_aircraft(path: Path) -> Aircraft:
    where = str(path)
    try:
        d = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{where}: {exc}") from None
    tail = normalize_tail(str(_req(d, "tail", where)))
    if tail != normalize_tail(path.parent.name):
        raise ProfileError(f"{where}: tail {tail} doesn't match its directory {path.parent.name}")

    w = _req(d, "weights", where)
    f = _req(d, "fuel", where)
    stations = tuple(
        wb.Station(_req(s, "id", f"{where} station"), _req(s, "name", f"{where} station"),
                   _num(s, "arm", f"{where} station"), _opt_num(s, "max_lb", f"{where} station"))
        for s in _req(d, "stations", where)
    )
    env = _req(d, "envelope", where)
    points = tuple((float(p[0]), float(p[1])) for p in _req(env, "points", f"{where} envelope"))
    if len(points) < 3:
        raise ProfileError(f"{where}: the envelope needs at least three points")
    limits = wb.Limits(
        empty_lb=_num(w, "empty_lb", f"{where} weights"),
        empty_arm=_num(w, "empty_arm", f"{where} weights"),
        max_ramp_lb=_num(w, "max_ramp_lb", f"{where} weights"),
        max_takeoff_lb=_num(w, "max_takeoff_lb", f"{where} weights"),
        max_landing_lb=_num(w, "max_landing_lb", f"{where} weights"),
        fuel_arm=_num(f, "arm", f"{where} fuel"),
        usable_gal=_num(f, "usable_gal", f"{where} fuel"),
        lb_per_gal=_num(f, "lb_per_gal", f"{where} fuel"),
        envelope=points,
        stations=stations,
    )

    cruise = tuple(
        CruiseRow(
            _num(r, "pressure_alt_ft", f"{where} cruise"), _num(r, "power_pct", f"{where} cruise"),
            _num(r, "tas_kt", f"{where} cruise"), _num(r, "gph", f"{where} cruise"),
            int(r["rpm"]) if "rpm" in r else None, _opt_num(r, "map_in", f"{where} cruise"),
        )
        for r in _req(d, "cruise", where)
    )
    if not cruise:
        raise ProfileError(f"{where}: needs at least one cruise row")

    tables = {}
    for t in d.get("performance", []):
        name = _req(t, "name", f"{where} performance")
        tw = f"{where} performance.{name}"
        axes = tuple(_req(t, "axes", tw))
        table = perf.Table(
            name=name,
            conditions=t.get("conditions", ""),
            axes=axes,
            breakpoints={a: tuple(float(x) for x in _req(_req(t, "breakpoints", tw), a, tw)) for a in axes},
            outputs=dict(_req(t, "outputs", tw)),
            units=_req(t, "units", tw),
            corrections=tuple(
                perf.Correction(_req(c, "kind", tw), _num(c, "percent", tw), float(c.get("per", 1)),
                                c.get("of"), c.get("note", ""))
                for c in t.get("corrections", [])
            ),
        )
        try:
            perf.check_shape(table)
        except perf.PerfError as exc:
            raise ProfileError(f"{where}: {exc}") from None
        tables[name] = table

    manuals = []
    for m in d.get("manuals", []):
        mw = f"{where} manual"
        mid = _req(m, "id", mw)
        kind = _req(m, "kind", mw)
        if kind not in MANUAL_KINDS:
            raise ProfileError(f"{mw} {mid}: kind must be one of {', '.join(sorted(MANUAL_KINDS))}")
        file = path.parent / "manuals" / _req(m, "file", mw)
        if file.parent != path.parent / "manuals" or not file.is_file():
            raise ProfileError(f"{mw} {mid}: {file} not found")
        sections = {k: _pages(v, f"{mw} {mid} {k}") for k, v in (m.get("sections") or {}).items()}
        manuals.append(Manual(mid, _req(m, "title", mw), kind, file, sections))

    speeds = {k: float(v) for k, v in (d.get("speeds") or {}).items()}
    return Aircraft(
        tail=tail,
        type=_req(d, "type", where),
        year=d.get("year"),
        fuel_type=d.get("fuel_type", ""),
        equipment=tuple(d.get("equipment", [])),
        notes=d.get("notes", "").strip(),
        limits=limits,
        taxi_gal=_num(f, "taxi_gal", f"{where} fuel"),
        climb_gal_per_1000ft=_num(f, "climb_gal_per_1000ft", f"{where} fuel"),
        speeds=speeds,
        max_demo_crosswind_kt=speeds.get("max_demo_crosswind_kt"),
        service_ceiling_ft=_opt_num(d, "service_ceiling_ft", where),
        cruise=cruise,
        default_power_pct=float(d.get("default_power_pct", cruise[0].power_pct)),
        tables=tables,
        manuals=tuple(manuals),
        directory=path.parent,
    )


def load(club_dir: Path) -> Club:
    club_file = club_dir / "club.toml"
    meta: dict = {}
    problems: list[str] = []
    if club_file.is_file():
        try:
            meta = tomllib.loads(club_file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            problems.append(f"{club_file}: {exc}")
    aircraft = {}
    for path in sorted((club_dir / "aircraft").glob("*/aircraft.toml")):
        try:
            a = load_aircraft(path)
        except ProfileError as exc:
            log.error("aircraft profile rejected: %s", exc)
            problems.append(str(exc))
            continue
        aircraft[a.tail] = a
    return Club(
        name=str(meta.get("name", "")),
        rules=str(meta.get("rules", "")).strip(),
        reserve_minutes=float(meta.get("reserve_minutes", 30)),
        aircraft=aircraft,
        problems=problems,
    )


class FleetCache:
    """The club, reloaded whenever a file under it changes. Adding a plane or
    fixing a profile then needs no restart."""

    def __init__(self, club_dir: Path) -> None:
        self.dir = club_dir
        self._stamp: tuple | None = None
        self._club: Club | None = None

    def _fingerprint(self) -> tuple:
        if not self.dir.is_dir():
            return ()
        return tuple(sorted(
            (str(p), p.stat().st_mtime_ns) for p in self.dir.rglob("*.toml")
        ))

    def get(self) -> Club:
        stamp = self._fingerprint()
        if self._club is None or stamp != self._stamp:
            self._club = load(self.dir)
            self._stamp = stamp
        return self._club
