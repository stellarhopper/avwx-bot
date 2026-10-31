"""Just enough of discord.py's objects to drive command callbacks in tests."""

from __future__ import annotations

import contextlib
import itertools
import types

import discord

GUILD = 111111111111111111
ALICE = 222222222222222222
BOB = 333333333333333333
BOT_USER = 999999999999999999

_ids = itertools.count(500000000000000000)


def perms(**overrides) -> types.SimpleNamespace:
    base = dict(
        view_channel=True,
        send_messages=True,
        attach_files=True,
        send_messages_in_threads=True,
        manage_threads=False,
        embed_links=True,
        create_public_threads=True,
        read_message_history=True,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class FakeMessage:
    def __init__(self, channel, content=None, file=None, files=None, view=None, embed=None) -> None:
        self.id = next(_ids)
        self.channel = channel
        self.content = content
        self.file = file
        self.files = files or ([file] if file else [])
        self.view = view
        self.embed = embed
        self.edits: list[dict] = []

    async def edit(self, **kwargs) -> "FakeMessage":
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]
        return self


class FakeThread:
    type = discord.ChannelType.public_thread

    def __init__(self, parent, name: str, applied_tags=()) -> None:
        self.id = next(_ids)
        self.parent = parent
        self.parent_id = parent.id
        self.name = name
        self.applied_tags = list(applied_tags)
        self.sent: list[FakeMessage] = []
        self.archived = False
        self.locked = False
        self.guild = getattr(parent, "guild", types.SimpleNamespace(id=GUILD))
        self.posted: list = []  # members' messages, oldest first, for history()
        self.typing_count = 0

    def typing(self):
        self.typing_count += 1
        return contextlib.nullcontext()

    async def history(self, *, limit=100, before=None):
        msgs = self.posted if before is None else self.posted[: self.posted.index(before)]
        for m in reversed(msgs[-limit:]):
            yield m

    async def send(self, content=None, **kwargs) -> FakeMessage:
        msg = FakeMessage(self, content, kwargs.get("file"), kwargs.get("files"), kwargs.get("view"), kwargs.get("embed"))
        self.sent.append(msg)
        return msg

    async def edit(self, **kwargs) -> "FakeThread":
        self.archived = kwargs.get("archived", self.archived)
        self.locked = kwargs.get("locked", self.locked)
        self.name = kwargs.get("name", self.name)
        return self

    def get_partial_message(self, message_id):
        return next((m for m in self.sent if m.id == message_id), None)

    async def fetch_message(self, message_id):
        msg = self.get_partial_message(message_id)
        if msg is None:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "unknown message")
        return msg


class FakeChannel:
    def __init__(self, name: str, *, kind=discord.ChannelType.text, permissions=None, category=None, tags=()) -> None:
        self.id = next(_ids)
        self.name = name
        self.type = kind
        self.position = 0
        self.category = category
        self.available_tags = [types.SimpleNamespace(id=next(_ids), name=t) for t in tags]
        self._perms = permissions or perms()
        self.sent: list[FakeMessage] = []
        self.threads: list[FakeThread] = []
        self.guild = types.SimpleNamespace(id=GUILD)
        self.posted: list = []  # members' messages, oldest first, for history()

    async def history(self, *, limit=100, before=None):
        msgs = self.posted if before is None else self.posted[: self.posted.index(before)]
        for m in reversed(msgs[-limit:]):
            yield m

    def permissions_for(self, member):
        return self._perms

    def get_tag(self, tag_id):
        return next((t for t in self.available_tags if t.id == tag_id), None)

    async def send(self, content=None, **kwargs) -> FakeMessage:
        msg = FakeMessage(self, content, kwargs.get("file"), kwargs.get("files"), kwargs.get("view"), kwargs.get("embed"))
        self.sent.append(msg)
        return msg

    async def create_thread(self, *, name, content=None, file=None, files=None, applied_tags=(), **kwargs):
        """ForumChannel.create_thread: returns (thread, message)."""
        thread = FakeThread(self, name, applied_tags)
        message = await thread.send(content, file=file, files=files, view=kwargs.get("view"))
        self.threads.append(thread)
        return types.SimpleNamespace(thread=thread, message=message)

    def get_partial_message(self, message_id):
        return next((m for m in self.sent if m.id == message_id), None)

    async def fetch_message(self, message_id):
        msg = self.get_partial_message(message_id)
        if msg is None:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "unknown message")
        return msg


class FakeGuild:
    def __init__(self, channels=(), roles=()) -> None:
        self.id = GUILD
        self.name = "Front Range Pilots"
        self.channels = list(channels)
        self.roles = list(roles)
        self.me = types.SimpleNamespace(id=BOT_USER)
        self.threads: list[FakeThread] = []

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def get_channel_or_thread(self, cid):
        for c in self.channels:
            if c.id == cid:
                return c
            for t in getattr(c, "threads", []):
                if t.id == cid:
                    return t
        return None

    def get_thread(self, tid):
        for c in self.channels:
            for t in getattr(c, "threads", []):
                if t.id == tid:
                    return t
        return None

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)


def role(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=next(_ids), name=name, position=1, is_default=lambda: False, managed=False)


class FakeAttachment:
    def __init__(self, data: bytes, filename: str = "config.json") -> None:
        self.data = data
        self.filename = filename
        self.size = len(data)

    async def read(self) -> bytes:
        return self.data


class FakeResponse:
    def __init__(self, record: list) -> None:
        self._record = record
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs) -> None:
        self._done = True

    async def send_message(self, content=None, **kwargs) -> None:
        self._done = True
        self._record.append(("response", content, kwargs))

    async def edit_message(self, **kwargs) -> None:
        self._done = True
        self._record.append(("edit", kwargs.get("content"), kwargs))


class FakeInteraction:
    def __init__(self, guild: FakeGuild, *, user_id: int = ALICE, channel=None) -> None:
        self.guild = guild
        self.guild_id = guild.id
        self.user = types.SimpleNamespace(id=user_id, roles=[], guild_permissions=types.SimpleNamespace(manage_guild=True))
        self.channel = channel
        self.channel_id = channel.id if channel else None
        self.record: list = []
        self.response = FakeResponse(self.record)
        self.followup = types.SimpleNamespace(send=self._followup)

    async def _followup(self, content=None, **kwargs):
        self.record.append(("followup", content, kwargs))
        return FakeMessage(self.channel, content, kwargs.get("file"))

    @property
    def reply(self) -> str:
        for _kind, content, _kw in reversed(self.record):
            if content is not None:
                return content
        raise AssertionError(f"no reply in {self.record}")

    @property
    def last(self) -> dict:
        return self.record[-1][2]


class FakeChatMessage:
    """A member's message, as the chat cog's on_message sees it."""

    def __init__(self, channel, content: str, *, author_id: int = ALICE, name: str = "Alice",
                 mentions_bot: bool = False, bot: bool = False, minutes_ago: float = 0) -> None:
        self.id = next(_ids)
        self.channel = channel
        self.guild = types.SimpleNamespace(id=GUILD)
        self.author = types.SimpleNamespace(id=author_id, name=name.lower(), display_name=name, bot=bot)
        self.content = ("<@999999999999999999> " if mentions_bot else "") + content
        self.clean_content = ("@avwx-bot " if mentions_bot else "") + content
        self.mentions = [types.SimpleNamespace(id=BOT_USER)] if mentions_bot else []
        self.webhook_id = None
        self.attachments = []
        from datetime import datetime, timedelta, timezone

        self.created_at = datetime(2026, 10, 24, 6, 40, tzinfo=timezone.utc) - timedelta(minutes=minutes_ago)
        if hasattr(channel, "posted"):
            channel.posted.append(self)

    async def create_thread(self, *, name, auto_archive_duration=None):
        thread = FakeThread(self.channel, name)
        self.channel.threads.append(thread)
        return thread
