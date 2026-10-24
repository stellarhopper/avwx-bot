import asyncio
from datetime import datetime, timezone

import pytest

from bot.sources import awc, nws
from bot.sources.http import Http, SourceError
from conftest import fixture_json


def metars():
    return {m.station: m for m in (awc.Metar.from_api(d) for d in fixture_json("metar.json"))}


def test_metar_model():
    m = metars()["KAPA"]
    assert m.wind_dir == 230 and m.wind_speed == 10 and m.wind_gust is None
    assert m.visibility_sm == 10
    assert m.ceiling_ft == 22000
    assert m.altimeter_inhg == 30.04  # from A3004 in the raw text, not the hPa field
    assert m.flight_category == "VFR"
    assert 5870 < m.elevation_ft < 5880
    assert m.observed == datetime(2026, 10, 24, 5, 53, tzinfo=timezone.utc)


def test_metar_density_altitude():
    m = metars()["KAPA"]
    assert 7100 < m.density_altitude_ft < 7300


def test_metar_gust_and_international():
    assert metars()["KDEN"].wind_gust == 17
    egll = metars()["EGLL"]
    assert egll.weather == "-RA" and egll.altimeter_hpa == 1012 and egll.altimeter_inhg is not None


def test_taf_model():
    taf = awc.Taf.from_api(fixture_json("taf.json")[0])
    assert taf.station == "KAPA"
    tempo = [p for p in taf.periods if p.change == "TEMPO"]
    assert tempo and tempo[0].is_conditional and tempo[0].flight_category == "MVFR"
    prevailing = taf.prevailing_at(datetime(2026, 10, 24, 16, tzinfo=timezone.utc))
    assert prevailing.wind_gust == 22


def test_sigmet_and_gairmet_models():
    sigmets = [awc._sigmet(d) for d in fixture_json("airsigmet.json")]
    convective = [s for s in sigmets if s.source == "CONVECTIVE"]
    assert convective and convective[0].polygon and convective[0].top
    g = [awc._gairmet(d) for d in fixture_json("gairmet.json") if d["geometryType"] == "AREA"]
    wave = [h for h in g if h.hazard == "TURB-LO" and h.due_to == "MTN WAVE"]
    assert wave and isinstance(wave[0].polygon[0][0], float)


def test_pirep_model():
    reps = [awc.Pirep.from_api(d) for d in fixture_json("pirep.json")]
    assert any(r.turbulence == "MOD" for r in reps)
    assert all(not r.urgent for r in reps)


def test_nws_alert_model():
    alerts = [nws.Alert.from_feature(f) for f in fixture_json("nws_alerts_co.json")["features"]]
    flood = next(a for a in alerts if a.urgency == "Immediate")
    assert flood.event == "Flood Warning"
    lat, lon = flood.polygons[0][0]
    assert 36 < lat < 42 and -110 < lon < -101  # (lat, lon), not GeoJSON order
    assert flood.references


def test_afd_aviation_section():
    text = fixture_json("afd_bou.json")["productText"]
    section = nws.aviation_section(text)
    assert section.startswith("Current radar shows")
    assert "&&" not in section and "Issued at" not in section


class FakeHttp(Http):
    def __init__(self, responses):
        super().__init__("test")
        self.responses = responses
        self.calls = 0

    async def _fetch(self, url, params, accept):
        self.calls += 1
        await asyncio.sleep(0.01)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


async def test_http_caches_and_shares_inflight_requests():
    http = FakeHttp({"https://x/a": (200, '{"v": 1}')})
    results = await asyncio.gather(*(http.get_json("https://x/a", ttl=60) for _ in range(5)))
    assert results == [{"v": 1}] * 5
    assert http.calls == 1
    assert await http.get_json("https://x/a", ttl=60) == {"v": 1}
    assert http.calls == 1


async def test_http_errors_are_source_errors_and_not_cached():
    http = FakeHttp({"https://x/a": (503, "down")})
    with pytest.raises(SourceError, match="HTTP 503"):
        await http.get_json("https://x/a", ttl=60)
    http.responses["https://x/a"] = (200, "[]")
    assert await http.get_json("https://x/a", ttl=60) == []


async def test_http_204_is_none():
    http = FakeHttp({"https://x/a": (204, "")})
    assert await http.get_json("https://x/a", ttl=60) is None
