"""Turning a route as a member says it into points on the map.

"KBJC VPROL KGNB" is easy: every item is an identifier. "KBJC, Rollins pass,
Granby, Kremmling, SBS" is how people actually talk, and it has to work too.
Each item is scored against everything it could mean:

- an airport identifier, with the usual K for three-letter US ones (SBS → KSBS)
- an FAA fix or navaid identifier (VPROL, BJC VOR)
- coordinates (39.93N 105.68W, or 39.93,-105.68)
- a mountain pass from data/regions/, by name or alias
- a public-use airport by name or town, from data/airports_us.json

Name matches are then ranked by distance to the items around them that did
resolve, so "Granby" between Rollins Pass and Kremmling is Granby, CO, not
Granby, MO. An item with no clear winner comes back **ambiguous** with its top
candidates, and one with nothing at all comes back **unresolved**. Claude then
asks the member, rather than this module guessing. It never invents
coordinates: every point here comes from a database.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from functools import cache

from . import regions
from .config import ROOT
from .sources.awc import Awc
from .sources.http import SourceError
from .wx import geo

GAZETTEER = ROOT / "data" / "airports_us.json"

# Scores, out of 100. An identifier typed exactly beats any name match.
EXACT = 100
FIX_TYPED_AS_IDENT = 90  # VPROL, in capitals
FIX_TYPED_AS_WORD = 50  # "eagle": more likely a town than the fix EAGLE
PASS_NAME = 90  # "Rollins Pass"
PASS_WORD = 80  # "Rollins" alone
NAME_EXACT = 70  # the town or the airport's name, in full
NAME_WORDS = 65  # every word typed appears in the name or town
NAME_FUZZY = 55  # a near miss, e.g. a typo
SIZE_BONUS = {"L": 6, "M": 4, "S": 0, "W": -10}  # seaplane bases are rarely meant

# Resolved needs at least this score and this lead over the runner-up.
MIN_SCORE = 60
MIN_LEAD = 12
# Distance penalty: a point per this many nm from the nearest neighbour that
# resolved, capped, so a far-off namesake loses but a long leg is still allowed.
PENALTY_NM_PER_POINT = 20
MAX_PENALTY = 45

MAX_CANDIDATES = 3

_IDENT = re.compile(r"^[A-Z0-9]{2,5}$")
_NAVAID_SUFFIX = re.compile(r"^([A-Z0-9]{2,3})\s+(VOR|VORTAC|VOR/DME|VOR-DME|NDB|TACAN)$")
_HEMI = re.compile(
    r"^(\d{1,2}(?:\.\d+)?)\s*°?\s*([NS])[\s,/]*(\d{1,3}(?:\.\d+)?)\s*°?\s*([EW])$"
)
_SIGNED = re.compile(r"^(-?\d{1,2}\.\d+)\s*[,/\s]\s*(-?\d{1,3}\.\d+)$")
_WORD = re.compile(r"[a-z0-9]+")
# Words that say what kind of place it is, not which one.
_FILLER = {"airport", "airfield", "apt", "the", "field", "pass"}


@dataclass(frozen=True, slots=True)
class Point:
    ident: str  # KGNB, VPROL, Rollins Pass, 39.93N/105.68W
    name: str
    kind: str  # airport | fix | navaid | pass | coordinates
    lat: float
    lon: float
    elevation_ft: float | None = None
    note: str = ""  # "VFR waypoint VPROL", "Granby, CO"

    @property
    def position(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    def label(self) -> str:
        if self.kind == "pass" and self.note:
            return f"{self.ident} ({self.note.removeprefix('VFR waypoint ')})"
        return self.ident

    def as_dict(self) -> dict:
        out = {"ident": self.ident, "name": self.name, "kind": self.kind,
               "lat": round(self.lat, 4), "lon": round(self.lon, 4)}
        if self.elevation_ft is not None:
            out["elevation_ft"] = round(self.elevation_ft)
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True, slots=True)
class Candidate:
    point: Point
    score: float
    why: str


@dataclass(frozen=True, slots=True)
class Resolved:
    text: str
    status: str  # resolved | ambiguous | unresolved
    point: Point | None
    candidates: tuple[Candidate, ...]
    why: str

    def as_dict(self) -> dict:
        out: dict = {"input": self.text, "status": self.status, "why": self.why}
        if self.point:
            out["point"] = self.point.as_dict()
        if self.status == "ambiguous":
            out["candidates"] = [
                {**c.point.as_dict(), "matched": c.why} for c in self.candidates
            ]
        return out


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


@cache
def _gazetteer() -> tuple[tuple, ...]:
    return tuple(tuple(r) for r in json.loads(GAZETTEER.read_text(encoding="utf-8")))


@cache
def _by_ident() -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for row in _gazetteer():
        out.setdefault(row[0], row)
        out.setdefault(row[1], row)
    return out


def _airport_point(row: tuple) -> Point:
    ident, _local, name, city, state, lat, lon, elev, _size, _kw = row
    return Point(ident, name, "airport", lat, lon, elev, f"{city}, {state}" if city else state)


def airport_by_ident(text: str) -> Point | None:
    """A public-use airport by identifier: KGNB, GNB or 20V."""
    token = text.strip().upper()
    index = _by_ident()
    row = index.get(token)
    if row is None and len(token) == 3 and token.isalpha():
        row = index.get("K" + token)
    return _airport_point(row) if row else None


def _all_passes() -> list[dict]:
    return [p for region in regions.all_regions().values() for p in region.get("passes", [])]


def _pass_point(p: dict) -> Point:
    name = re.sub(r"\s*\(.*?\)", "", p["name"]).strip()
    note = f"VFR waypoint {p['vfr_waypoint']}" if p.get("vfr_waypoint") else ""
    return Point(name, p["name"], "pass", p["lat"], p["lon"], p.get("elevation_ft"), note)


def _pass_candidates(text: str) -> list[Candidate]:
    typed = [w for w in _words(text) if w not in _FILLER]
    if not typed:
        return []
    said_pass = "pass" in _words(text)
    out = []
    for p in _all_passes():
        for name in [p["name"], *p.get("aliases", [])]:
            words = [w for w in _words(re.sub(r"\(.*?\)", "", name)) if w not in _FILLER]
            if typed == words:
                score = PASS_NAME if said_pass else PASS_WORD
                out.append(Candidate(_pass_point(p), score, f"pass name '{name}'"))
                break
            if set(typed) <= set(words):
                out.append(Candidate(_pass_point(p), PASS_WORD - 10, f"part of pass name '{name}'"))
                break
    return out


def _name_candidates(text: str) -> list[Candidate]:
    typed = [w for w in _words(text) if w not in _FILLER]
    if not typed:
        return []
    phrase = " ".join(typed)
    out = []
    for row in _gazetteer():
        _ident, _local, name, city, _state, *_rest, size, keywords = row
        name_words = [w for w in _words(name) if w not in _FILLER]
        city_words = _words(city)
        everything = set(name_words) | set(city_words) | set(_words(keywords))
        if " ".join(city_words) == phrase or " ".join(name_words) == phrase:
            score, why = NAME_EXACT, f"town or name '{city if ' '.join(city_words) == phrase else name}'"
        elif phrase in " ".join(name_words) or phrase in " ".join(city_words):
            score, why = NAME_WORDS, f"'{name}', {city}"
        elif set(typed) <= everything:
            score, why = NAME_WORDS, f"'{name}', {city}"
        else:
            best = max(
                difflib.SequenceMatcher(None, phrase, " ".join(city_words)).ratio(),
                difflib.SequenceMatcher(None, phrase, " ".join(name_words)).ratio(),
            )
            if best < 0.85:
                continue
            score, why = NAME_FUZZY, f"close to '{name}', {city}"
        out.append(Candidate(_airport_point(row), score + SIZE_BONUS.get(size, 0), why))
    out.sort(key=lambda c: -c.score)
    return out[:25]


def _coordinates(text: str) -> Point | None:
    t = text.strip().upper()
    m = _HEMI.match(t)
    if m:
        lat = float(m.group(1)) * (-1 if m.group(2) == "S" else 1)
        lon = float(m.group(3)) * (-1 if m.group(4) == "W" else 1)
    else:
        m = _SIGNED.match(t)
        if not m:
            return None
        lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    ident = f"{abs(lat):.2f}{'N' if lat >= 0 else 'S'}/{abs(lon):.2f}{'E' if lon >= 0 else 'W'}"
    return Point(ident, "coordinates", "coordinates", lat, lon)


async def resolve(awc: Awc, items: list[str]) -> list[Resolved]:
    """Resolve each route item. Order is kept; nothing is dropped."""
    texts = [" ".join(str(i).split()) for i in items]
    found: list[list[Candidate]] = [[] for _ in texts]
    problems: list[str | None] = [None] * len(texts)

    # Identifiers and coordinates first: they need no context.
    station_ids, fix_ids, navaid_ids = set(), set(), set()
    for i, text in enumerate(texts):
        upper = text.upper()
        point = _coordinates(text)
        if point:
            found[i].append(Candidate(point, EXACT, "coordinates"))
            continue
        navaid = _NAVAID_SUFFIX.match(upper)
        if navaid:
            navaid_ids.add(navaid.group(1))
            continue
        if not _IDENT.match(upper):
            continue
        point = airport_by_ident(upper)
        if point:
            found[i].append(Candidate(point, EXACT, "airport identifier"))
            continue
        station_ids.add(upper)
        if len(upper) == 3 and upper.isalpha():
            station_ids.add("K" + upper)
        if len(upper) == 5 and upper.isalpha():
            fix_ids.add(upper)
        if len(upper) <= 3:
            navaid_ids.add(upper)

    stations, fixes, navaids = {}, {}, {}
    for what, call, ids in (
        ("airport", awc.stations, station_ids),
        ("fix", awc.fixes, fix_ids),
        ("navaid", awc.navaids, navaid_ids),
    ):
        if not ids:
            continue
        try:
            result = await call(ids)
        except SourceError as exc:
            for i, text in enumerate(texts):
                if not found[i]:
                    problems[i] = f"{what} lookup failed: {exc}"
            continue
        if what == "airport":
            stations = result
        elif what == "fix":
            fixes = result
        else:
            navaids = result

    for i, text in enumerate(texts):
        if found[i] and found[i][0].score >= EXACT:
            continue
        upper = text.upper()
        navaid = _NAVAID_SUFFIX.match(upper)
        if navaid:
            n = navaids.get(navaid.group(1))
            if n:
                found[i].append(Candidate(
                    Point(n.ident, n.name, "navaid", n.lat, n.lon, None, n.kind), EXACT, f"{n.kind} identifier"
                ))
            continue
        if _IDENT.match(upper):
            for key in (upper, "K" + upper):
                s = stations.get(key)
                if s:
                    found[i].append(Candidate(
                        Point(s.icao, s.name, "airport", s.lat, s.lon, s.elevation_ft, s.state),
                        EXACT, "airport identifier",
                    ))
                    break
            typed_as_ident = text == upper
            if upper in fixes:
                f = fixes[upper]
                found[i].append(Candidate(
                    Point(f.ident, f.ident, "fix", f.lat, f.lon),
                    FIX_TYPED_AS_IDENT if typed_as_ident else FIX_TYPED_AS_WORD, "fix identifier",
                ))
            if upper in navaids:
                n = navaids[upper]
                found[i].append(Candidate(
                    Point(n.ident, n.name, "navaid", n.lat, n.lon, None, n.kind),
                    FIX_TYPED_AS_IDENT if typed_as_ident else FIX_TYPED_AS_WORD, f"{n.kind} identifier",
                ))
            if any(c.score >= FIX_TYPED_AS_IDENT for c in found[i]):
                continue
        found[i].extend(_pass_candidates(text))
        found[i].extend(_name_candidates(text))

    return _decide(texts, found, problems)


def _penalty(candidate: Candidate, refs: list[Point]) -> float:
    if not refs:
        return 0.0
    nearest = min(geo.distance_nm(candidate.point.position, r.position) for r in refs)
    return min(MAX_PENALTY, nearest / PENALTY_NM_PER_POINT)


def _decide(texts: list[str], found: list[list[Candidate]], problems: list[str | None]) -> list[Resolved]:
    """Pick a winner for each item, using already-settled neighbours as context.

    Repeats until nothing changes, so a name settled by its neighbours can in
    turn settle the next one along.
    """
    settled: list[Point | None] = [None] * len(texts)
    for i, cands in enumerate(found):
        top = sorted(cands, key=lambda c: -c.score)
        if top and top[0].score >= FIX_TYPED_AS_IDENT and (
            len(top) == 1 or top[0].score - top[1].score >= MIN_LEAD
        ):
            settled[i] = top[0].point

    ranked: list[list[Candidate]] = [sorted(c, key=lambda c: -c.score) for c in found]
    changed = True
    while changed:
        changed = False
        for i, cands in enumerate(found):
            if settled[i] is not None or not cands:
                continue
            refs = [p for p in _neighbours(settled, i) if p is not None]
            adjusted = sorted(
                (Candidate(c.point, c.score - _penalty(c, refs), c.why) for c in cands),
                key=lambda c: -c.score,
            )
            ranked[i] = adjusted
            if _clear_winner(adjusted):
                settled[i] = adjusted[0].point
                changed = True

    out = []
    for i, text in enumerate(texts):
        cands = ranked[i]
        if settled[i] is not None:
            out.append(Resolved(text, "resolved", settled[i], tuple(cands[:1]), cands[0].why if cands else ""))
        elif cands:
            why = (
                "more than one place matches; ask which one" if len(cands) > 1
                else "only a weak match; confirm it with the member"
            )
            out.append(Resolved(text, "ambiguous", None, tuple(cands[:MAX_CANDIDATES]), why))
        else:
            out.append(Resolved(
                text, "unresolved", None, (),
                problems[i] or "no airport, fix, navaid, pass or coordinates match; ask the member",
            ))
    return out


def _clear_winner(ranked: list[Candidate]) -> bool:
    if not ranked or ranked[0].score < MIN_SCORE:
        return False
    return len(ranked) == 1 or ranked[0].score - ranked[1].score >= MIN_LEAD


def _neighbours(settled: list[Point | None], i: int) -> list[Point | None]:
    """The nearest settled point before and after item i."""
    before = next((settled[j] for j in range(i - 1, -1, -1) if settled[j] is not None), None)
    after = next((settled[j] for j in range(i + 1, len(settled)) if settled[j] is not None), None)
    return [before, after]


def describe(points: list[Point]) -> str:
    """KBJC → Rollins Pass (VPROL) → KGNB."""
    return " → ".join(p.label() for p in points)
