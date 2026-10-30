"""Entrypoint: python -m bot"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from .config import Config, load_env_file
from .fleet import FleetCache
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
    "bot.cogs.alerts",
    "bot.cogs.mountain",
    "bot.cogs.chat",
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
        # Message content is a privileged intent, needed only for the club chat:
        # members reply in a chat thread without @mentioning the bot, and the
        # bot has to read those replies. It's asked for only when this host is
        # set up for chat, because Discord refuses the connection outright if
        # the intent is asked for but not switched on in the Developer Portal.
        # Nothing outside an open chat thread is read.
        chat_ready = bool(config.anthropic_api_key and config.chat_guild_ids)
        intents = discord.Intents.default()
        intents.message_content = chat_ready
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
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
        self.fleet = FleetCache(config.club_dir)
        self.claude = None
        if chat_ready:
            import anthropic

            self.claude = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key, timeout=180, max_retries=2)

    async def setup_hook(self) -> None:
        for name in EXTENSIONS:
            await self.load_extension(name)
        await publish_commands(self.tree, self.store, self.config.dev_guild_id)

    async def on_message(self, message: discord.Message) -> None:
        # The bot has no prefix commands, only slash commands. Bot's own
        # on_message would read "@avwx-bot I'm planning..." as a command named
        # "I'm" and log an error for every chat message. The chat cog's
        # listener still runs; listeners are dispatched separately.
        return

    async def on_ready(self) -> None:
        log.info("logged in as %s in %d guild(s)", self.user, len(self.guilds))

    async def close(self) -> None:
        await super().close()
        await self.http_client.close()
        if self.claude is not None:
            await self.claude.close()
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
