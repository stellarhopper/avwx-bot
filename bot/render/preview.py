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
from .. import briefing, hazards
from ..sources import awc, nws, windsaloft
from ..sources.awc import Metar, Taf
from ..wx import fmt
from . import brief, cards, theme, winds

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

    save("brief.png", brief.render(sample_brief()))
    w = sample_brief().winds
    save("winds.png", winds.winds_card(w.site, w.valid, w.use, w.levels, units=fmt.Units()))
    return written


def sample_brief() -> briefing.BriefData:
    """A brief assembled from the fixtures, the way briefing.gather would."""
    metars = {m.station: m for m in (Metar.from_api(d) for d in load("metar.json"))}
    tafs = {t.station: t for t in (Taf.from_api(d) for d in load("taf.json"))}
    airports = ["KBJC", "KAPA", "KDEN", "KLXV"]
    stations = {
        s: awc.Station(s, s, metars[s].lat, metars[s].lon, metars[s].elevation_ft, "CO", "US") for s in airports
    }
    points = [st.position for st in stations.values()]
    area = [awc._sigmet(d) for d in load("airsigmet.json")] + [
        awc._gairmet(d) for d in load("gairmet.json") if d["geometryType"] == "AREA"
    ]
    fb = windsaloft.parse((FIXTURES / "windtemp_all.txt").read_text() + (FIXTURES / "windtemp_high.txt").read_text())
    alerts = [nws.Alert.from_feature(f) for f in load("nws_alerts_co.json")["features"]]
    afd = nws.Discussion("BOU", datetime(2026, 10, 24, 5, 30, tzinfo=timezone.utc), load("afd_bou.json")["productText"])
    return briefing.BriefData(
        title="Morning brief — Front Range",
        generated=NOW,
        tz=TZ,
        units=fmt.Units(),
        airports=[briefing.AirportBrief(a, metars.get(a), tafs.get(a)) for a in airports],
        stations=stations,
        hazards=hazards.near(area, points, 150, NOW, hours=6),
        alerts=[a for a in alerts if hazards.alert_near(a, points, 150)][:3],
        pireps=[awc.Pirep.from_api(d) for d in load("pirep.json")],
        winds=briefing.WindsAloft("DEN", fb.valid, f"{fb.use_from}–{fb.use_to}Z", fb.site("DEN")),
        afd=briefing.Afd("BOU", afd.issued, afd.aviation),
    )


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "preview"
    for path in render_all(target):
        print(path)
