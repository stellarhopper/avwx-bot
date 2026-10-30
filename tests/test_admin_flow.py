import json
import types

import discord
import pytest

from bot import guildcfg, perms
from bot.cogs import admin
from bot.sources.awc import Station
from bot.store import Store
from fakes import FakeAttachment, FakeChannel, FakeGuild, FakeInteraction, perms as chan_perms, role

KNOWN = {"KBJC", "KAPA", "KDEN", "KASE"}


class FakeAwc:
    async def stations(self, ids):
        return {i: Station(i, i, 39.9, -105.1, 5500, "CO", "US") for i in ids if i in KNOWN}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(perms, "is_admin", lambda store, interaction: True)
    wx = FakeChannel("weather")
    forum = FakeChannel("briefs", kind=discord.ChannelType.forum, tags=("Daily brief", "Alert"))
    muted = FakeChannel("announcements", permissions=chan_perms(send_messages=False))
    pilots = role("Pilots")
    guild = FakeGuild([wx, forum, muted], [pilots])
    bot = types.SimpleNamespace(
        store=Store(":memory:"),
        awc=FakeAwc(),
        config=types.SimpleNamespace(site_url="https://example.test/avwx/", chat_guild_ids=frozenset()),
        claude=None,  # this host isn't set up for chat
    )
    cog = admin.Admin(bot)
    return types.SimpleNamespace(bot=bot, cog=cog, guild=guild, wx=wx, forum=forum, muted=muted, role=pilots)


def doc_for(s, **sections):
    return {
        "schema_version": 1,
        "home_airports": ["KBJC", "KAPA"],
        "brief": {"enabled": True, "channel_id": str(s.forum.id), "forum_tag_id": str(s.forum.available_tags[0].id)},
        "alerts": {"enabled": True, "channel_id": str(s.wx.id), "mention_role_id": str(s.role.id)},
        **sections,
    }


async def run_import(s, doc):
    i = FakeInteraction(s.guild)
    await s.cog.config_import.callback(s.cog, i, FakeAttachment(json.dumps(doc).encode()))
    return i


async def test_import_shows_diff_and_applies_on_confirm(setup):
    s = setup
    i = await run_import(s, doc_for(s))
    assert "Import" in i.reply and "brief.enabled" in i.reply
    view = i.last["view"]
    assert s.bot.store.get_config(s.guild.id) is None  # nothing until Apply

    click = FakeInteraction(s.guild)
    await view.apply.callback(click)
    assert click.reply == "✅ Applied."
    stored = s.bot.store.get_config(s.guild.id)
    assert stored["brief"]["channel_id"] == str(s.forum.id)


async def test_only_the_requester_can_apply(setup):
    s = setup
    i = await run_import(s, doc_for(s))
    view = i.last["view"]
    other = FakeInteraction(s.guild, user_id=12345)
    assert await view.interaction_check(other) is False


async def test_stale_confirmation_is_refused(setup):
    s = setup
    i = await run_import(s, doc_for(s))
    view = i.last["view"]
    s.bot.store.set_config(s.guild.id, {"schema_version": 1, "timezone": "UTC"}, actor_id=1)
    click = FakeInteraction(s.guild)
    await view.apply.callback(click)
    assert "changed while this was waiting" in click.reply
    assert s.bot.store.get_config(s.guild.id)["timezone"] == "UTC"


async def test_import_rejects_unknown_station_and_unpostable_channel(setup):
    s = setup
    doc = doc_for(s, home_airports=["KBJC", "KXYZ"])
    doc["alerts"]["channel_id"] = str(s.muted.id)
    i = await run_import(s, doc)
    assert "can't apply" in i.reply
    assert "KXYZ" in i.reply and "#announcements" in i.reply
    assert "view" not in i.last


async def test_import_rejects_schema_errors_by_name(setup):
    s = setup
    i = await run_import(s, {"schema_version": 1, "brief": {"time": "25:00"}})
    assert "brief.time" in i.reply


async def test_identical_import_is_a_no_op(setup):
    s = setup
    s.bot.store.set_config(s.guild.id, doc_for(s), actor_id=1)
    i = await run_import(s, doc_for(s))
    assert "nothing to do" in i.reply


async def test_edit_link_carries_config_and_channels(setup):
    s = setup
    i = FakeInteraction(s.guild)
    await s.cog.config_edit.callback(s.cog, i)
    link = i.reply.split("(<", 1)[1].split(">)", 1)[0]
    assert link.startswith("https://example.test/avwx/#c=")
    doc = guildcfg.decode_fragment(link.split("#c=", 1)[1])
    hints = doc["_guild"]
    assert {c["name"]: c["type"] for c in hints["channels"]} == {
        "weather": "text", "briefs": "forum", "announcements": "text"
    }
    forum = next(c for c in hints["channels"] if c["type"] == "forum")
    assert [t["name"] for t in forum["tags"]] == ["Daily brief", "Alert"]
    assert next(c for c in hints["channels"] if c["name"] == "announcements")["ok"] is False
    assert hints["roles"] == [{"id": str(s.role.id), "name": "Pilots"}]
    # The whole effective config, so the site shows every setting.
    assert doc["brief"]["time"] == "06:30"


async def test_edit_falls_back_to_a_file_for_huge_servers(setup):
    s = setup
    s.guild.channels.extend(FakeChannel(f"channel-{n}-with-a-long-descriptive-name") for n in range(300))
    i = FakeInteraction(s.guild)
    await s.cog.config_edit.callback(s.cog, i)
    assert "too many channels" in i.reply
    assert i.last["file"].filename == "config.json"


async def test_switch_on_goes_through_the_same_checks(setup):
    s = setup
    i = FakeInteraction(s.guild)
    await s.cog._switch(i, "morning-brief", True)
    assert "no channel" in i.reply  # can't turn on a brief with nowhere to post it


async def test_turning_chat_on_where_the_host_does_not_allow_it_warns(setup):
    s = setup
    s.bot.store.set_config(s.guild.id, doc_for(s, chat={"channel_ids": [str(s.forum.id)]}), actor_id=1)
    i = FakeInteraction(s.guild)
    await s.cog._switch(i, "chat", True)
    assert "CHAT_GUILD_IDS" in i.reply
    assert i.last.get("view") is not None  # a warning, not a refusal: Apply is still offered

    s.bot.claude = object()  # now the host allows it
    s.bot.config.chat_guild_ids = frozenset({s.guild.id})
    i = FakeInteraction(s.guild)
    await s.cog._switch(i, "chat", True)
    assert "CHAT_GUILD_IDS" not in i.reply


async def test_rollback_offers_the_previous_version(setup):
    s = setup
    s.bot.store.set_config(s.guild.id, {"schema_version": 1, "timezone": "America/Denver"}, actor_id=1)
    s.bot.store.set_config(s.guild.id, {"schema_version": 1, "timezone": "UTC"}, actor_id=1)
    i = FakeInteraction(s.guild)
    await s.cog.config_rollback.callback(s.cog, i)
    assert "Roll back" in i.reply and "America/Denver" in i.reply


def test_summary_mentions_unconfigured():
    assert "Not configured yet" in admin.summary(None)


def test_describe_changes_truncates():
    changes = [(f"k{n}", n, n + 1) for n in range(200)]
    text = admin.describe_changes(changes, limit=300)
    assert len(text) < 400 and "more" in text
