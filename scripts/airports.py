#!/usr/bin/env python3
"""Regenerate data/airports_us.json: public-use US airports, for name lookup.

Members plan routes the way they say them: "Rollins pass, Granby, Kremmling".
aviationweather.gov can look an airport up by identifier but not by name, so
route resolution keeps this small gazetteer to turn "Kremmling" into K20V.

The source is OurAirports (public domain). Only public-use airports go in,
recognised by a three-character FAA identifier. Private strips have
four-character ones (81CO), need the owner's permission anyway, and there are
three of them in Steamboat Springs alone, which would make "Steamboat"
ambiguous for no benefit. A private strip still resolves when typed by
identifier, through the AWC lookup. Run this now and then:

    python scripts/airports.py
"""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import DEFAULT_USER_AGENT  # noqa: E402

SOURCE = "https://davidmegginson.github.io/ourairports-data/airports.csv"
TYPES = {"large_airport": "L", "medium_airport": "M", "small_airport": "S", "seaplane_base": "W"}


def main() -> None:
    req = urllib.request.Request(SOURCE, headers={"User-Agent": DEFAULT_USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        size = TYPES.get(r["type"])
        local = r["local_code"].strip()
        if r["iso_country"] != "US" or size is None:
            continue
        if len(local) != 3 and not r["icao_code"]:
            continue
        ident = r["gps_code"].strip() or r["icao_code"].strip() or r["ident"].strip()
        elev = r["elevation_ft"].strip()
        rows.append([
            ident,
            local,
            r["name"].strip(),
            r["municipality"].strip(),
            r["iso_region"].removeprefix("US-"),
            round(float(r["latitude_deg"]), 4),
            round(float(r["longitude_deg"]), 4),
            int(float(elev)) if elev else None,
            size,
            r["keywords"].strip(),
        ])
    rows.sort(key=lambda row: row[0])
    out = ROOT / "data" / "airports_us.json"
    body = ",\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    out.write_text(
        "[\n" + body + "\n]\n",
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} airports to {out}")


if __name__ == "__main__":
    main()
