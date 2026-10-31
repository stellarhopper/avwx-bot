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
from ..sources.http import SourceError

log = logging.getLogger(__name__)

MAX_STATIONS = 4
# How many of a server's or thread's airports /taf looks through for ones
# with a TAF. The config allows at most 12.
DEFAULT_LIST_LIMIT = 12
# Discord rejects message content over 2000 characters.
CONTENT_LIMIT = 2000


def raw_block(lines: list[str], *, limit: int = CONTENT_LIMIT) -> str:
    """Raw reports in a code block of at most `limit` characters, fences included.

    Whole lines are dropped from the end rather than one being cut mid-report.
    """
    room = limit - 8  # the two fences and their newlines
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > room - 2:
            kept.append("…")
            break
        kept.append(line)
        used += len(line) + 1
    return "```\n" + "\n".join(kept) + "\n```"


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
        try:
            ends = await self.bot.awc.runways(m.station for m in metars)
        except SourceError:
            ends = {}  # the card is still useful without the runway overlay

        def render() -> bytes:
            return theme.stack([
                cards.metar_card(m, units=ctx.units, tz=ctx.tz, runways=ends.get(m.station)) for m in metars
            ])

        image = await asyncio.to_thread(render)
        alt = " | ".join(cards.metar_alt(m, ctx.units, ends.get(m.station)) for m in metars)
        content = raw_block([m.raw for m in metars])
        if missing:
            content += f"\nNo current METAR for {', '.join(missing)}."
        await interaction.followup.send(content, file=png_file(image, "metar.png", alt))

    @app_commands.command(name="taf", description="Terminal forecast with a flight-category timeline")
    @app_commands.describe(airports="Up to 4, e.g. KDEN. Leave empty for this server's or thread's airports.")
    async def taf(self, interaction: discord.Interaction, airports: str | None = None) -> None:
        ctx = context.guild_context(self.bot.store, interaction.guild_id)
        typed = bool(airports and airports.strip())
        try:
            # Airports typed are capped at the card limit as usual. The
            # server's or thread's list is taken whole, because many fields in
            # it (KBDU, KLMO) never issue a TAF: taking its first four could
            # show one TAF while three that exist further down go unseen.
            ids = context.resolve_stations(
                self.bot.store, interaction, airports,
                limit=MAX_STATIONS if typed else DEFAULT_LIST_LIMIT, fallback=ctx.home_airports,
            )
        except context.StationInputError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        found = await self.bot.awc.tafs(ids)
        # Airports without a TAF are left out without comment: plenty of
        # fields never issue one, and saying so each time is just noise.
        tafs = [found[i] for i in ids if i in found][:MAX_STATIONS]
        if not tafs:
            if typed:
                text = f"No TAF for {', '.join(ids)}. Not every airport has one; try the nearest larger field."
            else:
                text = f"None of these airports has a TAF: {', '.join(ids)}. Name a larger field, e.g. `/taf KDEN`."
            await interaction.followup.send(text)
            return

        def render() -> bytes:
            return theme.stack([cards.taf_card(t, tz=ctx.tz) for t in tafs])

        image = await asyncio.to_thread(render)
        alt = " | ".join(cards.taf_alt(t) for t in tafs)
        content = raw_block([t.raw for t in tafs])
        await interaction.followup.send(content, file=png_file(image, "taf.png", alt))


async def setup(bot) -> None:
    await bot.add_cog(Wx(bot))
