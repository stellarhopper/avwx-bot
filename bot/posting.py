"""Posting automatic content to a text channel or a forum.

An admin can point the brief or the alerts at either. In a text channel, each
one is an ordinary message. In a forum, each one becomes its own post (a
thread), with an optional tag. People can then run commands inside that thread
to dig deeper, and a bare `/metar` there picks up the post's airports from
`post_context`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord

from .store import Store

log = logging.getLogger(__name__)

# Discord's limit for a thread (forum post) title.
TITLE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class Posted:
    channel_id: int
    message_id: int
    thread_id: int | None  # set when the post is a forum thread


class PostError(Exception):
    """Couldn't post. The message says why, for logs and admin-facing replies."""


def resolve_channel(guild: discord.Guild, channel_id: int):
    channel = guild.get_channel(channel_id)
    if channel is None:
        raise PostError(f"channel {channel_id} no longer exists or isn't visible to me")
    return channel


def is_forum(channel) -> bool:
    return getattr(channel, "type", None) == discord.ChannelType.forum


async def post(
    store: Store,
    guild: discord.Guild,
    channel_id: int,
    *,
    title: str,
    content: str,
    file: discord.File | None = None,
    tag_id: int | None = None,
    kind: str,
    airports: list[str],
    allowed_mentions: discord.AllowedMentions | None = None,
) -> Posted:
    channel = resolve_channel(guild, channel_id)
    try:
        if is_forum(channel):
            tags = []
            if tag_id:
                tag = channel.get_tag(tag_id)
                if tag is not None:
                    tags.append(tag)
            kwargs = {"name": title[:TITLE_LIMIT], "content": content, "applied_tags": tags}
            if file is not None:
                kwargs["file"] = file
            if allowed_mentions is not None:
                kwargs["allowed_mentions"] = allowed_mentions
            created = await channel.create_thread(**kwargs)
            store.set_post_context(created.thread.id, guild.id, kind, airports)
            return Posted(channel.id, created.message.id, created.thread.id)

        kwargs = {}
        if file is not None:
            kwargs["file"] = file
        if allowed_mentions is not None:
            kwargs["allowed_mentions"] = allowed_mentions
        message = await channel.send(content, **kwargs)
        return Posted(channel.id, message.id, None)
    except discord.Forbidden as exc:
        raise PostError(f"missing permission to post in #{channel.name}") from exc
    except discord.HTTPException as exc:
        raise PostError(f"Discord refused the post in #{channel.name}: {exc}") from exc


async def find_message(guild: discord.Guild, posted_channel_id: int, message_id: int, thread_id: int | None):
    """The message a previous post() made, or None if it's gone."""
    target = None
    if thread_id:
        target = guild.get_thread(thread_id)
        if target is None:
            try:
                target = await guild.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden, AttributeError):
                return None
    else:
        target = guild.get_channel(posted_channel_id)
    if target is None:
        return None
    try:
        return await target.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden):
        return None
    except discord.HTTPException as exc:
        log.warning("couldn't fetch message %s: %s", message_id, exc)
        return None


async def reply_in_thread(guild: discord.Guild, thread_id: int, content: str, **kwargs) -> bool:
    thread = guild.get_thread(thread_id)
    if thread is None:
        return False
    try:
        if getattr(thread, "archived", False):
            await thread.edit(archived=False)
        await thread.send(content, **kwargs)
        return True
    except discord.HTTPException as exc:
        log.warning("couldn't reply in thread %s: %s", thread_id, exc)
        return False
