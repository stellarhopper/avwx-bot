"""The club chat in Discord: who's listened to, where, and how replies go out.

A conversation starts when a member @mentions the bot in one of the server's
chat channels. In a text channel the bot opens a thread on that message; in a
thread that already exists (the morning brief's forum post, say) it answers
in place. From then on it answers every message in the thread, mention or
not, until the thread has been quiet for `chat.idle_hours`. After that a new
@mention picks it up again.

The gates, in order, before a message is read at all: not from a bot; in a
server the host allows (CHAT_GUILD_IDS) that has chat switched on; in one of
its chat channels or a thread of one. Then, before anything is spent: the
member's daily request limit and the server's daily spend cap.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from .. import clock, context
from ..chat import gate, session, tools
from ..store import now as store_now

log = logging.getLogger(__name__)

THREAD_TYPES = {discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread}
TEXT_TYPES = {discord.ChannelType.text, discord.ChannelType.news}
MESSAGE_LIMIT = 1900  # under Discord's 2000, leaving room for the odd addition
MEMBER_TEXT_LIMIT = 4000
THREAD_NAME_LIMIT = 90
KEEP_CLOSED_DAYS = 30
# A mention with nothing else to it ("@avwx-bot", "@avwx-bot ^", "@avwx-bot
# this?") points at something said before the bot was listening. Up to this
# many earlier messages are passed along, from no further back than LOOKBACK.
EARLIER_MAX = 3
LOOKBACK = timedelta(hours=12)
EARLIER_SCAN = 15  # messages searched to find them, skipping the bot's own
REPLIED_CLIP = 1500  # characters of a replied-to message passed along
_BARE = re.compile(r"^[\s^?!.,:;'\"…↑⬆️👆]*(?:(?:this|that|above|here|thoughts|pls|please|see above)\b[\s^?!.,:;'\"…↑⬆️👆]*)?$", re.I)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Chunks under `limit`, broken at paragraphs, then lines, then words."""
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = ""
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            if cut <= 0:
                cut = para.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(para[:cut].rstrip())
            para = para[cut:].lstrip()
        current = para
    if current:
        chunks.append(current)
    return chunks


def member_text(message, bot_user) -> str:
    """What the member wrote, with the bot's own mention removed and other
    mentions shown as names."""
    text = getattr(message, "clean_content", None) or message.content or ""
    if bot_user is not None:
        for name in {getattr(bot_user, "display_name", None), getattr(bot_user, "name", None)}:
            if name:
                text = re.sub(rf"@{re.escape(name)}\b", "", text)
    if getattr(message, "attachments", None):
        text += " [attachment not shown]"
    return text.strip()[:MEMBER_TEXT_LIMIT]


def is_bare(text: str) -> bool:
    """A mention with nothing of its own to answer: just the mention, "^",
    "?", "this ^" and the like."""
    return bool(_BARE.match(text))


def thread_name(text: str) -> str:
    # Code fences (the raw METARs under a card) would show as backticks.
    first = " ".join(text.replace("```", " ").split())[:THREAD_NAME_LIMIT] or "Flight planning"
    return first


class Chat(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}
        self.pending: dict[int, list] = {}
        self.earlier: dict[int, list] = {}  # thread → messages a bare mention points back at
        self.replied: dict[int, list] = {}  # thread → bot messages members replied to
        self.addressed: dict[int, set] = {}  # thread → ids of messages that replied to the bot

    async def cog_load(self) -> None:
        self.janitor.start()

    async def cog_unload(self) -> None:
        self.janitor.cancel()

    @tasks.loop(hours=6)
    async def janitor(self) -> None:
        dropped = self.bot.store.prune_chat(store_now() - KEEP_CLOSED_DAYS * 86400)
        if dropped:
            log.info("forgot %d closed chat threads", dropped)

    # -------------------------------------------------------------- gates

    def chat_config(self, guild_id: int) -> dict | None:
        if self.bot.claude is None or guild_id not in self.bot.config.chat_guild_ids:
            return None
        cfg = context.guild_context(self.bot.store, guild_id).config
        return cfg if cfg["chat"].get("enabled") else None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id or message.guild is None:
            return
        cfg = self.chat_config(message.guild.id)
        if cfg is None:
            return
        channel = message.channel
        in_thread = channel.type in THREAD_TYPES
        home = channel.parent_id if in_thread else channel.id
        if home not in {int(c) for c in cfg["chat"].get("channel_ids", [])}:
            return
        mentioned = self.bot.user is not None and any(u.id == self.bot.user.id for u in message.mentions)
        store, guild_id = self.bot.store, message.guild.id
        # A Discord reply to one of the bot's messages is addressed to it,
        # whether or not the reply pings it, and brings that message along.
        replied = await self.replied_to_bot(message)
        addressed = mentioned or replied is not None
        bare = mentioned and replied is None and is_bare(member_text(message, self.bot.user))

        if not in_thread:
            if not addressed or channel.type not in TEXT_TYPES:
                return
            earlier = await self.earlier_messages(channel, message, since=None) if bare else []
            own = member_text(message, self.bot.user)
            if earlier:
                title = earlier[-1].text
            elif replied is not None and is_bare(own):
                title = replied.text
            else:
                title = own
            try:
                thread = await message.create_thread(name=thread_name(title), auto_archive_duration=1440)
            except discord.HTTPException as exc:
                log.warning("couldn't open a chat thread in %s: %s", channel.id, exc)
                return
            store.open_chat_thread(guild_id, thread.id, message.author.id, fresh=True)
            await self.enqueue(thread, message, cfg, earlier, replied)
            return

        record = store.get_chat_thread(guild_id, channel.id)
        idle = timedelta(hours=float(cfg["chat"]["idle_hours"]))
        is_open = (
            record is not None and record.closed_at is None
            and store_now() - record.last_active <= idle.total_seconds()
        )
        if record is not None and record.closed_at is None and not is_open:
            store.close_chat_thread(guild_id, channel.id, "idle")
        earlier = []
        if not is_open:
            if not addressed:
                return
            # Anything said since the bot stopped listening is new to it.
            if bare:
                earlier = await self.earlier_messages(channel, message, since=record.last_active if record else None)
            fresh = record is not None and record.close_reason == "long"
            store.open_chat_thread(guild_id, channel.id, message.author.id, fresh=fresh)
        await self.enqueue(channel, message, cfg, earlier, replied)

    async def replied_to_bot(self, message) -> session.MemberMessage | None:
        """The bot message this one replies to, as text for the model, or
        None if it isn't a reply to the bot. Images count by their alt text:
        many of the bot's posts are cards."""
        ref = getattr(message, "reference", None)
        if ref is None or ref.message_id is None or self.bot.user is None:
            return None
        target = getattr(ref, "resolved", None)
        if target is None or isinstance(target, discord.DeletedReferencedMessage):
            if ref.channel_id not in (None, message.channel.id):
                return None
            try:
                target = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return None
        author = getattr(target, "author", None)
        if author is None or author.id != self.bot.user.id:
            return None
        parts = [target.content or ""]
        parts += [f"[image: {a.description}]" for a in getattr(target, "attachments", []) if getattr(a, "description", None)]
        parts += [e.description for e in getattr(target, "embeds", []) if getattr(e, "description", None)]
        text = "\n".join(p for p in parts if p).strip()[:REPLIED_CLIP]
        sent = target.created_at if isinstance(getattr(target, "created_at", None), datetime) else clock.now()
        return session.MemberMessage(getattr(author, "display_name", None) or author.name, text, sent)

    async def earlier_messages(self, channel, message, *, since: int | None) -> list[session.MemberMessage]:
        """Members' messages just before `message` that the bot didn't see:
        newer than `since` (an epoch time) and LOOKBACK, oldest first."""
        floor = clock.now() - LOOKBACK
        out = []
        try:
            async for m in channel.history(limit=EARLIER_SCAN, before=message):
                if m.author.bot or m.webhook_id:
                    continue
                if m.created_at < floor or (since is not None and m.created_at.timestamp() <= since):
                    break
                text = member_text(m, self.bot.user)
                if text:
                    out.append(self._member(m))
                if len(out) >= EARLIER_MAX:
                    break
        except discord.HTTPException as exc:
            log.warning("couldn't read earlier messages in %s: %s", channel.id, exc)
        return out[::-1]

    def _member(self, m) -> session.MemberMessage:
        return session.MemberMessage(
            getattr(m.author, "display_name", None) or m.author.name,
            member_text(m, self.bot.user),
            m.created_at if isinstance(m.created_at, datetime) else clock.now(),
        )

    # ------------------------------------------------------------- turns

    async def enqueue(self, thread, message, cfg: dict, earlier: list | None = None, replied=None) -> None:
        """Answer now, or, if a turn is already running in this thread, add
        the message to the next one. Members often send two or three messages
        in a row; they're answered together."""
        self.pending.setdefault(thread.id, []).append(message)
        if earlier:
            self.earlier.setdefault(thread.id, []).extend(earlier)
        if replied is not None:
            self.replied.setdefault(thread.id, []).append(replied)
            self.addressed.setdefault(thread.id, set()).add(message.id)
        lock = self.locks.setdefault(thread.id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            while self.pending.get(thread.id):
                batch = self.pending.pop(thread.id)
                try:
                    await self.turn(thread, batch, cfg)
                except Exception:  # noqa: BLE001 - one bad turn mustn't wedge the thread
                    log.exception("chat turn failed in thread %s", thread.id)
                    await self.send(thread, session.SORRY)

    def _over_limits(self, guild_id: int, user_id: int, cfg: dict, day: str) -> str | None:
        chat = cfg["chat"]
        spent = self.bot.store.chat_usage(guild_id, day)
        if spent.cost_usd >= float(chat["daily_usd_cap"]):
            return "The club has used today's chat budget, so I'll be back tomorrow. (An admin can raise the cap.)"
        mine = self.bot.store.chat_usage(guild_id, day, user_id=user_id)
        if mine.requests >= int(chat["daily_requests_per_user"]):
            return "You've reached today's limit of questions for me. I'll be back tomorrow."
        return None

    async def turn(self, thread, batch: list, cfg: dict) -> None:
        guild_id = thread.guild.id
        tz = context.guild_context(self.bot.store, guild_id).tz
        day = clock.now().astimezone(tz).date().isoformat()
        asker = batch[-1].author.id
        members = [self._member(m) for m in batch]
        earlier = self.earlier.pop(thread.id, [])
        replied = self.replied.pop(thread.id, [])
        replies_to_bot = self.addressed.pop(thread.id, set())
        convo = session.Conversation(
            self.bot.claude, self.bot.store, guild_id, thread.id, effort=self.bot.config.chat_effort
        )
        history = convo.history()

        # Messages that don't mention the bot go past a cheap check first, so
        # chatter costs no Opus call and shows no typing indicator. A mention
        # always gets an answer.
        mentioned = self.bot.user is not None and any(
            u.id == self.bot.user.id for m in batch for u in m.mentions
        )
        if not mentioned and not replies_to_bot and history:
            reply_wanted, cost = await gate.should_reply(self.bot.claude, history, session.wrap(members, tz))
            self.bot.store.add_chat_usage(guild_id, day, asker, cost)
            if not reply_wanted:
                convo.note(members, tz)
                self.bot.store.touch_chat_thread(guild_id, thread.id)
                return

        # Limits are checked only once the bot means to answer, so chatter
        # never draws a "you've reached today's limit" reply.
        refusal = self._over_limits(guild_id, asker, cfg, day)
        if refusal:
            await self.send(thread, refusal)
            return

        ctx = tools.ToolContext(self.bot.awc, self.bot.nws, self.bot.fleet, cfg, clock.now)
        thread_context = None if history else self.describe_thread(thread)
        async with thread.typing():
            reply = await convo.respond(members, ctx, context=thread_context, earlier=earlier, replied_to=replied)

        self.bot.store.add_chat_usage(guild_id, day, asker, reply.usage)
        self.bot.store.touch_chat_thread(guild_id, thread.id)
        files = [
            discord.File(io.BytesIO(a.data), filename=a.filename, description=a.alt[:1024])
            for a in reply.attachments
        ]
        if reply.text or files:
            await self.send(thread, reply.text or "", files=files)
        if reply.close_reason == "long":
            self.bot.store.close_chat_thread(guild_id, thread.id, "long")
            await self.send(
                thread,
                "This conversation is getting long, so I'll stop listening here. "
                "@mention me to start fresh.",
            )

    def describe_thread(self, thread) -> str:
        """What the model should know about where it is."""
        parts = [f"Discord thread: {getattr(thread, 'name', '')}"]
        post = self.bot.store.get_post_context(thread.guild.id, thread.id)
        if post is not None:
            parts.append(f"This thread is the bot's {post.kind} post for {', '.join(post.airports)}.")
        return " ".join(parts)

    async def send(self, thread, text: str, *, files: list | None = None) -> None:
        chunks = split_message(text) if text else [""]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            try:
                await thread.send(
                    chunk or None,
                    files=files if last and files else discord.utils.MISSING,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                log.warning("couldn't post a chat reply in %s: %s", thread.id, exc)
                return


async def setup(bot) -> None:
    await bot.add_cog(Chat(bot))
