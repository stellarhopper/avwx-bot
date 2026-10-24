#!/usr/bin/env python3
"""Regenerate data/fb_sites.json: where each winds-aloft (FB) site is.

The FB product names its sites (DEN, GJT, ...) but gives no coordinates, and
the brief needs them to pick the site nearest a server's airports. Each site is
looked up as the airport of the same name (K-prefixed, or P- in Alaska and
Hawaii). The dozen offshore and VOR-only sites with no such airport are left
out. Run this if the FB site list ever changes:

    python scripts/fb_sites.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import DEFAULT_USER_AGENT  # noqa: E402
from bot.sources import windsaloft  # noqa: E402

BASE = "https://aviationweather.gov/api/data"


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode()


def main() -> None:
    text = get(f"{BASE}/windtemp?region=all&level=low&fcst=06")
    sites = sorted(windsaloft.parse(text).sites)
    candidates = [prefix + s for s in sites for prefix in ("K", "P")]
    found: dict[str, list[float]] = {}
    for i in range(0, len(candidates), 40):
        chunk = ",".join(candidates[i : i + 40])
        for d in json.loads(get(f"{BASE}/stationinfo?format=json&ids={chunk}") or "[]"):
            site = d["icaoId"][1:]
            if site in sites and site not in found:
                found[site] = [round(d["lat"], 3), round(d["lon"], 3)]
    out = ROOT / "data" / "fb_sites.json"
    lines = ",\n".join(f'  "{k}": {json.dumps(v)}' for k, v in sorted(found.items()))
    out.write_text("{\n" + lines + "\n}\n", encoding="utf-8")
    missing = sorted(set(sites) - set(found))
    print(f"wrote {len(found)} sites to {out}; no airport for: {', '.join(missing)}")


if __name__ == "__main__":
    main()
