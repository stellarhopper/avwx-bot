"""/avwx: server configuration.

The round trip is:

1. `/avwx config edit` replies, privately, with a link to the configurator.
   The link's #fragment carries the server's current config plus the names
   and IDs of its channels and roles, which the static site can't look up.
2. The admin edits on the site and downloads config.json.
3. `/avwx config import` with that file attached validates it, shows what
   would change, and applies it on **Apply**.

Quick on/off switches exist too, for the changes not worth a round trip.
"""

from __future__ import annotations

import io
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import guildcfg, perms, posting, regions
from ..sources.http import SourceError

log = logging.getLogger(__name__)

# Discord caps message content at 2000 characters. The link goes in a
# sentence, so leave room for the words around it.
MAX_LINK = 1800
CONFIRM_TIMEOUT = 600

FEATURES = {
    "morning-brief": ("brief", "enabled"),
    "alerts": ("alerts", "enabled"),
    "mountain": ("mountain", "enabled"),
    "metar": ("commands", "metar"),
    "taf": ("commands", "taf"),
    "brief-command": ("commands", "brief"),
    "winds-command": ("commands", "winds"),
    "pirep-command": ("commands", "pirep"),
    "mountain-command": ("commands", "mountain"),
    "alerts-command": ("commands", "alerts"),
}
FEATURE_CHOICES = [
    app_commands.Choice(name="Morning brief (automatic post)", value="morning-brief"),
    app_commands.Choice(name="Severe-weather alerts (automatic post)", value="alerts"),
    app_commands.Choice(name="Mountain flying", value="mountain"),
    app_commands.Choice(name="/metar", value="metar"),
    app_commands.Choice(name="/taf", value="taf"),
    app_commands.Choice(name="/brief", value="brief-command"),
    app_commands.Choice(name="/winds", value="winds-command"),
    app_commands.Choice(name="/pirep", value="pirep-command"),
    app_commands.Choice(name="/mountain", value="mountain-command"),
    app_commands.Choice(name="/alerts", value="alerts-command"),
]


# --------------------------------------------------------------- guild facts


def channel_kind(channel) -> str:
    """"text", "forum", or "other". By channel type rather than class, so
    announcement channels count as text and tests can use simple fakes."""
    kind = getattr(channel, "type", None)
    if kind == discord.ChannelType.forum:
        return "forum"
    if kind in (discord.ChannelType.text, discord.ChannelType.news):
        return "text"
    return "other"


def channel_info(guild: discord.Guild, channel_id: int) -> guildcfg.ChannelInfo | None:
    channel = guild.get_channel(channel_id)
    if channel is None:
        return None
    p = channel.permissions_for(guild.me)
    return guildcfg.ChannelInfo(
        name=channel.name,
        kind=channel_kind(channel),
        can_post=p.view_channel and p.send_messages,
        can_attach=p.attach_files,
        can_post_in_threads=p.send_messages_in_threads,
        can_create_threads=p.create_public_threads,
        can_read_history=p.read_message_history,
    )


def guild_hints(guild: discord.Guild) -> dict:
    """Names the configurator needs to show dropdowns instead of raw IDs."""
    channels = []
    for channel in sorted(guild.channels, key=lambda c: (c.category.position if c.category else -1, c.position)):
        kind = channel_kind(channel)
        if kind == "other":
            continue
        p = channel.permissions_for(guild.me)
        if not p.view_channel:
            continue
        entry = {
            "id": str(channel.id),
            "name": channel.name,
            "type": kind,
            "category": channel.category.name if channel.category else None,
            "ok": bool(p.send_messages and p.attach_files),
        }
        if kind == "forum":
            entry["tags"] = [{"id": str(t.id), "name": t.name} for t in channel.available_tags]
        channels.append(entry)
    roles = [
        {"id": str(r.id), "name": r.name}
        for r in sorted(guild.roles, key=lambda r: -r.position)
        if not r.is_default() and not r.managed
    ]
    return {"id": str(guild.id), "name": guild.name, "channels": channels, "roles": roles}


def editable_document(stored: dict | None) -> dict:
    """The full effective config — every setting visible, defaults included."""
    return guildcfg.strip_hints(guildcfg.effective(stored))


# -------------------------------------------------------------- diff display


def _value(v) -> str:
    if v is None:
        return "—"
    text = json.dumps(v, ensure_ascii=False)
    return text if len(text) <= 60 else text[:57] + "…"


def describe_changes(changes: list[tuple[str, object, object]], limit: int = 1500) -> str:
    lines = [f"`{path}`: {_value(before)} → **{_value(after)}**" for path, before, after in changes]
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) > limit:
            out.append(f"…and {len(lines) - i} more.")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def bullet(items: list[str]) -> str:
    return "\n".join(f"• {item}" for item in items)


class ConfirmView(discord.ui.View):
    """Apply / Cancel for a pending config change. Only its requester can press."""

    def __init__(self, cog: "Admin", *, guild_id: int, user_id: int, doc: dict, base: dict | None) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.doc = doc
        self.base = base

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This change belongs to someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success)
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        store = self.cog.bot.store
        current = store.get_config(self.guild_id)
        if current != self.base:
            # Someone else applied a change while this one sat waiting. Applying
            # now would silently revert theirs.
            await interaction.response.edit_message(
                content="The config changed while this was waiting, so I didn't apply it. Run the import again.",
                view=None,
            )
            self.stop()
            return
        store.set_config(self.guild_id, self.doc, actor_id=self.user_id)
        log.info("guild %s: config updated by %s", self.guild_id, self.user_id)
        await interaction.response.edit_message(content="✅ Applied.", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled; nothing changed.", view=None)
        self.stop()


class Admin(commands.Cog):
    avwx = app_commands.Group(name="avwx", description="avwx-bot settings for this server", guild_only=True)
    config = app_commands.Group(name="config", description="View or change this server's settings", parent=avwx)

    def __init__(self, bot) -> None:
        self.bot = bot

    # ---------------------------------------------------------------- checks

    async def check(self, guild: discord.Guild, doc: dict) -> tuple[list[str], list[str]]:
        """Problems and warnings beyond the schema, looking at the live server."""
        stations = guildcfg.config_stations(doc)
        unknown: set[str] = set()
        warnings: list[str] = []
        if stations:
            try:
                known = await self.bot.awc.stations(stations)
                unknown = stations - set(known)
                foreign = sorted(s for s, info in known.items() if info.country and info.country != "US")
                eff = guildcfg.effective(doc)
                us_only = eff["alerts"]["enabled"] or eff["mountain"]["enabled"] or eff["brief"]["include"]["afd_aviation"]
                if foreign and us_only:
                    warnings.append(
                        f"{', '.join(foreign)} {'is' if len(foreign) == 1 else 'are'} outside the US. "
                        "Alerts, mountain data and forecast discussions only cover the US."
                    )
            except SourceError:
                warnings.append("I couldn't reach aviationweather.gov to check the airport list.")
        problems, more = guildcfg.semantic_problems(
            doc,
            channel=lambda cid: channel_info(guild, cid),
            role_exists=lambda rid: guild.get_role(rid) is not None,
            unknown_stations=unknown,
            known_regions=regions.all_regions().keys(),
            known_passes=regions.pass_ids,
        )
        return problems, warnings + more

    async def propose(self, interaction: discord.Interaction, doc: dict, *, verb: str) -> None:
        """Validate `doc`, show the diff, and offer Apply/Cancel. Response must be deferred."""
        guild = interaction.guild
        problems, warnings = await self.check(guild, doc)
        if problems:
            text = f"I can't apply that:\n{bullet(problems)}"
            if warnings:
                text += f"\n\nAlso:\n{bullet(warnings)}"
            await interaction.followup.send(text[:2000], ephemeral=True)
            return
        base = self.bot.store.get_config(guild.id)
        changes = guildcfg.diff(base, doc)
        if not changes:
            await interaction.followup.send("That's the same as the current config; nothing to do.", ephemeral=True)
            return
        text = f"**{verb}** — {len(changes)} change{'s' if len(changes) != 1 else ''}:\n{describe_changes(changes)}"
        if warnings:
            text += f"\n\n⚠️ {bullet(warnings)}"
        view = ConfirmView(self, guild_id=guild.id, user_id=interaction.user.id, doc=doc, base=base)
        await interaction.followup.send(text[:2000], view=view, ephemeral=True)

    # --------------------------------------------------------------- config

    @config.command(name="edit", description="Get a link to the configurator, pre-filled with this server's settings")
    async def config_edit(self, interaction: discord.Interaction) -> None:
        if not await perms.require_admin(self.bot.store, interaction):
            return
        doc = editable_document(self.bot.store.get_config(interaction.guild_id))
        doc[guildcfg.HINTS_KEY] = guild_hints(interaction.guild)
        site = self.bot.config.site_url
        url = f"{site}#c={guildcfg.encode_fragment(doc)}"
        steps = (
            "When you're done, press **Download config.json** on the page, then run "
            "`/avwx config import` here and attach the file. Nothing changes until you press **Apply**."
        )
        if len(url) <= MAX_LINK:
            await interaction.response.send_message(
                f"**[Open the configurator for {interaction.guild.name}](<{url}>)**\n{steps}", ephemeral=True
            )
            return
        # Too many channels to fit in a link. Hand over the same document as a
        # file; the site's "Open file" button reads it.
        data = json.dumps(doc, indent=2, ensure_ascii=False).encode()
        await interaction.response.send_message(
            f"This server has too many channels to fit in a link, so here's the config as a file. "
            f"Open **<{site}>**, press **Open file**, and choose it.\n{steps}",
            file=discord.File(io.BytesIO(data), filename="config.json"),
            ephemeral=True,
        )

    @config.command(name="import", description="Apply a config.json from the configurator")
    @app_commands.describe(file="The config.json the configurator saved")
    async def config_import(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        if not await perms.require_admin(self.bot.store, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if file.size > guildcfg.MAX_IMPORT_BYTES:
            await interaction.followup.send(
                f"That file is {file.size // 1024} KB; a config is a few KB. Is it the right file?", ephemeral=True
            )
            return
        try:
            doc = guildcfg.parse_import(await file.read())
        except guildcfg.ConfigError as exc:
            await interaction.followup.send(
                f"That config has problems:\n{bullet(exc.problems[:15])}"[:2000], ephemeral=True
            )
            return
        await self.propose(interaction, doc, verb="Import")

    @config.command(name="export", description="Download this server's current settings as config.json")
    async def config_export(self, interaction: discord.Interaction) -> None:
        if not await perms.require_admin(self.bot.store, interaction):
            return
        doc = editable_document(self.bot.store.get_config(interaction.guild_id))
        data = json.dumps(doc, indent=2, ensure_ascii=False).encode()
        await interaction.response.send_message(
            "This server's current settings:",
            file=discord.File(io.BytesIO(data), filename="config.json"),
            ephemeral=True,
        )

    @config.command(name="show", description="Summarise this server's settings")
    async def config_show(self, interaction: discord.Interaction) -> None:
        if not await perms.require_admin(self.bot.store, interaction):
            return
        stored = self.bot.store.get_config(interaction.guild_id)
        await interaction.response.send_message(summary(stored), ephemeral=True)

    @config.command(name="rollback", description="Go back to the previous settings")
    async def config_rollback(self, interaction: discord.Interaction) -> None:
        if not await perms.require_admin(self.bot.store, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        history = self.bot.store.config_history(interaction.guild_id, limit=2)
        if len(history) < 2:
            await interaction.followup.send("There's no earlier version to go back to.", ephemeral=True)
            return
        await self.propose(interaction, history[1].config, verb="Roll back")

    # ------------------------------------------------------------ switches

    async def _switch(self, interaction: discord.Interaction, feature: str, on: bool) -> None:
        if not await perms.require_admin(self.bot.store, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        section, key = FEATURES[feature]
        doc = editable_document(self.bot.store.get_config(interaction.guild_id))
        doc.setdefault(section, {})[key] = on
        await self.propose(interaction, doc, verb=f"{'Enable' if on else 'Disable'} {feature}")

    @avwx.command(name="enable", description="Switch a feature on")
    @app_commands.choices(feature=FEATURE_CHOICES)
    async def enable(self, interaction: discord.Interaction, feature: app_commands.Choice[str]) -> None:
        await self._switch(interaction, feature.value, True)

    @avwx.command(name="disable", description="Switch a feature off")
    @app_commands.choices(feature=FEATURE_CHOICES)
    async def disable(self, interaction: discord.Interaction, feature: app_commands.Choice[str]) -> None:
        await self._switch(interaction, feature.value, False)

    @avwx.command(name="post-brief", description="Post the morning brief now, to check the channel and layout")
    async def post_brief(self, interaction: discord.Interaction) -> None:
        if not await perms.require_admin(self.bot.store, interaction):
            return
        cfg = guildcfg.effective(self.bot.store.get_config(interaction.guild_id))
        if not cfg["brief"].get("channel_id"):
            await interaction.response.send_message(
                "There's no brief channel set. Choose one with `/avwx config edit`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        briefs = self.bot.get_cog("Briefs")
        try:
            posted = await briefs.post_brief(interaction.guild, cfg, day=None)
        except (posting.PostError, SourceError) as exc:
            await interaction.followup.send(f"Couldn't post the brief: {exc}", ephemeral=True)
            return
        where = f"<#{posted.thread_id}>" if posted.thread_id else f"<#{posted.channel_id}>"
        await interaction.followup.send(
            f"Posted to {where}. Today's scheduled brief will still go out as normal.", ephemeral=True
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("removed from guild %s; forgetting its data", guild.id)
        self.bot.store.forget_guild(guild.id)


def _onoff(value: bool) -> str:
    return "on" if value else "off"


def summary(stored: dict | None) -> str:
    eff = guildcfg.effective(stored)
    if stored is None:
        head = "Not configured yet — everything is at its defaults. Run `/avwx config edit` to set things up.\n"
    else:
        head = ""
    brief, alerts, mountain = eff["brief"], eff["alerts"], eff["mountain"]
    cmds = ", ".join(f"/{k}" for k, v in eff["commands"].items() if v) or "none"
    lines = [
        head,
        f"**Time zone:** {eff['timezone']}",
        f"**Home airports:** {', '.join(eff.get('home_airports') or []) or '—'}",
        f"**Commands on:** {cmds}",
        f"**Morning brief:** {_onoff(brief['enabled'])}"
        + (f" — {brief['time']} in <#{brief['channel_id']}>" if brief.get("channel_id") else ""),
        f"**Alerts:** {_onoff(alerts['enabled'])}"
        + (f" — in <#{alerts['channel_id']}>, {alerts['area']['radius_nm']} nm around "
           f"{', '.join(alerts['area'].get('airports') or eff.get('home_airports') or []) or '—'}"
           if alerts.get("channel_id") else ""),
        f"**Mountain flying:** {_onoff(mountain['enabled'])}"
        + (f" — {mountain['region']}" if mountain["enabled"] else ""),
    ]
    return "\n".join(line for line in lines if line)


async def setup(bot) -> None:
    await bot.add_cog(Admin(bot))
