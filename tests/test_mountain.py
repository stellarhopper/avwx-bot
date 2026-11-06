import types
from datetime import datetime, timezone

from bot import guildcfg, mountain, regions
from bot.cogs import brief as brief_cog
from bot.cogs import mountain as mountain_cog
from bot.sources.awc import Hazard, Metar, Pirep
from bot.sources.windsaloft import Wind
from bot.store import Store
from conftest import fixture_json
from fakeapi import clients
from fakes import FakeChannel, FakeGuild, FakeInteraction

NOW = datetime(2026, 10, 24, 6, 40, tzinfo=timezone.utc)
REGION = regions.get("co_front_range")


def metar(**over) -> Metar:
    base = next(d for d in fixture_json("metar_mountain.json") if d["icaoId"] == "K0CO")
    return Metar.from_api({**base, **over})


def test_sector_wraps_north():
    assert mountain.in_sector(270, 240, 320)
    assert not mountain.in_sector(200, 240, 320)
    assert mountain.in_sector(10, 340, 20) and not mountain.in_sector(30, 340, 20)
    assert not mountain.in_sector(None, 240, 320)


def test_ridge_level_is_first_level_at_or_above_the_pass():
    levels = {9000: Wind(9000, 270, 10, 0), 12000: Wind(12000, 270, 30, -5)}
    assert mountain.ridge_level(levels, 11307).altitude_ft == 12000
    assert mountain.ridge_level(levels, 9000).altitude_ft == 9000
    assert mountain.ridge_level(levels, 13000) is None


def status(on_pass=None, sides=None, obscured=(), ridge=None) -> mountain.PassStatus:
    p = mountain.PassStatus("berthoud", "Berthoud Pass", 11307, on_pass=on_pass, sides=sides or {})
    return mountain.assess_pass(p, obscured_by=list(obscured), ridge=ridge)


def test_pass_go_caution_no_go():
    assert status(metar()).status == "ok"
    assert status(metar(fltCat="MVFR")).status == "marginal"
    assert status(metar(fltCat="IFR")).status == "adverse"
    assert status(metar(wxString="TSRA")).status == "adverse"
    assert status(metar(), obscured=["mountains obscured (G-AIRMET)"]).status == "adverse"
    assert status().status == "unknown"


def test_pass_ridge_wind_thresholds():
    assert status(metar(), ridge=Wind(12000, 270, 14, 0)).status == "ok"
    assert status(metar(), ridge=Wind(12000, 270, 15, 0)).status == "marginal"
    no_go = status(metar(), ridge=Wind(12000, 270, 25, 0))
    assert no_go.status == "adverse" and no_go.why() == ["25 kt at 12k"]


def test_ifr_on_an_approach_side_is_caution_and_explained():
    s = status(metar(), sides={"east": metar(icaoId="KBJC", fltCat="IFR")})
    assert s.status == "marginal" and s.why() == ["east side KBJC IFR"]


def winds(direction, speed):
    return {"DEN": {12000: Wind(12000, direction, speed, -5), 18000: Wind(18000, direction, speed + 5, -20)}}


def test_wave_risk_from_cross_ridge_flow():
    assert mountain.assess_wave(REGION, winds(270, 15), tango=[], pireps=[], discussion=None).level == "low"
    elevated = mountain.assess_wave(REGION, winds(270, 22), tango=[], pireps=[], discussion=None)
    assert elevated.level == "elevated" and "across the ridge at 18k" in elevated.reasons[0]
    assert mountain.assess_wave(REGION, winds(280, 40), tango=[], pireps=[], discussion=None).level == "high"
    # Strong but along the ridge (southerly): not wave-producing by this rule.
    assert mountain.assess_wave(REGION, winds(180, 50), tango=[], pireps=[], discussion=None).level == "low"


def test_wave_risk_raised_by_tango_pireps_and_discussion():
    tango = Hazard("GAIRMET", "TURB-LO", "x", None, None, [], "170", "SFC", "MOD", "MTN WAVE", "")
    assert mountain.assess_wave(REGION, winds(270, 5), tango=[tango], pireps=[], discussion=None).level == "elevated"
    severe = Pirep.from_api({"rawOb": "DEN UA /TB SEV", "tbInt1": "SEV"})
    assert mountain.assess_wave(REGION, winds(270, 5), tango=[], pireps=[severe], discussion=None).level == "high"
    r = mountain.assess_wave(REGION, winds(270, 5), tango=[], pireps=[], discussion="Expect lenticular clouds.")
    assert r.level == "elevated" and "lenticular" in r.reasons[0]
    assert mountain.assess_wave(REGION, {}, tango=[], pireps=[], discussion=None).level == "unknown"


async def test_build_from_fixtures():
    api = clients()
    board = await mountain.build(api.awc, api.nws, REGION, guildcfg.effective(None), now=NOW)
    assert len(board.passes) == len(REGION["passes"])
    assert {p.status for p in board.passes} <= {"ok", "marginal", "adverse", "unknown"}
    assert set(board.ridge_winds) == {"DEN", "GJT", "PUB", "ALS"}
    assert board.wave.level == "elevated"  # the fixture's Tango and wave PIREPs
    assert any(d.station == "KLXV" for d in board.density)
    assert board.problems == []


async def test_build_respects_chosen_passes():
    api = clients()
    cfg = guildcfg.effective({"schema_version": 1, "mountain": {"enabled": True, "passes": ["kenosha", "berthoud"]}})
    board = await mountain.build(api.awc, api.nws, REGION, cfg, now=NOW)
    assert [p.id for p in board.passes] == ["berthoud", "kenosha"]


def make_bot(config):
    api = clients()
    bot = types.SimpleNamespace(store=Store(":memory:"), awc=api.awc, nws=api.nws, http_client=api.http, guilds=[])
    guild = FakeGuild([FakeChannel("weather")])
    if config is not None:
        bot.store.set_config(guild.id, config, actor_id=None)
    return bot, guild


async def test_mountain_command_is_off_until_enabled():
    bot, guild = make_bot(None)
    cog = mountain_cog.Mountain(bot)
    i = FakeInteraction(guild)
    await cog.mountain_board.callback(cog, i)
    assert "isn't switched on" in i.reply

    bot, guild = make_bot({"schema_version": 1, "mountain": {"enabled": True}})
    cog = mountain_cog.Mountain(bot)
    i = FakeInteraction(guild)
    await cog.mountain_board.callback(cog, i)
    assert "wave risk **elevated**" in i.reply and "weathercams.faa.gov" in i.reply
    assert i.last["file"].filename == "mountain.png"


async def test_brief_includes_mountain_only_when_enabled():
    bot, guild = make_bot(None)
    cog = brief_cog.Briefs(bot)
    on = guildcfg.effective({"schema_version": 1, "home_airports": ["KBJC"],
                             "mountain": {"enabled": True}, "brief": {"include": {"mountain": True}}})
    data, _ = await cog.build(on, ["KBJC"], title="T")
    assert data.mountain is not None
    off = guildcfg.effective({"schema_version": 1, "home_airports": ["KBJC"], "brief": {"include": {"mountain": True}}})
    data, _ = await cog.build(off, ["KBJC"], title="T")
    assert data.mountain is None


def test_each_pass_box_gives_the_reason_for_its_status():
    from bot.render.mountain import chip_reason

    p = mountain.PassStatus("rollins", "Rollins Pass", 11671)
    assert chip_reason(p) == "No data"
    p.worsen("adverse", "mountains obscured (G-AIRMET)")
    p.worsen("adverse", "IFR (G-AIRMET)")
    assert chip_reason(p) == "Mountains obscured (G-AIRMET) +1"
    q = mountain.PassStatus("kenosha", "Kenosha Pass", 10001)
    q.worsen("ok", "K4BM VFR")
    q.worsen("marginal", "18 kt at 12k")
    assert chip_reason(q) == "18 kt at 12k"  # the deciding reason, not the fine one


def test_mountain_data_failures_reach_the_brief_warnings():
    import types

    from bot.render.brief import problems

    board = types.SimpleNamespace(problems=["METARs: aviationweather.gov is having trouble (HTTP 502)."])
    data = types.SimpleNamespace(problems=["Winds aloft: aviationweather.gov is having trouble (HTTP 502)."],
                                 mountain=board)
    assert problems(data) == [
        "Winds aloft: aviationweather.gov is having trouble (HTTP 502).",
        "Mountain data — METARs: aviationweather.gov is having trouble (HTTP 502).",
    ]


async def test_an_empty_winds_forecast_is_a_reported_failure():
    import pytest as _pytest

    from bot import briefing
    from bot.sources.http import SourceError

    c = clients(windtemp=(204, ""))
    with _pytest.raises(SourceError, match="winds-aloft"):
        await c.awc.winds_aloft()
    data = await briefing.gather(c.awc, c.nws, title="t", airports=["KBJC"], config=guildcfg.effective(None),
                                 include={"winds_aloft": True}, now=NOW)
    assert data.winds is None and any(p.startswith("Winds aloft:") for p in data.problems)
    board = await mountain.build(c.awc, c.nws, REGION, guildcfg.effective(None), NOW)
    # No ridge winds, and the board says why. (Wave risk can still be raised
    # by the Tango G-AIRMET and PIREPs in the fixtures.)
    assert board.ridge_winds == {} and all(p.ridge_wind is None for p in board.passes)
    assert any(p.startswith("Winds aloft:") for p in board.problems)


async def test_a_long_dead_station_does_not_rate_its_pass():
    import json

    from fakeapi import all_metars

    def metars(params):
        ids = set(params.get("ids", "").split(","))
        out = []
        for d in all_metars():
            if d["icaoId"] in ids:
                if d["icaoId"] == "KC99":  # Rollins' station, last heard from weeks ago
                    d = {**d, "obsTime": d["obsTime"] - 20 * 86400}
                out.append(d)
        return 200, json.dumps(out)

    c = clients(metar=metars)
    board = await mountain.build(c.awc, c.nws, REGION, guildcfg.effective(None), NOW)
    rollins = next(p for p in board.passes if p.id == "rollins")
    assert rollins.on_pass is None
    assert not any("KC99" in why for _, why in rollins.reasons)
