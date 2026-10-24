"""Regional reference data: mountain passes, winds-aloft sites, NWS offices.

Each region is one JSON file in data/regions/, so adding an area (the Wasatch,
the Sierra) is a data change, not a code change. The configurator site is
published with the same files, so its pass picker always matches the bot.
"""

from __future__ import annotations

import json
from functools import cache

from .config import ROOT

REGION_DIR = ROOT / "data" / "regions"


@cache
def all_regions() -> dict[str, dict]:
    out = {}
    for path in sorted(REGION_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out[data["id"]] = data
    return out


def get(region_id: str | None) -> dict | None:
    return all_regions().get(region_id or "")


def pass_ids(region_id: str) -> set[str]:
    region = get(region_id)
    return {p["id"] for p in region.get("passes", [])} if region else set()
