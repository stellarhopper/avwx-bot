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
    assert [f.filename for f in kwargs["files"]] == ["taf.png"]


async def test_typed_airports_without_a_taf_are_left_out_quietly():
    cog, i = setup(CLUB_HOME)
    content, _ = await run_taf(cog, i, "bjc apa")
    assert "TAF KAPA" in content and "KBJC" not in content and "No TAF" not in content


async def test_a_list_with_no_tafs_says_so():
    cog, i = setup(["KBDU", "KLMO"])
    content, kwargs = await run_taf(cog, i)
    assert content.startswith("None of these airports has a TAF: KBDU, KLMO.")
    assert "files" not in kwargs


async def test_metar_shows_every_home_airport_in_images_of_four():
    ten = ["KBJC", "KAPA", "KDEN", "KLXV", "KASE", "KFNL", "KGNB", "KSBS", "K20V", "KEGE"]
    cog, i = setup(ten)
    await cog.metar.callback(cog, i, airports=None)
    sent = [(content, kw) for kind, content, kw in i.record if kind == "followup"]
    files = sent[0][1]["files"]
    assert [f.filename for f in files] == ["metar-1.png", "metar-2.png", "metar-3.png"]
    # Each image's alt text covers only its own cards.
    assert files[0].description.count(" | ") == 3 and files[2].description.count(" | ") == 1
    text = "".join(content for content, _ in sent)
    assert all(f"METAR {a}" in text or f"SPECI {a}" in text for a in ten)
    assert all(len(content) <= wx_cog.CONTENT_LIMIT for content, _ in sent)


def test_raw_blocks_split_on_whole_lines_and_keep_order():
    lines = [f"TAF K{n:03d} " + "X" * 390 for n in range(12)]
    blocks = wx_cog.raw_blocks(lines)
    assert len(blocks) > 1
    assert all(len(b) <= wx_cog.CONTENT_LIMIT and b.startswith("```\n") and b.endswith("\n```") for b in blocks)
    assert [ln for b in blocks for ln in b[4:-4].split("\n")] == lines
    assert wx_cog.raw_blocks(["A" * 5000])[0].endswith("…\n```")  # an absurd line is cut, not dropped
