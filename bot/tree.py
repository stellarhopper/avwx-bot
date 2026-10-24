"""Command tree with error handling and per-server command switches.

Without an `on_error` override, discord.py logs a traceback and the user sees
"The application did not respond" — no explanation. Everything here exists to
make a failure legible from both ends.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from . import guildcfg
from .sources.http import SourceError

log = logging.getLogger(__name__)

# Discord's JSON error code for an interaction it no longer knows about. Means
# the 3-second acknowledgement window elapsed before we responded; the token is
# dead and nothing can be sent on it.
UNKNOWN_INTERACTION = 10062

# An interaction must be acknowledged within 3s. Log anything that reaches the
# handler with less than a second to spare.
SLOW_DISPATCH_SECONDS = 2.0

GENERIC_FAILURE = "Something went wrong on my end. Try again in a minute, and tell a server admin if it keeps happening."

# Commands an admin can switch off per server, by top-level command name. The
# keys match the schema's `commands` section.
SWITCHABLE = {"metar", "taf", "brief", "winds", "pirep", "mountain", "alerts"}


def unwrap(error: BaseException) -> BaseException:
    """Strip discord.py's CommandInvokeError wrapper to get the real cause."""
    while isinstance(
        error, (app_commands.CommandInvokeError, app_commands.TransformerError)
    ) and error.__cause__ is not None:
        error = error.__cause__
    return error


def is_expired_interaction(error: BaseException) -> bool:
    original = unwrap(error)
    return (
        isinstance(original, discord.NotFound)
        and getattr(original, "code", None) == UNKNOWN_INTERACTION
    )


def dispatch_lag(interaction: discord.Interaction) -> float:
    return (discord.utils.utcnow() - interaction.created_at).total_seconds()


def command_disabled(stored_config: dict | None, command_name: str | None) -> bool:
    if command_name not in SWITCHABLE:
        return False
    return not guildcfg.effective(stored_config)["commands"].get(command_name, True)


class AvwxTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        lag = dispatch_lag(interaction)
        if lag >= SLOW_DISPATCH_SECONDS:
            log.warning(
                "slow dispatch: %s reached the handler %.2fs after Discord created it",
                interaction.command.qualified_name if interaction.command else "interaction",
                lag,
            )
        if interaction.type is discord.InteractionType.autocomplete:
            return True
        store = getattr(interaction.client, "store", None)
        if interaction.guild_id is None or store is None or interaction.command is None:
            return True
        root = interaction.command.qualified_name.split()[0]
        if command_disabled(store.get_config(interaction.guild_id), root):
            await self.notify(interaction, f"`/{root}` is switched off on this server.")
            return False
        return True

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        name = interaction.command.qualified_name if interaction.command else "unknown"

        if is_expired_interaction(error):
            log.warning(
                "/%s expired before it could be acknowledged (%.2fs lag, 3s limit)",
                name,
                dispatch_lag(interaction),
            )
            return

        original = unwrap(error)
        if isinstance(original, SourceError):
            # A weather API being down is not a bug in the bot; say what failed.
            log.warning("/%s: %s", name, original)
            await self.notify(interaction, f"{original} Try again in a few minutes.")
            return

        log.exception("/%s failed", name, exc_info=original)
        await self.notify(interaction, GENERIC_FAILURE)

    @staticmethod
    async def notify(interaction: discord.Interaction, message: str) -> None:
        """Best-effort user-facing message. Never raises."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.warning("could not deliver the failure notice")
