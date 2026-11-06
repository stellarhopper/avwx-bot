"""Model winds from Open-Meteo standing in when no FB forecast is published."""

import json

from bot import briefing, guildcfg, mountain, regions
from bot.chat import tools
from bot.fleet import FleetCache
from bot.render import brief as brief_render
from bot.render import mountain as mountain_render
from bot.render import winds as winds_render
from bot.sources import openmeteo, windsaloft
from bot.wx import fmt
from conftest import FIXTURE_TIME, FIXTURES
from fakeapi import clients

NO_FB = {"windtemp": (204, "")}
REGION = regions.get("co_front_range")


def test_pressure_levels_sit_at_their_standard_altitudes():
    # ICAO standard atmosphere tables: 700 hPa 3,012 m, 500 hPa 5,574 m, 250 hPa 10,363 m.
    for hpa, metres in ((700, 3012), (500, 5574), (250, 10363)):
        assert abs(openmeteo.pressure_altitude_ft(hpa) - metres * 3.28084) < 25


async def test_the_fb_is_used_when_there_is_one():
    c = clients()
    forecast = await c.awc.winds_aloft()
    assert forecast.model is None and windsaloft.disclaimer(forecast.model) is None
    assert not any("open-meteo" in url for url, _ in c.http.requests)


async def test_model_winds_stand_in_for_a_missing_fb():
    c = clients(**NO_FB)
    forecast = await c.awc.winds_aloft()
    assert forecast.model == openmeteo.MODEL
    # Fixture time 06:40Z: valid 09Z, for use 06-12Z, like a 06 h FB.
    assert (forecast.valid, forecast.use_from, forecast.use_to) == ("240900Z", "0600", "1200")
    den = forecast.site("DEN")
    # Denver's model ground is 5,400 ft, so 3k and 6k are left out, as in the FB.
    assert 3000 not in den and 6000 not in den and {9000, 12000, 18000, 39000} <= set(den)
    # 18,000 ft is within 300 ft of 500 hPa, so it is nearly that level's value.
    captured = json.loads((FIXTURES / "openmeteo_gfs.json").read_text())[0]["hourly"]
    i = captured["time"].index("2026-10-24T09:00")
    assert abs(den[18000].speed_kt - captured["wind_speed_500hPa"][i]) <= 1
    assert abs(den[18000].direction - captured["wind_direction_500hPa"][i]) <= 3
    assert "GFS model" in windsaloft.disclaimer(forecast.model)


async def test_one_model_request_serves_every_forecast_time():
    c = clients(**NO_FB)
    later = [await c.awc.winds_aloft(h) for h in ("06", "12", "24")]
    assert [f.valid for f in later] == ["240900Z", "241500Z", "250000Z"]
    assert [(f.use_from, f.use_to) for f in later] == [("0600", "1200"), ("1200", "1800"), ("1800", "0600")]
    model_requests = [p for url, p in c.http.requests if "open-meteo" in url]
    assert len(model_requests) == 1
    # Every FB site, in one request.
    assert len(model_requests[0]["latitude"].split(",")) == len(json.loads(
        (FIXTURES.parent.parent / "data" / "fb_sites.json").read_text()))


async def test_the_board_and_brief_say_the_winds_are_model_winds():
    c = clients(**NO_FB)
    board = await mountain.build(c.awc, c.nws, REGION, guildcfg.effective(None), FIXTURE_TIME)
    assert board.ridge_winds and board.winds_model == openmeteo.MODEL
    assert not any(p.startswith("Winds aloft") for p in board.problems)
    assert "GFS model" in mountain_render.board_alt(board)
    mountain_render.render(board, tz=None, units=fmt.Units())

    data = await briefing.gather(c.awc, c.nws, title="t", airports=["KBJC"], config=guildcfg.effective(None),
                                 include={"winds_aloft": True}, now=FIXTURE_TIME)
    assert data.winds is not None and data.winds.model == openmeteo.MODEL
    data.mountain = board
    brief_render.render(data)

    w = data.winds
    winds_render.winds_card(w.site, w.valid, w.use, w.levels, units=fmt.Units(), model=w.model)
    assert "GFS model" in winds_render.winds_alt(w.site, w.levels, w.model)


async def test_chat_results_lead_with_the_disclaimer():
    from test_chat_tools import CLUB

    c = clients(**NO_FB)
    cfg = guildcfg.effective({"schema_version": 1, "timezone": "America/Denver"})
    ctx = tools.ToolContext(c.awc, c.nws, FleetCache(CLUB), cfg, lambda: FIXTURE_TIME)

    content, err = await tools.run(ctx, "mountain_passes", {"passes": None})
    assert not err and any("GFS model" in a for a in json.loads(content)["attention"])

    content, err = await tools.run(ctx, "flight_plan", {
        "route": ["KBJC", "KGNB"], "tail": "N9999X", "departure_local": "2026-10-24 01:00",
        "cruise_altitude_ft": 13500, "stops": None,
    })
    out = json.loads(content)
    assert not err and any("GFS model" in a for a in out["attention"])
    assert all(w["wind"] for w in out["winds_aloft"])
