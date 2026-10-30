from datetime import datetime, timedelta, timezone

from bot import flightwx, route
from bot.sources.windsaloft import Forecast
from conftest import FIXTURE_TIME
from fakeapi import clients


async def plan(items, **kw):
    c = clients()
    points = [r.point for r in await route.resolve(c.awc, items)]
    kw.setdefault("departure", FIXTURE_TIME)
    kw.setdefault("now", FIXTURE_TIME)
    return await flightwx.plan(c.awc, c.nws, points, cruise_alt_ft=13500, **kw)


def test_fb_use_window_crosses_midnight():
    fc = Forecast(valid="250000Z", use_from="1800", use_to="0600")
    start, end = flightwx.use_window(fc, FIXTURE_TIME)
    assert start == datetime(2026, 10, 24, 18, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 25, 6, tzinfo=timezone.utc)


async def test_mountain_route_now():
    rw = await plan(["KBJC", "Rollins pass", "Granby", "Kremmling", "SBS", "KBJC"], stops={2, 4})
    assert [f.dest for f in rw.log.flights] == ["KGNB", "KSBS", "KBJC"]
    assert [a.ident for a in rw.airports] == ["KBJC", "KGNB", "K20V", "KSBS", "KBJC"]
    assert [a.role for a in rw.airports][:3] == ["departure", "stop", "waypoint"]
    dep = rw.airports[0]
    assert dep.wind_source == "METAR" and dep.density_source.startswith("METAR")
    assert dep.runway is not None
    # Rollins on the way out and back, Rabbit Ears between Kremmling and Steamboat.
    assert {p.id for p in rw.passes} == {"rollins", "rabbit_ears"}
    assert rw.wave is not None
    assert rw.highest == ("Rollins Pass (VPROL)", 11671)
    assert all(w.site == "DEN" and w.wind is not None for w in rw.leg_winds)
    assert [a.office for a in rw.afds] == ["BOU"]


async def test_later_arrival_uses_the_nws_hourly_forecast():
    # The hourly fixture covers 30 Oct; the METAR is too old to stand in.
    t = datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
    rw = await plan(["KBJC", "KGNB"], departure=t, now=t - timedelta(hours=12))
    gnb = rw.airports[-1]
    assert gnb.wind_source == "NWS hourly forecast"
    assert gnb.hourly is not None and gnb.density_source.startswith("forecast temperature")
    assert any("outside the period" in p for p in rw.problems)


async def test_a_failing_source_costs_only_its_section():
    c = clients(gairmet=(503, ""), airsigmet=(503, ""))
    points = [r.point for r in await route.resolve(c.awc, ["KBJC", "KGNB"])]
    rw = await flightwx.plan(c.awc, c.nws, points, departure=FIXTURE_TIME, cruise_alt_ft=12500, now=FIXTURE_TIME)
    assert rw.hazards is None
    assert any(p.startswith("AIRMETs/SIGMETs") for p in rw.problems)
    assert rw.airports[0].metar is not None
