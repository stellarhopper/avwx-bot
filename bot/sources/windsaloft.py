"""Parser for the FB (formerly FD) winds and temperatures aloft forecast.

The product is fixed-width text:

    FT  3000    6000    9000   12000   18000   24000  30000  34000  39000
    DEN              9900+10 2117+03 2231-12 2262-24 207738 209249 228754

Each column's value ends at the same character position as its altitude in the
FT header line, which is how blank columns (levels below the station, or within
1,500 ft of it) are told apart from present ones.

Each group is DDSS[±TT]:

* DD is direction in tens of degrees, SS speed in knots.
* 9900 is light and variable.
* DD of 51–86 means speed of 100–199 kt: subtract 50 from DD, add 100 to SS.
* Temperature is signed up to 24,000 ft. Above that it is always negative and
  the sign is left out (``207738`` is 200° at 77 kt, −38 °C).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADER = re.compile(r"^FT\s+\d")
_VALID = re.compile(r"VALID\s+(\d{6}Z)\s+FOR USE\s+(\d{4})-(\d{4})Z")


@dataclass(frozen=True, slots=True)
class Wind:
    altitude_ft: int
    direction: int | None  # None for light and variable
    speed_kt: int
    temp_c: int | None

    @property
    def light_variable(self) -> bool:
        return self.direction is None


@dataclass
class Forecast:
    valid: str | None = None  # e.g. "240600Z"
    use_from: str | None = None  # e.g. "0200"
    use_to: str | None = None
    sites: dict[str, dict[int, Wind]] = field(default_factory=dict)

    def site(self, ident: str) -> dict[int, Wind]:
        ident = ident.upper()
        if len(ident) == 4 and ident.startswith("K"):
            ident = ident[1:]
        return self.sites.get(ident, {})


def decode(group: str, altitude_ft: int) -> Wind | None:
    group = group.strip()
    if len(group) < 4 or not group[:4].isdigit():
        return None
    dd, ss = int(group[:2]), int(group[2:4])
    temp: int | None = None
    rest = group[4:]
    if rest:
        if rest[0] in "+-":
            try:
                temp = int(rest)
            except ValueError:
                temp = None
        elif rest.isdigit():
            temp = -int(rest)
    if dd == 99 and ss == 0:
        return Wind(altitude_ft, None, 0, temp)
    if dd >= 51:
        dd -= 50
        ss += 100
    if dd > 36:
        return None
    direction = 360 if dd in (0, 36) else dd * 10
    return Wind(altitude_ft, direction, ss, temp)


def parse(text: str) -> Forecast:
    forecast = Forecast()
    columns: list[tuple[int, int, int]] = []  # (start, end, altitude)
    for line in text.splitlines():
        line = line.rstrip()
        valid = _VALID.search(line)
        if valid and forecast.valid is None:
            forecast.valid, forecast.use_from, forecast.use_to = valid.groups()
            continue
        if _HEADER.match(line):
            columns = []
            prev_end = 3
            for match in re.finditer(r"\d+", line):
                columns.append((prev_end, match.end(), int(match.group())))
                prev_end = match.end()
            continue
        if not columns or len(line) < 4 or not line[:3].strip().isalnum():
            continue
        if line[3] != " ":
            continue
        ident = line[:3].strip()
        levels = forecast.sites.setdefault(ident, {})
        for start, end, altitude in columns:
            chunk = line[start:end].strip()
            if not chunk:
                continue
            wind = decode(chunk, altitude)
            if wind is not None:
                levels[altitude] = wind
    return forecast
