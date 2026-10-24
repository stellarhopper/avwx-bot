"""Deciding what counts as an alert, as pure functions over fetched data.

Each hazard becomes a `Candidate` with a stable **key**, which decides whether a
later poll updates an earlier post or makes a new one:

* NWS warnings key on their VTEC event number (office, phenomenon,
  significance, event number). That number stays the same through every
  update to one storm's warning, while the alert's own ID changes each time.
* Convective SIGMETs are reissued with new numbers every hour. They share one
  key per server while any are active, so a line of storms is one post that
  updates rather than a new post an hour.
* CWAs work the same way.
* METAR triggers key per station and kind: thunderstorm at KBJC, gusts at KAPA.

The fingerprint is what the post shows. When it changes, the post is edited.
When a key stops appearing, its post is marked as ended.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from . import hazards as hz
from .sources.awc import Hazard, Metar
from .sources.nws import Alert

_VTEC = re.compile(
    r"/[OTEX]\.(?P<action>[A-Z]{3})\.(?P<office>[A-Z]{4})\.(?P<phen>[A-Z]{2})\."
    r"(?P<sig>[A-Z])\.(?P<etn>\d{4})\."
)
ENDED_ACTIONS = {"CAN", "EXP"}

KIND_NWS = "nws"
KIND_CONVECTIVE = "convective"
KIND_CWA = "cwa"
KIND_METAR = "metar"

# NWS warnings aren't held back by the cooldown: each is a distinct, issued
# warning, and a second tornado warning ten minutes after the first is news.
COOLDOWN_KINDS = {KIND_CONVECTIVE, KIND_CWA, KIND_METAR}


@dataclass(frozen=True, slots=True)
class Candidate:
    key: str
    kind: str
    title: str
    lines: list[str]
    until: datetime | None
    severity: int  # 3 = most severe
    stations: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def fingerprint(self) -> str:
        text = "\n".join([self.title, *self.lines, self.until.isoformat() if self.until else "", self.raw])
        return hashlib.sha1(text.encode()).hexdigest()[:16]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def vtec_key(alert: Alert) -> tuple[str | None, bool]:
    """(stable key, ended?) from the alert's VTEC code, or (None, False) if it has none."""
    for code in alert.vtec:
        m = _VTEC.search(code)
        if m:
            key = f"vtec:{m['office']}.{m['phen']}.{m['sig']}.{m['etn']}"
            return key, m["action"] in ENDED_ACTIONS
    return None, False


def nws_candidates(alerts: list[Alert], events: list[str], now: datetime) -> list[Candidate]:
    wanted = {e.lower() for e in events}
    out: dict[str, Candidate] = {}
    for a in alerts:
        if a.event.lower() not in wanted or a.message_type == "Cancel":
            continue
        if a.urgency not in ("Immediate", "Expected"):
            continue
        key, ended = vtec_key(a)
        if ended:
            continue
        if a.until and a.until < now:
            continue
        lines = []
        if a.nws_headline:
            lines.append(a.nws_headline.capitalize() if a.nws_headline.isupper() else a.nws_headline)
        details = []
        if a.max_gust:
            details.append(f"gusts {a.max_gust}")
        if a.max_hail and a.max_hail not in ("0.00", "0"):
            details.append(f"hail {a.max_hail} in")
        if details:
            lines.append(", ".join(details).capitalize())
        if a.area:
            lines.append(a.area)
        severity = 3 if a.severity in ("Extreme", "Severe") or "Warning" in a.event else 2
        cand = Candidate(
            key=key or f"nws:{a.id}",
            kind=KIND_NWS,
            title=a.event,
            lines=lines,
            until=a.until,
            severity=severity,
            raw=_clip(a.description, 600),
        )
        # The feed can carry an old and a new version of one warning at once;
        # keep the most recent.
        prev = out.get(cand.key)
        if prev is None or (a.until and prev.until and a.until > prev.until):
            out[cand.key] = cand
    return list(out.values())


def area_candidates(hazards: list[Hazard], *, convective: bool, cwa: bool) -> list[Candidate]:
    out = []
    groups = []
    if convective:
        groups.append((KIND_CONVECTIVE, "Convective SIGMET", [h for h in hazards if h.source == "CONVECTIVE"]))
    if cwa:
        groups.append((KIND_CWA, "Center Weather Advisory", [h for h in hazards if h.source == "CWA"]))
    for kind, title, items in groups:
        if not items:
            continue
        lines = []
        for h in sorted(items, key=lambda h: h.ident):
            bits = [h.ident, hz.detail(h)]
            lines.append(" · ".join(b for b in bits if b))
        until = max((h.valid_to for h in items if h.valid_to), default=None)
        out.append(
            Candidate(
                key=kind,
                kind=kind,
                title=title if len(items) == 1 else f"{title}s ({len(items)})",
                lines=lines,
                until=until,
                severity=3 if kind == KIND_CONVECTIVE else 2,
                raw=_clip("\n\n".join(h.raw for h in items if h.raw), 900),
            )
        )
    return out


def metar_candidates(
    metars: list[Metar], *, thunderstorm: bool, gust_kt: int, funnel: bool, now: datetime
) -> list[Candidate]:
    out = []
    for m in metars:
        age = m.age_minutes(now)
        if age is None or age > 90:
            continue  # a stale report isn't news
        wx = m.weather.upper()
        if funnel and "FC" in wx:
            what = "Tornado" if "+FC" in wx else "Funnel cloud"
            out.append(Candidate(f"metar-fc:{m.station}", KIND_METAR, f"{what} reported at {m.station}",
                                 [m.name] if m.name else [], None, 3, [m.station], m.raw))
        if thunderstorm and "TS" in wx:
            where = "in the vicinity of" if "VCTS" in wx else "at"
            out.append(Candidate(f"metar-ts:{m.station}", KIND_METAR, f"Thunderstorm {where} {m.station}",
                                 [m.name] if m.name else [], None, 2, [m.station], m.raw))
        if gust_kt and m.wind_gust and m.wind_gust >= gust_kt:
            out.append(Candidate(f"metar-gust:{m.station}", KIND_METAR,
                                 f"Gusts to {m.wind_gust} kt at {m.station}",
                                 [m.name] if m.name else [], None, 2, [m.station], m.raw))
    return out


def in_quiet_hours(local: datetime, quiet: dict | None) -> bool:
    """True inside [start, end), which may wrap past midnight (22:00–06:00)."""
    if not quiet:
        return False
    start = time.fromisoformat(quiet["start"])
    end = time.fromisoformat(quiet["end"])
    now = local.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def cooled_down(last_posted: int | None, now: datetime, cooldown_min: int) -> bool:
    if last_posted is None or cooldown_min <= 0:
        return True
    return now.timestamp() - last_posted >= cooldown_min * 60


SEVERITY_EMOJI = {3: "🟥", 2: "🟧", 1: "🟨"}


def message_text(c: Candidate, tz, *, ping_role: int | None, ended: bool = False, updated: datetime | None = None) -> str:
    emoji = "✅" if ended else SEVERITY_EMOJI.get(c.severity, "🟨")
    until = f" · until {c.until.astimezone(tz).strftime('%H:%M %Z')}" if c.until and not ended else ""
    head = f"{emoji} **{c.title}**{until}"
    if ended:
        head = f"{emoji} ~~{c.title}~~ — ended"
    parts = [head]
    if ping_role and not ended:
        parts.append(f"<@&{ping_role}>")
    if not ended:
        parts.extend(f"> {line}" for line in c.lines[:4])
    if updated and not ended:
        parts.append(f"-# updated {updated.astimezone(tz).strftime('%H:%M')}")
    return "\n".join(parts)[:2000]


def thread_title(c: Candidate, tz) -> str:
    until = f", until {c.until.astimezone(tz).strftime('%H:%M')}" if c.until else ""
    return f"⚠ {c.title}{until}"


def expired(until_ts: int | None, now: datetime, grace: timedelta = timedelta(minutes=5)) -> bool:
    return until_ts is not None and now.timestamp() > until_ts + grace.total_seconds()
