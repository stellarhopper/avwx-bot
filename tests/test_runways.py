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


def test_light_wind_takes_the_longest_runway():
    apa = ends_for("KAPA")
    for wind in [(None, 0, None), (230, 0, None), (None, 3, None), (100, 2, 3)]:
        assert runways.best(apa, *wind).end.length_ft == 10001, wind  # 17L/35R
    # 4 kt is enough to choose by: 28 is best aligned with a 100° wind.
    assert runways.best(apa, 280, 4, None).end.name == "28"


def test_stronger_variable_wind_has_no_choice():
    assert runways.best(ends_for("KAPA"), None, 5, None) is None
    assert runways.best([], 230, 10, None) is None


def test_options_add_the_longest_in_wind_under_10_kt():
    bjc = ends_for("KBJC")
    names = [c.end.name for c in runways.options(bjc, 30, 6, None)]
    assert names == ["03", "30R"]  # best aligned, then the 9,000 ft runway
    assert [c.end.name for c in runways.options(bjc, 30, 12, None)] == ["03"]  # 10 kt+: just the best
    assert [c.end.name for c in runways.options(bjc, 30, 6, 10)] == ["03"]  # gusts count toward the 10
    assert len(runways.options(ends_for("KAPA"), 230, 8, None)) == 1  # best is already the longest


def test_arrows():
    choice = runways.best(ends_for("KAPA"), 230, 10, 20)
    assert runways.arrows(choice) == "↓6  ←8 G16"  # crosswind from the right blows left
    left = runways.best(ends_for("EGLL"), 210, 9, None)
    assert runways.arrows(left) == "↓5  →8"
    straight = runways.best(ends_for("KDEN"), 270, 18, None)
    assert runways.arrows(straight).endswith("↔0")
    seventeen_l = next(e for e in ends_for("KAPA") if e.name == "17L")  # 178° true
    tail = runways._choice(seventeen_l, 358, 10, None)  # wind from straight behind
    assert runways.arrows(tail) == "↑10  ↔0"


def test_describe():
    choice = runways.best(ends_for("KAPA"), 230, 10, 20)
    assert runways.describe(choice) == "RWY 17L: headwind 6 kt, crosswind 8 kt from the right (gusts 16)"


async def test_metar_command_includes_runway_in_alt_text():
    api = clients()
    bot = type("Bot", (), {"store": Store(":memory:"), "awc": api.awc})()
    cog = wx_cog.Wx(bot)
    i = FakeInteraction(FakeGuild([]))
    await cog.metar.callback(cog, i, "KAPA")
    assert "RWY 17L: headwind 6 kt" in i.last["files"][0].description


async def test_metar_command_survives_missing_runway_data():
    api = clients(airport=(503, "down"))
    bot = type("Bot", (), {"store": Store(":memory:"), "awc": api.awc})()
    cog = wx_cog.Wx(bot)
    i = FakeInteraction(FakeGuild([]))
    await cog.metar.callback(cog, i, "KAPA")
    image = i.last["files"][0]
    assert image.filename == "metar.png" and "RWY" not in image.description


def end(name, heading, length):
    return runways.RunwayEnd(name, heading, length, "x")


def test_within_five_degrees_the_longer_runway_wins():
    short_aligned = end("27", 270, 5000)
    long_off = end("28", 274, 9000)  # 4° further from the wind
    assert runways.best([short_aligned, long_off], 270, 15, None).end.name == "28"


def test_beyond_five_degrees_alignment_wins():
    short_aligned = end("27", 270, 5000)
    long_off = end("28", 276, 9000)  # 6° off: no longer "about equal"
    assert runways.best([short_aligned, long_off], 270, 15, None).end.name == "27"


def test_equal_length_within_margin_takes_the_better_aligned():
    assert runways.best([end("27", 273, 9000), end("28", 271, 9000)], 270, 15, None).end.name == "28"


def test_taf_labels_lead_with_local_time():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from bot.render.cards import _period_label
    from bot.sources.awc import TafPeriod

    utc = timezone.utc
    p = TafPeriod(datetime(2026, 10, 24, 20, tzinfo=utc), datetime(2026, 10, 25, 0, tzinfo=utc), "TEMPO", None,
                  None, None, None, None, "", [], None, "", None)
    assert _period_label(p, ZoneInfo("America/Denver")) == ("TEMPO Sat 14–18", "20–00Z")
