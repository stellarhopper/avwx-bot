import types

from bot.cogs import wx as wx_cog
from bot.store import Store
from fakeapi import clients
from fakes import GUILD, FakeGuild, FakeInteraction

# The flying club's home airports, in their order. Of these, only KBJC,
# KAPA, KCOS and KDEN issue TAFs; in the fixtures only KAPA and KDEN do.
CLUB_HOME = ["KBJC", "KBDU", "KLMO", "KFNL", "KGXY", "KAPA", "KCOS", "KDEN"]


def setup(home):
    store = Store(":memory:")
    store.set_config(GUILD, {"schema_version": 1, "home_airports": home}, actor_id=None)
    bot = types.SimpleNamespace(store=store, awc=clients().awc)
    return wx_cog.Wx(bot), FakeInteraction(FakeGuild())


async def run_taf(cog, interaction, airports=None):
    await cog.taf.callback(cog, interaction, airports=airports)
    _kind, content, kwargs = interaction.record[-1]
    return content, kwargs


async def test_bare_taf_finds_the_airports_that_have_one_anywhere_in_the_list():
    cog, i = setup(CLUB_HOME)
    content, kwargs = await run_taf(cog, i)
    # KAPA and KDEN are sixth and eighth: past a first-four cut, but shown.
    assert "TAF KAPA" in content and "TAF KDEN" in content
    assert "No TAF" not in content
    assert kwargs["file"].filename == "taf.png"


async def test_typed_airports_without_a_taf_are_left_out_quietly():
    cog, i = setup(CLUB_HOME)
    content, _ = await run_taf(cog, i, "bjc apa")
    assert "TAF KAPA" in content and "KBJC" not in content and "No TAF" not in content


async def test_a_list_with_no_tafs_says_so():
    cog, i = setup(["KBDU", "KLMO"])
    content, kwargs = await run_taf(cog, i)
    assert content.startswith("None of these airports has a TAF: KBDU, KLMO.")
    assert "file" not in kwargs
