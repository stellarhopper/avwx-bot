"""Who may change a server's configuration.

Two independent grants, either of which is sufficient:

* the Discord **Manage Server** permission — the permanent bootstrap, so a
  server owner can never lock themselves out, and
* membership in a role listed in the config's ``admin_role_ids`` — so an
  existing staff role can manage the bot without full server management.

The decision itself is a pure function so it can be tested without Discord.
"""

from __future__ import annotations

from collections.abc import Iterable

import discord

from . import guildcfg
from .store import Store


def has_admin_access(
    *, manage_guild: bool, member_role_ids: Iterable[int], admin_role_ids: Iterable[int]
) -> bool:
    if manage_guild:
        return True
    allowed = set(admin_role_ids)
    return any(role_id in allowed for role_id in member_role_ids)


def is_admin(store: Store, interaction: discord.Interaction) -> bool:
    if interaction.guild_id is None:
        return False
    member = interaction.user
    if not isinstance(member, discord.Member):
        # A guild-only command reached us with a bare User, which happens when
        # the member cache is cold. Fail closed.
        return False
    roles = guildcfg.effective(store.get_config(interaction.guild_id)).get("admin_role_ids", [])
    return has_admin_access(
        manage_guild=member.guild_permissions.manage_guild,
        member_role_ids=[role.id for role in member.roles],
        admin_role_ids=[int(r) for r in roles],
    )


DENIED = (
    "That's for server admins: you need the **Manage Server** permission, or a "
    "role listed as an admin role in this server's avwx-bot config."
)


async def require_admin(store: Store, interaction: discord.Interaction) -> bool:
    """Check admin access, replying with an explanation if it fails."""
    if is_admin(store, interaction):
        return True
    if interaction.response.is_done():
        await interaction.followup.send(DENIED, ephemeral=True)
    else:
        await interaction.response.send_message(DENIED, ephemeral=True)
    return False
