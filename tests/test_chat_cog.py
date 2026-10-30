import asyncio
import types
from pathlib import Path

import discord

from bot.chat import prompt
from bot.cogs import chat as chat_cog
from bot.fleet import FleetCache
from bot.store import ChatUsage, Store
from fakeapi import clients
from fakeclaude import FakeClaude, message, text
from fakes import BOB, BOT_USER, GUILD, FakeChannel, FakeChatMessage, FakeThread

CLUB = Path(__file__).parent / "fixtures" / "club"


def setup(*script, enabled=True, allowed=True, channels=None, cfg_extra=None):
    store = Store(":memory:")
    channel = FakeChannel("flight-planning")
    forum = FakeChannel("briefs", kind=discord.ChannelType.forum)
    ids = channels if channels is not None else [str(channel.id), str(forum.id)]
    store.set_config(GUILD, {"schema_version": 1, "timezone": "America/Denver",
                             "chat": {"enabled": enabled, "channel_ids": ids, **(cfg_extra or {})}}, actor_id=None)
    c = clients()
    fake = FakeClaude(*script)
    bot = types.SimpleNamespace(
        store=store, awc=c.awc, nws=c.nws, fleet=FleetCache(CLUB), claude=fake,
        config=types.SimpleNamespace(chat_guild_ids=frozenset({GUILD} if allowed else ()), chat_effort="medium"),
        user=types.SimpleNamespace(id=BOT_USER, name="avwx-bot", display_name="avwx-bot"),
    )
    return chat_cog.Chat(bot), bot, channel, forum, fake


async def test_a_mention_opens_a_thread_and_answers_there():
    cog, bot, channel, _, fake = setup(message(text("Hello, pilot.")))
    await cog.on_message(FakeChatMessage(channel, "what's the weather at KBJC?", mentions_bot=True))
    (thread,) = channel.threads
    assert thread.name == "what's the weather at KBJC?"
    assert [m.content for m in thread.sent] == ["Hello, pilot."]
    assert channel.sent == []
    sent_user = fake.requests[0]["messages"][0]["content"][0]["text"]
    assert 'name="Alice"' in sent_user and "@avwx-bot" not in sent_user
    assert bot.store.chat_usage(GUILD, "2026-10-24").requests == 1
    assert bot.store.get_chat_thread(GUILD, thread.id).closed_at is None


async def test_replies_in_an_open_thread_need_no_mention():
    cog, bot, channel, _, fake = setup(message(text("First.")), message(text("Second.")))
    await cog.on_message(FakeChatMessage(channel, "hi", mentions_bot=True))
    thread = channel.threads[0]
    await cog.on_message(FakeChatMessage(thread, "and tomorrow?", author_id=BOB, name="Bob"))
    assert [m.content for m in thread.sent] == ["First.", "Second."]
    assert len(fake.requests[1]["messages"]) == 3


async def test_what_is_ignored():
    cog, bot, channel, _, fake = setup()
    other = FakeChannel("general")
    await cog.on_message(FakeChatMessage(channel, "no mention here"))
    await cog.on_message(FakeChatMessage(other, "hi", mentions_bot=True))
    await cog.on_message(FakeChatMessage(channel, "hi", mentions_bot=True, bot=True))
    loose = FakeThread(channel, "someone else's thread")
    await cog.on_message(FakeChatMessage(loose, "no mention, no session"))
    assert fake.requests == [] and channel.threads == []
    for kwargs in ({"enabled": False}, {"allowed": False}):
        cog, _, channel, _, fake = setup(**kwargs)
        await cog.on_message(FakeChatMessage(channel, "hi", mentions_bot=True))
        assert fake.requests == []


async def test_a_brief_forum_post_continues_in_place_with_its_airports():
    cog, bot, _, forum, fake = setup(message(text("Looks good.")))
    post = FakeThread(forum, "Morning brief")
    forum.threads.append(post)
    bot.store.set_post_context(post.id, GUILD, "brief", ["KBJC", "KAPA"])
    await cog.on_message(FakeChatMessage(post, "anything to watch today?", mentions_bot=True))
    assert [m.content for m in post.sent] == ["Looks good."]
    first = fake.requests[0]["messages"][0]["content"][0]["text"]
    assert "brief post for KBJC, KAPA" in first


async def test_an_idle_thread_needs_a_mention_and_keeps_its_history():
    cog, bot, channel, _, fake = setup(message(text("One.")), message(text("Two.")))
    await cog.on_message(FakeChatMessage(channel, "hi", mentions_bot=True))
    thread = channel.threads[0]
    with bot.store._conn:
        bot.store._conn.execute("UPDATE chat_threads SET last_active = 0")
    await cog.on_message(FakeChatMessage(thread, "still there?"))
    assert len(fake.requests) == 1
    assert bot.store.get_chat_thread(GUILD, thread.id).close_reason == "idle"
    await cog.on_message(FakeChatMessage(thread, "still there?", mentions_bot=True))
    assert len(fake.requests) == 2 and len(fake.requests[1]["messages"]) == 3


async def test_no_reply_sends_nothing():
    cog, _, channel, _, _ = setup(message(text(prompt.NO_REPLY)))
    await cog.on_message(FakeChatMessage(channel, "hi", mentions_bot=True))
    assert channel.threads[0].sent == []


async def test_daily_limits_stop_before_any_spend():
    cog, bot, channel, _, fake = setup(cfg_extra={"daily_requests_per_user": 1})
    bot.store.add_chat_usage(GUILD, "2026-10-24", 222222222222222222, ChatUsage(requests=1))
    await cog.on_message(FakeChatMessage(channel, "hi", mentions_bot=True))
    assert fake.requests == []
    assert "limit" in channel.threads[0].sent[0].content

    cog, bot, channel, _, fake = setup(cfg_extra={"daily_usd_cap": 1})
    bot.store.add_chat_usage(GUILD, "2026-10-24", BOB, ChatUsage(requests=1, cost_micro_usd=1_000_000))
    await cog.on_message(FakeChatMessage(channel, "hi", mentions_bot=True))
    assert fake.requests == [] and "budget" in channel.threads[0].sent[0].content


async def test_a_long_thread_closes_and_starts_fresh_when_mentioned():
    big = {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 200_000, "cache_creation_input_tokens": 0}
    cog, bot, channel, _, fake = setup(message(text("Long answer."), usage=big), message(text("Fresh.")))
    await cog.on_message(FakeChatMessage(channel, "hi", mentions_bot=True))
    thread = channel.threads[0]
    assert "getting long" in thread.sent[-1].content
    assert bot.store.get_chat_thread(GUILD, thread.id).close_reason == "long"
    await cog.on_message(FakeChatMessage(thread, "again", mentions_bot=True))
    assert len(fake.requests[1]["messages"]) == 1  # history cleared


async def test_messages_during_a_turn_are_answered_together_next():
    gate = asyncio.Event()

    class Slow(FakeClaude):
        async def _create(self, **kwargs):
            if not self.requests:
                self.requests.append(kwargs)
                await gate.wait()
                self.requests.pop()
            return await super()._create(**kwargs)

    cog, bot, channel, _, _ = setup()
    slow = Slow(message(text("First answer.")), message(text("Second answer.")))
    bot.claude = slow
    first = asyncio.create_task(cog.on_message(FakeChatMessage(channel, "hi", mentions_bot=True)))
    while not slow.requests:
        await asyncio.sleep(0)
    thread = channel.threads[0]
    await cog.on_message(FakeChatMessage(thread, "also this", name="Alice"))
    await cog.on_message(FakeChatMessage(thread, "and this", author_id=BOB, name="Bob"))
    gate.set()
    await first
    assert [m.content for m in thread.sent] == ["First answer.", "Second answer."]
    batch = slow.requests[-1]["messages"][-1]["content"][0]["text"]
    assert "also this" in batch and "and this" in batch


def test_split_message_keeps_paragraphs_and_limits():
    text_ = "\n\n".join(["a" * 1000, "b" * 1000, "c" * 50])
    chunks = chat_cog.split_message(text_)
    assert chunks == ["a" * 1000, "b" * 1000 + "\n\n" + "c" * 50]
    long_line = "word " * 1000
    assert all(len(c) <= chat_cog.MESSAGE_LIMIT for c in chat_cog.split_message(long_line))
