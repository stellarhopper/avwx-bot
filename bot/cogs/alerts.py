"""Severe-weather alerts: the watcher loop and /alerts."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import alerting, briefing, context, guildcfg, hazards, posting
from ..alerting import Candidate
from ..render import alert as alert_render
from ..sources.http import SourceError
from ..store import SentAlert
from ..wx import fmt
from .wx import png_file

log = logging.getLogger(__name__)

POLL_MINUTES = 3
RETAIN_CLOSED_DAYS = 7


def area_airports(cfg: dict) -> list[str]:
    return list(cfg["alerts"]["area"].get("airports") or cfg.get("home_airports") or [])


class Alerts(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.watcher.start()

    async def cog_unload(self) -> None:
        self.watcher.cancel()

    # ------------------------------------------------------------ gathering

    async def candidates(self, cfg: dict, now: datetime) -> tuple[list[Candidate], set[str]]:
        """Everything that should be posted for this config right now, and the
        kinds whose source failed (so their open posts aren't wrongly ended)."""
        acfg = cfg["alerts"]
        triggers = acfg["triggers"]
        radius = acfg["area"].get("radius_nm", 25)
        airports = area_airports(cfg)
        failed: set[str] = set()
        out: list[Candidate] = []
        if not airports:
            return out, failed
        try:
            stations = await self.bot.awc.stations(airports)
        except SourceError:
            return out, {alerting.KIND_NWS, alerting.KIND_CONVECTIVE, alerting.KIND_CWA, alerting.KIND_METAR}
        points = [s.position for s in stations.values()]
        states = {s.state for s in stations.values() if s.country == "US" and s.state}

        if triggers.get("nws_events") and states and points:
            try:
                found = await briefing.us_alerts(self.bot.nws, states, points, radius)
                out += alerting.nws_candidates(found, triggers["nws_events"], now)
            except SourceError:
                failed.add(alerting.KIND_NWS)

        if (triggers.get("convective_sigmet") or triggers.get("cwa")) and points:
            try:
                sigmets, cwas = await asyncio.gather(self.bot.awc.sigmets(), self.bot.awc.cwas())
                near = hazards.near([*sigmets, *cwas], points, radius, now, hours=0)
                out += alerting.area_candidates(
                    near, convective=bool(triggers.get("convective_sigmet")), cwa=bool(triggers.get("cwa"))
                )
            except SourceError:
                failed.update({alerting.KIND_CONVECTIVE, alerting.KIND_CWA})

        if triggers.get("metar_thunderstorm") or triggers.get("metar_gust_kt") or triggers.get("metar_funnel"):
            try:
                metars = await self.bot.awc.metars(airports)
                out += alerting.metar_candidates(
                    list(metars.values()),
                    thunderstorm=bool(triggers.get("metar_thunderstorm")),
                    gust_kt=int(triggers.get("metar_gust_kt") or 0),
                    funnel=bool(triggers.get("metar_funnel")),
                    now=now,
                )
            except SourceError:
                failed.add(alerting.KIND_METAR)
        return sorted(out, key=lambda c: -c.severity), failed

    # --------------------------------------------------------- reconciling

    async def check_guild(self, guild: discord.Guild, cfg: dict, now: datetime) -> None:
        found, failed = await self.candidates(cfg, now)
        store = self.bot.store
        seen = set()
        for c in found:
            seen.add(c.key)
            existing = store.get_alert(guild.id, c.key)
            if existing is not None and existing.closed_at is None:
                if existing.fingerprint != c.fingerprint:
                    await self.update(guild, cfg, existing, c, now)
                continue
            if c.kind in alerting.COOLDOWN_KINDS and not alerting.cooled_down(
                store.last_alert_at(guild.id, c.kind), now, cfg["alerts"].get("cooldown_min", 30)
            ):
                continue
            await self.announce(guild, cfg, c, now)

        for alert in store.open_alerts(guild.id):
            if alert.alert_key in seen or alert.kind in failed:
                continue
            await self.close(guild, cfg, alert, now)

    def _ping(self, cfg: dict, now: datetime) -> int | None:
        role = cfg["alerts"].get("mention_role_id")
        if not role:
            return None
        local = now.astimezone(fmt.zone(cfg.get("timezone")))
        return None if alerting.in_quiet_hours(local, cfg["alerts"].get("quiet_hours")) else int(role)

    async def announce(self, guild: discord.Guild, cfg: dict, c: Candidate, now: datetime) -> None:
        tz = fmt.zone(cfg.get("timezone"))
        ping = self._ping(cfg, now)
        image = await asyncio.to_thread(alert_render.alert_card, c, tz=tz, now=now)
        tag = cfg["alerts"].get("forum_tag_id")
        try:
            posted = await posting.post(
                self.bot.store,
                guild,
                int(cfg["alerts"]["channel_id"]),
                title=alerting.thread_title(c, tz),
                content=alerting.message_text(c, tz, ping_role=ping),
                file=png_file(image, "alert.png", alert_render.alert_alt(c, tz)),
                tag_id=int(tag) if tag else None,
                kind="alert",
                airports=c.stations or area_airports(cfg),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=[discord.Object(ping)] if ping else False
                ),
            )
        except posting.PostError as exc:
            log.warning("guild %s: couldn't post alert %s: %s", guild.id, c.key, exc)
            return
        self.bot.store.record_alert(
            SentAlert(
                guild_id=guild.id,
                alert_key=c.key,
                channel_id=posted.channel_id,
                message_id=posted.message_id,
                thread_id=posted.thread_id,
                fingerprint=c.fingerprint,
                kind=c.kind,
                posted_at=int(now.timestamp()),
                expires_at=int(c.until.timestamp()) if c.until else None,
                closed_at=None,
            )
        )
        log.info("guild %s: posted alert %s", guild.id, c.key)

    async def update(self, guild: discord.Guild, cfg: dict, existing: SentAlert, c: Candidate, now: datetime) -> None:
        tz = fmt.zone(cfg.get("timezone"))
        message = await posting.find_message(guild, existing.channel_id, existing.message_id, existing.thread_id)
        if message is not None:
            image = await asyncio.to_thread(alert_render.alert_card, c, tz=tz, now=now)
            try:
                await message.edit(
                    content=alerting.message_text(c, tz, ping_role=self._ping(cfg, now), updated=now),
                    attachments=[png_file(image, "alert.png", alert_render.alert_alt(c, tz))],
                )
            except discord.HTTPException as exc:
                log.warning("guild %s: couldn't edit alert %s: %s", guild.id, c.key, exc)
        if existing.thread_id:
            until = f" Now until {c.until.astimezone(tz).strftime('%H:%M')}." if c.until else ""
            await posting.reply_in_thread(guild, existing.thread_id, f"**Updated {now.astimezone(tz).strftime('%H:%M')}.**{until}\n" + "\n".join(f"> {line}" for line in c.lines[:4]))
        self.bot.store.update_alert(
            guild.id, c.key, fingerprint=c.fingerprint, expires_at=int(c.until.timestamp()) if c.until else None
        )

    async def close(self, guild: discord.Guild, cfg: dict, alert: SentAlert, now: datetime) -> None:
        tz = fmt.zone(cfg.get("timezone"))
        message = await posting.find_message(guild, alert.channel_id, alert.message_id, alert.thread_id)
        if message is not None:
            content = message.content or ""
            first = content.split("\n", 1)[0]
            ended = f"✅ Ended {now.astimezone(tz).strftime('%H:%M')} — " + first.lstrip("🟥🟧🟨 ")
            try:
                await message.edit(content=ended[:2000])
            except discord.HTTPException as exc:
                log.warning("guild %s: couldn't mark alert %s ended: %s", guild.id, alert.alert_key, exc)
        if alert.thread_id:
            await posting.reply_in_thread(guild, alert.thread_id, f"✅ Ended {now.astimezone(tz).strftime('%H:%M')}.")
            thread = guild.get_thread(alert.thread_id)
            if thread is not None:
                try:
                    await thread.edit(archived=True, locked=True)
                except discord.HTTPException:
                    pass  # Manage Threads is optional; an unlocked ended post is fine
        self.bot.store.update_alert(guild.id, alert.alert_key, closed_at=int(now.timestamp()))
        log.info("guild %s: closed alert %s", guild.id, alert.alert_key)

    # ---------------------------------------------------------------- loop

    @tasks.loop(minutes=POLL_MINUTES)
    async def watcher(self) -> None:
        now = datetime.now(timezone.utc)
        for guild in list(self.bot.guilds):
            cfg = guildcfg.effective(self.bot.store.get_config(guild.id))
            if not cfg["alerts"].get("enabled") or not cfg["alerts"].get("channel_id"):
                continue
            try:
                await self.check_guild(guild, cfg, now)
            except Exception:  # noqa: BLE001 - one guild's failure mustn't stop the loop
                log.exception("guild %s: alert check failed", guild.id)
        self.bot.store.prune_alerts(older_than=int(time.time()) - RETAIN_CLOSED_DAYS * 86400)

    @watcher.before_loop
    async def before_watcher(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------- command

    @app_commands.command(name="alerts", description="Severe weather affecting this server's area right now")
    async def alerts(self, interaction: discord.Interaction) -> None:
        ctx = context.guild_context(self.bot.store, interaction.guild_id)
        if not area_airports(ctx.config):
            await interaction.response.send_message(
                "This server has no home or alert airports set, so I don't know which area to check.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        now = datetime.now(timezone.utc)
        found, failed = await self.candidates(ctx.config, now)
        radius = ctx.config["alerts"]["area"].get("radius_nm", 25)
        head = f"**Within {radius} nm of {', '.join(area_airports(ctx.config))}:**"
        if not found:
            body = "Nothing severe right now."
        else:
            body = "\n\n".join(alerting.message_text(c, ctx.tz, ping_role=None) for c in found[:8])
        if failed:
            body += f"\n-# Couldn't check: {', '.join(sorted(failed))}."
        await interaction.followup.send(f"{head}\n{body}"[:2000])


async def setup(bot) -> None:
    await bot.add_cog(Alerts(bot))
