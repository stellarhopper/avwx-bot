"""Render every card from the test fixtures, to check layouts by eye.

    python -m bot.render.preview [out_dir]

No network and no Discord: the inputs are the saved API responses in
tests/fixtures/, with "now" pinned to just after they were captured.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..config import ROOT
from ..sources.awc import Metar, Taf
from ..wx import fmt
from . import cards, theme

FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 10, 24, 6, 40, tzinfo=timezone.utc)
TZ = fmt.zone("America/Denver")


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def render_all(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []

    def save(name: str, data: bytes) -> None:
        path = out / name
        path.write_bytes(data)
        written.append(path)

    metars = [Metar.from_api(d) for d in load("metar.json")]
    save("metar.png", theme.stack([cards.metar_card(m, units=fmt.Units(), tz=TZ, now=NOW) for m in metars]))
    tafs = [Taf.from_api(d) for d in load("taf.json")]
    save("taf.png", theme.stack([cards.taf_card(t, tz=TZ, now=NOW) for t in tafs]))
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "preview"
    for path in render_all(target):
        print(path)
