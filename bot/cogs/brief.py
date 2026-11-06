"""/brief, /winds, /pirep, and the scheduled morning brief."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import briefing, clock, context, guildcfg, hazards, mountain, posting, regions, route
from ..render import brief as brief_render
from ..render import winds as winds_render
from ..sources import nws
from ..sources.http import SourceError
from ..wx import fmt
from .chat import split_message
from .wx import png_file, raw_block

log = logging.getLogger(__name__)

MAX_BRIEF_AIRPORTS = 8
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# A brief more than this late (the bot was down at 06:30) is skipped rather
# than posted mid-afternoon as if it were news.
LATE_LIMIT = timedelta(minutes=60)
MAX_ATTEMPTS = 3


def due_day(now: datetime, cfg: dict) -> str | None:
    """The local date (YYYY-MM-DD) whose brief is due at `now`, or None.

    Due means: brief on, today is a configured day, the configured time has
    passed, and by no more than LATE_LIMIT.
    """
    brief = cfg["brief"]
    if not brief.get("enabled") or not brief.get("channel_id"):
        return None
    tz = fmt.zone(cfg.get("timezone"))
    local = now.astimezone(tz)
    if WEEKDAYS[local.weekday()] not in brief.get("days", WEEKDAYS):
        return None
    hh, mm = (int(x) for x in brief.get("time", "06:30").split(":"))
    scheduled = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not (scheduled <= local < scheduled + LATE_LIMIT):
        return None
    return local.strftime("%Y-%m-%d")


def brief_airports(cfg: dict) -> list[str]:
    return list(cfg["brief"].get("airports") or cfg.get("home_airports") or [])[:MAX_BRIEF_AIRPORTS]


class Briefs(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.attempts: dict[tuple[int, str], int] = {}

    async def cog_load(self) -> None:
        self.scheduler.start()

    async def cog_unload(self) -> None:
        self.scheduler.cancel()

    # ------------------------------------------------------------- building

    async def build(self, cfg: dict, airports: list[str], *, title: str, include: dict | None = None):
        include = include if include is not None else cfg["brief"]["include"]
        board = None
        region = regions.get(cfg["mountain"].get("region"))
        if include.get("mountain") and cfg["mountain"].get("enabled") and region:
            board = mountain.build(self.bot.awc, self.bot.nws, region, cfg)
        data = await briefing.gather(
            self.bot.awc,
            self.bot.nws,
            title=title,
            airports=airports,
            config=cfg,
            include=include,
            mountain=board,
        )
        radius = cfg["alerts"]["area"].get("radius_nm", briefing.HAZARD_RADIUS_NM)
        image = await asyncio.to_thread(brief_render.render, data, radius=radius)
        return data, image

    async def post_brief(self, guild: discord.Guild, cfg: dict, *, day: str | None) -> posting.Posted:
        """Post the brief. `day` is recorded so the scheduler won't repeat it; a
        manual post passes None so it doesn't use up the day's scheduled brief."""
        airports = brief_airports(cfg)
        if not airports:
            raise posting.PostError("no airports configured for the brief")
        tz = fmt.zone(cfg.get("timezone"))
        local = clock.now().astimezone(tz)
        title = f"Morning brief — {local.strftime('%a %-d %b')}"
        data, image = await self.build(cfg, airports, title=title)
        # No raw METAR or TAF text: the image and its alt text carry the
        # reports, and /metar or /taf in the thread gives the raw text to
        # anyone who wants it.
        content = f"**{title}**\n{brief_render.summary_line(data)}"
        channel = posting.resolve_channel(guild, int(cfg["brief"]["channel_id"]))
        tag = cfg["brief"].get("forum_tag_id")
        posted = await posting.post(
            self.bot.store,
            guild,
            channel.id,
            title=title,
            content=content[:2000],
            file=png_file(image, "brief.png", brief_render.alt_text(data)),
            tag_id=int(tag) if tag else None,
            kind="brief",
            airports=airports,
        )
        if posted.thread_id:
            await self.thread_details(guild, posted.thread_id, data)
        if day is not None:
            self.bot.store.record_brief(guild.id, day, channel_id=posted.channel_id, message_id=posted.message_id)
        return posted

    async def thread_details(self, guild: discord.Guild, thread_id: int, data: briefing.BriefData) -> None:
        """In a forum post there's room for the forecast discussion's aviation section."""
        for text in afd_messages(data):
            await posting.reply_in_thread(guild, thread_id, text)
        await posting.reply_in_thread(
            guild,
            thread_id,
            "-# Run `/metar`, `/taf`, `/pirep` or `/winds` here with no airport to get this brief's airports.",
        )

    # ------------------------------------------------------------ scheduler

    @tasks.loop(minutes=1)
    async def scheduler(self) -> None:
        now = clock.now()
        for guild in list(self.bot.guilds):
            cfg = guildcfg.effective(self.bot.store.get_config(guild.id))
            day = due_day(now, cfg)
            if day is None or self.bot.store.brief_sent(guild.id, day):
                continue
            key = (guild.id, day)
            if self.attempts.get(key, 0) >= MAX_ATTEMPTS:
                continue
            self.attempts[key] = self.attempts.get(key, 0) + 1
            try:
                await self.post_brief(guild, cfg, day=day)
                log.info("guild %s: posted the %s brief", guild.id, day)
            except (posting.PostError, SourceError) as exc:
                log.warning("guild %s: brief for %s failed (attempt %d): %s", guild.id, day, self.attempts[key], exc)
            except Exception:  # noqa: BLE001 - one guild's failure mustn't stop the loop
                log.exception("guild %s: brief for %s failed", guild.id, day)
        # Forget attempts from previous days.
        today = now.strftime("%Y-%m-%d")
        for key in [k for k in self.attempts if k[1] < today]:
            del self.attempts[key]
        self.bot.http_client.purge()

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------- commands

    @app_commands.command(name="brief", description="A dashboard of current weather, forecasts and hazards")
    @app_commands.describe(airports="Up to 8, e.g. KBJC KAPA. Leave empty for this server's or thread's airports.")
    async def brief(self, interaction: discord.Interaction, airports: str | None = None) -> None:
        ctx = context.guild_context(self.bot.store, interaction.guild_id)
        try:
            ids = context.resolve_stations(
                self.bot.store,
                interaction,
                airports,
                limit=MAX_BRIEF_AIRPORTS,
                fallback=brief_airports(ctx.config),
            )
        except context.StationInputError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        data, image = await self.build(ctx.config, ids, title="Weather brief")
        # The same as the morning brief: the image, the summary line, then the
        # forecast discussion as text. No raw reports; /metar and /taf give those.
        await interaction.followup.send(
            brief_render.summary_line(data)[:2000], file=png_file(image, "brief.png", brief_render.alt_text(data))
        )
        for text in afd_messages(data):
            await interaction.followup.send(text)

    @app_commands.command(name="winds", description="Winds and temperatures aloft")
    @app_commands.describe(where="An airport (nearest forecast site is used) or a winds-aloft site such as DEN")
    async def winds(self, interaction: discord.Interaction, where: str | None = None) -> None:
        ctx = context.guild_context(self.bot.store, interaction.guild_id)
        await interaction.response.defer(thinking=True)
        forecast = await self.bot.awc.winds_aloft()
        site = None
        typed = (where or "").strip().upper()
        if typed and forecast.site(typed) and len(typed) == 3:
            site = typed
        else:
            try:
                ids = context.resolve_stations(
                    self.bot.store, interaction, typed or None, limit=1, fallback=ctx.home_airports
                )
            except context.StationInputError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            stations = await self.bot.awc.stations(ids)
            if ids[0] not in stations:
                await interaction.followup.send(f"I don't know where {ids[0]} is.", ephemeral=True)
                return
            site = hazards.nearest_fb_site(stations[ids[0]].position, forecast.sites)
        levels = forecast.site(site) if site else {}
        if not levels:
            await interaction.followup.send("No winds-aloft forecast for there (they cover the US only).", ephemeral=True)
            return
        use = f"{forecast.use_from}–{forecast.use_to}Z" if forecast.use_from else None
        image = await asyncio.to_thread(winds_render.winds_card, site, forecast.valid, use, levels, units=ctx.units)
        await interaction.followup.send(
            f"Winds aloft for **{site}**, valid {forecast.valid}.",
            file=png_file(image, "winds.png", winds_render.winds_alt(site, levels)),
        )

    @app_commands.command(name="pirep", description="Recent pilot reports near an airport")
    @app_commands.describe(airport="Center of the search", radius="Nautical miles (default 100)", hours="How far back (default 3)")
    async def pirep(
        self,
        interaction: discord.Interaction,
        airport: str | None = None,
        radius: app_commands.Range[int, 10, 300] = 100,
        hours: app_commands.Range[int, 1, 12] = 3,
    ) -> None:
        ctx = context.guild_context(self.bot.store, interaction.guild_id)
        try:
            ids = context.resolve_stations(self.bot.store, interaction, airport, limit=1, fallback=ctx.home_airports)
        except context.StationInputError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        reps = await self.bot.awc.pireps(ids[0], radius, hours)
        await interaction.followup.send(pirep_message(ids[0], radius, hours, reps, ctx.tz))

    @app_commands.command(name="discussion", description="The latest NWS forecast discussion")
    @app_commands.describe(
        where="An airport, town or pass. Leave empty for this server's or thread's airports.",
        section="Just the aviation section (default), or the whole discussion",
    )
    @app_commands.choices(section=[
        app_commands.Choice(name="Aviation", value="aviation"),
        app_commands.Choice(name="All sections", value="all"),
    ])
    async def discussion(
        self, interaction: discord.Interaction, where: str | None = None,
        section: app_commands.Choice[str] | None = None,
    ) -> None:
        ctx = context.guild_context(self.bot.store, interaction.guild_id)
        typed = (where or "").strip()
        if not typed:
            try:
                typed = context.resolve_stations(
                    self.bot.store, interaction, None, limit=1, fallback=ctx.home_airports
                )[0]
            except context.StationInputError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.defer(thinking=True)
        # Discussions come from the NWS office covering a place, so any place
        # the route resolver understands will do: "Granby" finds GJT's.
        home = ctx.home_airports[0] if ctx.home_airports else None
        resolved = await route.resolve_near(self.bot.awc, typed, home)
        if resolved.status != "resolved":
            options = ", ".join(f"{c.point.ident} ({c.point.note or c.point.name})" for c in resolved.candidates)
            text = f"I'm not sure where `{typed[:40]}` is." + (f" Did you mean {options}?" if options else "")
            await interaction.followup.send(text, ephemeral=True)
            return
        point = resolved.point
        office = await self.bot.nws.office_for(point.lat, point.lon)
        if not office:
            await interaction.followup.send(
                f"No NWS forecast office covers {point.label()}; forecast discussions are US only.", ephemeral=True
            )
            return
        found = await self.bot.nws.discussion(office)
        if found is None:
            await interaction.followup.send(f"NWS {office} has no current forecast discussion.", ephemeral=True)
            return
        parts = nws.sections(found.text)
        everything = section is not None and section.value == "all"
        if not everything:
            parts = [(t, b) for t, b in parts if t.upper().startswith("AVIATION")]
            if not parts:
                await interaction.followup.send(
                    f"NWS {office}'s latest discussion has no aviation section. Try `section: All sections`."
                )
                return
        name = nws.office_name(found.text) or office
        messages = discussion_messages(
            "all sections" if everything else "aviation", name, found.issued, ctx.tz, parts
        )
        for text in messages:
            await interaction.followup.send(text)


def discussion_messages(heading: str, office: str, issued, tz, parts: list[tuple[str, str]]) -> list[str]:
    """Forecast discussion text as Discord messages: one heading, then the
    text reflowed so it wraps to the screen rather than at NWS's 66 columns,
    and split across messages rather than cut off. With more than one part,
    each gets its section title."""
    if not parts:
        return []
    when = f", issued {fmt.local(issued, tz, '%a %H:%M')}" if issued else ""
    head = f"**Forecast discussion — {heading}** (NWS {office}{when})"
    if len(parts) == 1:
        text = nws.reflow(parts[0][1])
    else:
        text = "\n\n".join(f"**{title}**\n{nws.reflow(body)}" for title, body in parts)
    chunks = split_message(text, limit=1900 - len(head))
    return [(head + "\n" if i == 0 else "") + ">>> " + chunk for i, chunk in enumerate(chunks)]


def afd_messages(data: briefing.BriefData) -> list[str]:
    """The brief's discussion: the aviation section. The morning brief and
    /brief both post it."""
    if not data.afd:
        return []
    return discussion_messages("aviation", data.afd.office, data.afd.issued, data.tz, [("Aviation", data.afd.aviation)])


def pirep_message(center: str, radius: int, hours: int, reps, tz) -> str:
    head = f"**PIREPs within {radius} nm of {center}, last {hours} h** — {len(reps)} report{'s' if len(reps) != 1 else ''}"
    if not reps:
        return head + "\nNone."
    notable = briefing.notable_pireps(reps)
    rest = [p for p in sorted(reps, key=lambda p: p.observed.timestamp() if p.observed else 0, reverse=True) if p not in notable]
    lines = []
    for p in [*notable, *rest]:
        when = p.observed.astimezone(tz).strftime("%H:%M") if p.observed else "--:--"
        flag = "‼ " if p.urgent else ("! " if p in notable else "  ")
        lines.append(f"{flag}{when} {p.raw}")
    if notable:
        head += f", {len(notable)} worth a look (marked !)"
    return head + "\n" + raw_block(lines, limit=2000 - len(head) - 1)


async def setup(bot) -> None:
    await bot.add_cog(Briefs(bot))
