import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
import pytest

from bot import briefing, guildcfg
from bot.cogs import brief as brief_cog
from bot.cogs.wx import raw_block
from bot.sources.awc import Pirep
from bot.store import Store
from conftest import fixture_json
from fakeapi import clients
from fakes import FakeChannel, FakeGuild, FakeInteraction

NOW = datetime(2026, 10, 24, 6, 40, tzinfo=timezone.utc)


def cfg(**brief):
    return guildcfg.effective({"schema_version": 1, "home_airports": ["KBJC", "KAPA"], "brief": brief})


# ---------------------------------------------------------------- schedule


def at(local: str) -> datetime:
    """A UTC instant from a Denver wall-clock time on Sat 24 Oct 2026 (MDT, UTC-6)."""
    hh, mm = map(int, local.split(":"))
    return datetime(2026, 10, 24, hh + 6, mm, tzinfo=timezone.utc)


def test_due_at_and_shortly_after_the_time():
    c = cfg(enabled=True, channel_id="123456789012345678", time="06:30")
    assert brief_cog.due_day(at("06:29"), c) is None
    assert brief_cog.due_day(at("06:30"), c) == "2026-10-24"
    assert brief_cog.due_day(at("07:29"), c) == "2026-10-24"
    assert brief_cog.due_day(at("07:31"), c) is None  # too late to be news


def test_not_due_when_off_or_wrong_day_or_no_channel():
    assert brief_cog.due_day(at("06:45"), cfg(enabled=False, channel_id="123456789012345678")) is None
    assert brief_cog.due_day(at("06:45"), cfg(enabled=True)) is None
    weekdays = cfg(enabled=True, channel_id="123456789012345678", days=["mon", "tue", "wed", "thu", "fri"])
    assert brief_cog.due_day(at("06:45"), weekdays) is None  # the 24th is a Saturday


def test_due_uses_the_servers_time_zone():
    c = guildcfg.effective({"schema_version": 1, "timezone": "Europe/London",
                            "brief": {"enabled": True, "channel_id": "123456789012345678", "time": "07:00"}})
    # 07:10 BST is 06:10Z.
    assert brief_cog.due_day(datetime(2026, 10, 24, 6, 10, tzinfo=timezone.utc), c) == "2026-10-24"


# ---------------------------------------------------------------- gathering


async def test_gather_builds_every_section_from_fixtures():
    api = clients()
    data = await briefing.gather(
        api.awc, api.nws, title="T", airports=["KBJC", "KAPA", "KDEN"], config=cfg(),
        include=cfg()["brief"]["include"], now=NOW,
    )
    assert [a.station for a in data.airports] == ["KBJC", "KAPA", "KDEN"]
    assert data.airports[1].taf is not None
    assert data.winds.site == "DEN"
    assert data.afd.office == "BOU" and data.afd.aviation.startswith("Current radar")
    assert data.pireps and data.hazards is not None and data.alerts is not None
    assert data.problems == []


async def test_gather_survives_a_failing_source():
    api = clients(pirep=(503, "down"), gairmet=(500, "oops"))
    data = await briefing.gather(
        api.awc, api.nws, title="T", airports=["KBJC"], config=cfg(), include=cfg()["brief"]["include"], now=NOW,
    )
    assert data.pireps is None and data.hazards is None
    assert any("PIREPs" in p for p in data.problems) and any("AIRMETs" in p for p in data.problems)
    assert data.airports[0].metar is not None


async def test_gather_skips_us_only_sections_abroad():
    api = clients()
    data = await briefing.gather(
        api.awc, api.nws, title="T", airports=["EGLL"], config=cfg(), include=cfg()["brief"]["include"], now=NOW,
    )
    assert data.alerts is None and data.afd is None
    assert not any("/points/" in url for url, _ in api.http.requests)


def test_notable_pireps_rank_moderate_and_wave_first():
    reps = [Pirep.from_api(d) for d in fixture_json("pirep.json")]
    notable = briefing.notable_pireps(reps)
    assert notable and all("MOD" in p.turbulence or p.mentions_wave or p.icing for p in notable)
    assert not any(p.turbulence == "NEG" and not p.mentions_wave for p in notable)


# ---------------------------------------------------------------- posting


def make_bot():
    api = clients()
    return types.SimpleNamespace(store=Store(":memory:"), awc=api.awc, nws=api.nws, http_client=api.http, guilds=[])


async def test_brief_to_text_channel_is_one_message_with_raw_text():
    bot = make_bot()
    channel = FakeChannel("weather")
    guild = FakeGuild([channel])
    cog = brief_cog.Briefs(bot)
    c = cfg(enabled=True, channel_id=str(channel.id))
    posted = await cog.post_brief(guild, c, day="2026-10-24")
    assert posted.thread_id is None
    [msg] = channel.sent
    assert msg.content.startswith("**Morning brief")
    assert "```" in msg.content and "METAR KBJC" in msg.content
    assert msg.file.filename == "brief.png"
    assert bot.store.brief_sent(guild.id, "2026-10-24")


async def test_brief_to_forum_makes_a_tagged_post_with_details_in_the_thread():
    bot = make_bot()
    forum = FakeChannel("briefs", kind=discord.ChannelType.forum, tags=("Brief",))
    guild = FakeGuild([forum])
    cog = brief_cog.Briefs(bot)
    c = cfg(enabled=True, channel_id=str(forum.id), forum_tag_id=str(forum.available_tags[0].id))
    posted = await cog.post_brief(guild, c, day=None)
    [thread] = forum.threads
    assert posted.thread_id == thread.id
    assert thread.name.startswith("Morning brief — ")
    assert [t.name for t in thread.applied_tags] == ["Brief"]
    texts = [m.content for m in thread.sent]
    assert texts[0].startswith("**Morning brief") and "```" not in texts[0]
    assert any(t.startswith("**METARs**") for t in texts)
    assert any(t.startswith("**Forecast discussion") for t in texts)
    # A bare /metar in this thread now means the brief's airports.
    assert bot.store.get_post_context(guild.id, thread.id).airports == ["KBJC", "KAPA"]
    # day=None: a manual post doesn't use up the scheduled one.
    assert not bot.store.brief_sent(guild.id, "2026-10-24")


async def test_scheduler_posts_once_and_retries_failures_a_few_times(monkeypatch):
    bot = make_bot()
    channel = FakeChannel("weather")
    guild = FakeGuild([channel])
    bot.guilds = [guild]
    bot.store.set_config(guild.id, {"schema_version": 1, "home_airports": ["KBJC"],
                                    "brief": {"enabled": True, "channel_id": str(channel.id)}}, actor_id=None)
    cog = brief_cog.Briefs(bot)
    monkeypatch.setattr(brief_cog, "due_day", lambda now, c: "2026-10-24")
    await cog.scheduler.coro(cog)
    await cog.scheduler.coro(cog)
    assert len(channel.sent) == 1

    broken = FakeGuild([])  # channel gone
    broken.id += 1
    bot.guilds = [broken]
    bot.store.set_config(broken.id, {"schema_version": 1, "home_airports": ["KBJC"],
                                     "brief": {"enabled": True, "channel_id": "123456789012345678"}}, actor_id=None)
    for _ in range(5):
        await cog.scheduler.coro(cog)
    assert cog.attempts[(broken.id, "2026-10-24")] == brief_cog.MAX_ATTEMPTS


async def test_brief_command_uses_home_airports(monkeypatch):
    bot = make_bot()
    guild = FakeGuild([])
    bot.store.set_config(guild.id, {"schema_version": 1, "home_airports": ["KDEN"]}, actor_id=None)
    cog = brief_cog.Briefs(bot)
    i = FakeInteraction(guild)
    await cog.brief.callback(cog, i)
    assert "KDEN VFR" in i.reply
    assert i.last["file"].filename == "brief.png"


async def test_winds_command_by_airport_and_by_site():
    bot = make_bot()
    guild = FakeGuild([])
    cog = brief_cog.Briefs(bot)
    i = FakeInteraction(guild)
    await cog.winds.callback(cog, i, "KBJC")
    assert "**DEN**" in i.reply
    i = FakeInteraction(guild)
    await cog.winds.callback(cog, i, "gjt")
    assert "**GJT**" in i.reply


def test_pirep_message_fits_and_marks_notable():
    reps = [Pirep.from_api(d) for d in fixture_json("pirep.json")] * 20
    text = brief_cog.pirep_message("KDEN", 100, 3, reps, ZoneInfo("UTC"))
    assert len(text) <= 2000 and text.rstrip().endswith("```")
    assert "marked !" in text


@pytest.mark.parametrize("limit", [200, 2000])
def test_raw_block_never_breaks_its_fences(limit):
    block = raw_block(["METAR " + "X" * 70] * 100, limit=limit)
    assert len(block) <= limit and block.startswith("```\n") and block.endswith("\n```")
