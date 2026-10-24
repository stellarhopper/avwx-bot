"""On-demand weather commands: /metar and /taf."""

from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import context
from ..render import cards, theme

log = logging.getLogger(__name__)

MAX_STATIONS = 4
# Discord rejects message content over 2000 characters.
CONTENT_LIMIT = 2000


def raw_block(lines: list[str]) -> str:
    """Raw reports in a code block, trimmed to fit a message."""
    body = "\n".join(lines)
    if len(body) > CONTENT_LIMIT - 10:
        body = body[: CONTENT_LIMIT - 14] + "\n…"
    return f"```\n{body}\n```"


def png_file(data: bytes, name: str, alt: str) -> discord.File:
    # Discord caps alt text at 1024 characters.
    return discord.File(io.BytesIO(data), filename=name, description=alt[:1024])


class Wx(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(name="metar", description="Current observations, as a card")
    @app_commands.describe(airports="Up to 4, e.g. KBJC KAPA. Leave empty for this server's or thread's airports.")
    async def metar(self, interaction: discord.Interaction, airports: str | None = None) -> None:
        ctx = context.guild_context(self.bot.store, interaction.guild_id)
        try:
            ids = context.resolve_stations(
                self.bot.store, interaction, airports, limit=MAX_STATIONS, fallback=ctx.home_airports
            )
        except context.StationInputError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        found = await self.bot.awc.metars(ids)
        missing = [i for i in ids if i not in found]
        metars = [found[i] for i in ids if i in found]
        if not metars:
            await interaction.followup.send(f"No current METAR for {', '.join(ids)}.")
            return

        def render() -> bytes:
            return theme.stack([cards.metar_card(m, units=ctx.units, tz=ctx.tz) for m in metars])

        image = await asyncio.to_thread(render)
        alt = " | ".join(cards.metar_alt(m, ctx.units) for m in metars)
        content = raw_block([m.raw for m in metars])
        if missing:
            content += f"\nNo current METAR for {', '.join(missing)}."
        await interaction.followup.send(content, file=png_file(image, "metar.png", alt))

    @app_commands.command(name="taf", description="Terminal forecast with a flight-category timeline")
    @app_commands.describe(airports="Up to 4, e.g. KDEN. Leave empty for this server's or thread's airports.")
    async def taf(self, interaction: discord.Interaction, airports: str | None = None) -> None:
        ctx = context.guild_context(self.bot.store, interaction.guild_id)
        try:
            ids = context.resolve_stations(
                self.bot.store, interaction, airports, limit=MAX_STATIONS, fallback=ctx.home_airports
            )
        except context.StationInputError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        found = await self.bot.awc.tafs(ids)
        missing = [i for i in ids if i not in found]
        tafs = [found[i] for i in ids if i in found]
        if not tafs:
            await interaction.followup.send(
                f"No TAF for {', '.join(ids)}. Not every airport has one; try the nearest larger field."
            )
            return

        def render() -> bytes:
            return theme.stack([cards.taf_card(t, tz=ctx.tz) for t in tafs])

        image = await asyncio.to_thread(render)
        alt = " | ".join(cards.taf_alt(t) for t in tafs)
        content = raw_block([t.raw for t in tafs])
        if missing:
            content += f"\nNo TAF for {', '.join(missing)}."
        await interaction.followup.send(content, file=png_file(image, "taf.png", alt))


async def setup(bot) -> None:
    await bot.add_cog(Wx(bot))
