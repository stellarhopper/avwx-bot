import pytest

from bot.cogs import wx as wx_cog
from bot.store import Store
from bot.wx import runways
from conftest import fixture_json
from fakeapi import clients
from fakes import FakeGuild, FakeInteraction


def ends_for(icao):
    a = next(a for a in fixture_json("airport.json") if a["icaoId"] == icao)
    return runways.runway_ends(a["runways"], a["magdec"])


@pytest.mark.parametrize("raw, value", [("09E", 9), ("12W", -12), ("0", 0), (None, 0), ("junk", 0)])
def test_magnetic_variation(raw, value):
    assert runways.magnetic_variation(raw) == value


def test_ends_use_true_alignment_and_skip_helipads():
    apa = {e.name: e for e in ends_for("KAPA")}
    assert set(apa) == {"10", "28", "17L", "35R", "17R", "35L"}  # H1 is not a runway
    assert apa["17L"].heading == 178 and apa["35R"].heading == 358
    assert apa["17L"].opposite == "35R"


def test_old_magnetic_numbers_still_get_the_right_end():
    # KDEN's 16/34 were numbered for 1990s variation and now point 181° true.
    den = {e.name: e for e in ends_for("KDEN")}
    assert den["16L"].heading == 181 and den["34R"].heading == 1


def test_unpaved_runways_are_ignored():
    rw = [{"id": "09/27", "surface": "T", "alignment": 90, "dimension": "2000x60"}]
    assert runways.runway_ends(rw, "0") == []


def test_components():
    head, cross = runways.components(270, 10, 270)
    assert (round(head), round(cross)) == (10, 0)
    head, cross = runways.components(300, 10, 270)  # 30° off the nose, from the right
    assert round(head, 1) == 8.7 and round(cross) == 5
    head, cross = runways.components(240, 10, 270)
    assert round(cross) == -5  # from the left


def test_best_picks_most_headwind_then_least_crosswind_then_longest():
    apa = ends_for("KAPA")
    choice = runways.best(apa, 230, 10, None)
    assert choice.end.name == "17L"  # 17L and 17R tie on wind; 17L is longer
    assert round(choice.headwind) == 6 and round(choice.crosswind) == 8 and choice.cross_from == "R"
    gusty = runways.best(apa, 230, 10, 20)
    assert round(gusty.gust_crosswind) == 16


def test_best_is_none_when_calm_or_variable():
    apa = ends_for("KAPA")
    assert runways.best(apa, None, 5, None) is None
    assert runways.best(apa, 230, 0, None) is None
    assert runways.best([], 230, 10, None) is None


def test_describe():
    choice = runways.best(ends_for("KAPA"), 230, 10, 20)
    assert runways.describe(choice) == "RWY 17L: headwind 6 kt, crosswind 8 kt from the right (gusts 16)"


async def test_metar_command_includes_runway_in_alt_text():
    api = clients()
    bot = type("Bot", (), {"store": Store(":memory:"), "awc": api.awc})()
    cog = wx_cog.Wx(bot)
    i = FakeInteraction(FakeGuild([]))
    await cog.metar.callback(cog, i, "KAPA")
    assert "RWY 17L: headwind 6 kt" in i.last["file"].description


async def test_metar_command_survives_missing_runway_data():
    api = clients(airport=(503, "down"))
    bot = type("Bot", (), {"store": Store(":memory:"), "awc": api.awc})()
    cog = wx_cog.Wx(bot)
    i = FakeInteraction(FakeGuild([]))
    await cog.metar.callback(cog, i, "KAPA")
    assert i.last["file"].filename == "metar.png" and "RWY" not in i.last["file"].description
