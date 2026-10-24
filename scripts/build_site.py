#!/usr/bin/env python3
"""Assemble the configurator site into _site/ (or the directory given).

The page needs the config schema and the region data, which live elsewhere in
the repo because the bot uses them too. Copying them in at build time keeps a
single copy of each in git. Used by .github/workflows/pages.yml and for local
testing:

    python scripts/build_site.py && python -m http.server -d _site 8000
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "site", out)
    shutil.copy(ROOT / "schema" / "config.schema.json", out / "config.schema.json")
    regions = {}
    for path in sorted((ROOT / "data" / "regions").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        # The page only needs names for its pickers.
        regions[data["id"]] = {
            "id": data["id"],
            "name": data["name"],
            "passes": [{"id": p["id"], "name": p["name"]} for p in data.get("passes", [])],
        }
    (out / "regions.json").write_text(json.dumps(regions, indent=1), encoding="utf-8")
    print(f"built {out} ({len(regions)} region(s))")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site")
