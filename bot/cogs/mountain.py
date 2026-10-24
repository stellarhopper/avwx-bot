"""/mountain: pass status, wave risk, ridge winds and density altitude."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from .. import context, mountain, regions
from ..render import mountain as mountain_render
from .wx import png_file


class Mountain(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(name="mountain", description="Mountain flying: passes, wave risk, ridge winds, density altitude")
    async def mountain_board(self, interaction: discord.Interaction) -> None:
        ctx = context.guild_context(self.bot.store, interaction.guild_id)
        if not ctx.config["mountain"].get("enabled"):
            await interaction.response.send_message(
                "Mountain flying isn't switched on for this server. "
                "An admin can turn it on with `/avwx enable mountain`.",
                ephemeral=True,
            )
            return
        region = regions.get(ctx.config["mountain"].get("region"))
        if region is None:
            await interaction.response.send_message(
                "This server's mountain region isn't one I have data for.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        board = await mountain.build(self.bot.awc, self.bot.nws, region, ctx.config)
        image = await asyncio.to_thread(mountain_render.render, board, tz=ctx.tz, units=ctx.units)
        lines = [f"**Mountain flying — {region['name']}** · wave risk **{board.wave.level}**"]
        if board.weathercams_url:
            lines.append(f"Check the cams before you go: <{board.weathercams_url}>")
        await interaction.followup.send(
            "\n".join(lines), file=png_file(image, "mountain.png", mountain_render.board_alt(board))
        )


async def setup(bot) -> None:
    await bot.add_cog(Mountain(bot))
