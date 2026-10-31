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

# As many airports as a server's config can list. More typed at once would
# just be a wall of cards.
MAX_STATIONS = 12
# Cards per image. A stack of twelve is twelve thousand pixels tall, which
# Discord shrinks to an unreadable strip; several images make a gallery.
CARDS_PER_IMAGE = 4
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


def raw_blocks(lines: list[str], *, limit: int = CONTENT_LIMIT) -> list[str]:
    """Raw reports split into as many code blocks of at most `limit`
    characters as they need, whole lines in each. A dozen TAFs run well past
    one message."""
    room = limit - 8  # the two fences and their newlines
    blocks: list[list[str]] = [[]]
    used = 0
    for line in lines:
        line = line if len(line) <= room else line[: room - 1] + "…"
        if blocks[-1] and used + len(line) + 1 > room:
            blocks.append([])
            used = 0
        blocks[-1].append(line)
        used += len(line) + 1
    return ["```\n" + "\n".join(b) + "\n```" for b in blocks if b]


async def send_cards(interaction: discord.Interaction, name: str, raws: list[str], cards_: list,
                     alts: list[str], render, note: str = "") -> None:
    """The cards in images of CARDS_PER_IMAGE, with the raw text after.

    `render(group)` draws one image from a slice of `cards_`. The first
    message carries the images and as much raw text as fits; the rest of
    the text follows in further messages.
    """
    groups = [cards_[i : i + CARDS_PER_IMAGE] for i in range(0, len(cards_), CARDS_PER_IMAGE)]
    alt_groups = [alts[i : i + CARDS_PER_IMAGE] for i in range(0, len(alts), CARDS_PER_IMAGE)]
    images = await asyncio.to_thread(lambda: [render(g) for g in groups])
    files = [
        png_file(img, f"{name}.png" if len(images) == 1 else f"{name}-{n}.png", " | ".join(alt))
        for n, (img, alt) in enumerate(zip(images, alt_groups), 1)
    ]
    texts = raw_blocks(raws)
    if note:
        texts[-1] += "\n" + note
    await interaction.followup.send(texts[0], files=files)
    for text in texts[1:]:
        await interaction.followup.send(text)


def png_file(data: bytes, name: str, alt: str) -> discord.File:
    # Discord caps alt text at 1024 characters.
    return discord.File(io.BytesIO(data), filename=name, description=alt[:1024])


class Wx(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(name="metar", description="Current observations, as cards")
    @app_commands.describe(airports="e.g. KBJC KAPA. Leave empty for this server's or thread's airports.")
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

        def render(group) -> bytes:
            return theme.stack([
                cards.metar_card(m, units=ctx.units, tz=ctx.tz, runways=ends.get(m.station)) for m in group
            ])

        await send_cards(
            interaction, "metar", [m.raw for m in metars], metars,
            [cards.metar_alt(m, ctx.units, ends.get(m.station)) for m in metars], render,
            note=f"No current METAR for {', '.join(missing)}." if missing else "",
        )

    @app_commands.command(name="taf", description="Terminal forecasts with a flight-category timeline")
    @app_commands.describe(airports="e.g. KDEN. Leave empty for this server's or thread's airports.")
    async def taf(self, interaction: discord.Interaction, airports: str | None = None) -> None:
        ctx = context.guild_context(self.bot.store, interaction.guild_id)
        typed = bool(airports and airports.strip())
        try:
            ids = context.resolve_stations(
                self.bot.store, interaction, airports, limit=MAX_STATIONS, fallback=ctx.home_airports
            )
        except context.StationInputError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        found = await self.bot.awc.tafs(ids)
        # Airports without a TAF are left out without comment: plenty of
        # fields never issue one, and saying so each time is just noise.
        tafs = [found[i] for i in ids if i in found]
        if not tafs:
            if typed:
                text = f"No TAF for {', '.join(ids)}. Not every airport has one; try the nearest larger field."
            else:
                text = f"None of these airports has a TAF: {', '.join(ids)}. Name a larger field, e.g. `/taf KDEN`."
            await interaction.followup.send(text)
            return

        def render(group) -> bytes:
            return theme.stack([cards.taf_card(t, tz=ctx.tz) for t in group])

        await send_cards(interaction, "taf", [t.raw for t in tafs], tafs, [cards.taf_alt(t) for t in tafs], render)


async def setup(bot) -> None:
    await bot.add_cog(Wx(bot))
