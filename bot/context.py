"""Per-interaction context: which server config applies and which airports to use."""

from __future__ import annotations

import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import discord

from . import guildcfg
from .store import Store
from .wx import fmt

_SPLIT = re.compile(r"[\s,;/]+")
_STATION = re.compile(r"^[A-Z0-9]{3,4}$")


class StationInputError(ValueError):
    """User-typed station list that can't be used. The message is user-facing."""


def parse_stations(text: str, *, limit: int) -> list[str]:
    """`kbjc apa, KDEN` → ["KBJC", "KAPA", "KDEN"].

    A three-letter, all-letter identifier gets a K in front, since that is how
    US pilots write them and no ICAO identifier is three characters. Ones with
    digits (00C, C24) are left alone; they are FAA-only identifiers.
    """
    out: list[str] = []
    for token in _SPLIT.split(text.strip().upper()):
        if not token:
            continue
        if not _STATION.match(token):
            raise StationInputError(f"`{token[:12]}` doesn't look like an airport identifier.")
        if len(token) == 3 and token.isalpha():
            token = "K" + token
        if token not in out:
            out.append(token)
    if not out:
        raise StationInputError("Give me at least one airport, e.g. `KBJC`.")
    if len(out) > limit:
        raise StationInputError(f"That's {len(out)} airports; I'll do up to {limit} at a time.")
    return out


@dataclass(frozen=True, slots=True)
class GuildContext:
    stored: dict | None
    config: dict  # effective

    @property
    def tz(self) -> ZoneInfo:
        return fmt.zone(self.config.get("timezone"))

    @property
    def units(self) -> fmt.Units:
        return fmt.Units.from_config(self.config)

    @property
    def home_airports(self) -> list[str]:
        return list(self.config.get("home_airports") or [])


def guild_context(store: Store, guild_id: int | None) -> GuildContext:
    stored = store.get_config(guild_id) if guild_id else None
    return GuildContext(stored=stored, config=guildcfg.effective(stored))


def thread_airports(store: Store, interaction: discord.Interaction) -> list[str]:
    """Airports of the brief or alert post this command was typed in, if any."""
    channel = interaction.channel
    if interaction.guild_id is None or not isinstance(channel, discord.Thread):
        return []
    ctx = store.get_post_context(interaction.guild_id, channel.id)
    return ctx.airports if ctx else []


def resolve_stations(
    store: Store,
    interaction: discord.Interaction,
    typed: str | None,
    *,
    limit: int,
    fallback: list[str] | None = None,
) -> list[str]:
    """What the user typed; else the thread's airports; else the server's home airports."""
    if typed and typed.strip():
        return parse_stations(typed, limit=limit)
    for source in (thread_airports(store, interaction), fallback or []):
        if source:
            return source[:limit]
    raise StationInputError(
        "Which airport? e.g. `KBJC`. (A server admin can set home airports so this works without one.)"
    )
