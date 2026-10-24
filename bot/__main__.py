"""Entrypoint: python -m bot"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from .config import Config, load_env_file
from .sources.awc import Awc
from .sources.http import Http
from .sources.nws import Nws
from .store import Store
from .tree import AvwxTree

log = logging.getLogger("avwx")

EXTENSIONS = (
    "bot.cogs.wx",
    "bot.cogs.admin",
    "bot.cogs.brief",
)


async def publish_commands(tree, store: Store, dev_guild_id: int | None) -> None:
    """Publish the command tree, in exactly one scope.

    Discord *merges* a guild's own commands with the global set, so a tree
    registered in both scopes shows every command **twice** in the picker.
    Switching between dev and production therefore has to retract the scope it
    is leaving, not just publish the one it is entering — and by the time you
    leave dev mode, DEV_GUILD_ID is gone from the environment, so the guild it
    pointed at is remembered in the database instead.
    """
    for guild_id in store.dev_synced_guilds():
        if guild_id == dev_guild_id:
            continue  # still the dev guild; nothing to retract
        stale = discord.Object(id=guild_id)
        tree.clear_commands(guild=stale)
        await tree.sync(guild=stale)
        log.info("removed the guild-scoped commands from guild %s", guild_id)

    if dev_guild_id:
        # Guild-scoped sync applies instantly, which makes iterating on command
        # definitions tolerable. Global sync can take an hour.
        guild = discord.Object(id=dev_guild_id)
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        store.set_dev_synced_guilds([dev_guild_id])
        # Copy first, then retract the global set: clear_commands drops the
        # commands from the local tree, so the other order would leave nothing
        # to copy.
        tree.clear_commands(guild=None)
        await tree.sync()
        log.info(
            "synced %d commands to dev guild %s (global set retracted)",
            len(synced),
            dev_guild_id,
        )
    else:
        synced = await tree.sync()
        store.set_dev_synced_guilds([])
        log.info("synced %d commands globally", len(synced))


class AvwxBot(commands.Bot):
    def __init__(self, config: Config, store: Store) -> None:
        # Slash commands only, so no message content intent is needed — the bot
        # never reads message text.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            tree_cls=AvwxTree,
            # Alerts may ping a role; nothing the bot posts should ever ping
            # @everyone or a user by accident.
            allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
        )
        self.config = config
        self.store = store
        self.http_client = Http(config.user_agent)
        self.awc = Awc(self.http_client)
        self.nws = Nws(self.http_client)

    async def setup_hook(self) -> None:
        for name in EXTENSIONS:
            await self.load_extension(name)
        await publish_commands(self.tree, self.store, self.config.dev_guild_id)

    async def on_ready(self) -> None:
        log.info("logged in as %s in %d guild(s)", self.user, len(self.guilds))

    async def close(self) -> None:
        await super().close()
        await self.http_client.close()
        self.store.close()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    load_env_file()
    config = Config()
    token = config.require_token()
    store = Store(config.db_path)
    log.info("database at %s", config.db_path)

    bot = AvwxBot(config, store)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
