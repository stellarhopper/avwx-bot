"""The alert card: what, until when, and the details, in one glance."""

from __future__ import annotations

from datetime import datetime

from ..alerting import Candidate
from .theme import ACCENT, DANGER, MUTED, PANEL, TEXT, WARN, Canvas

WIDTH = 560
PAD = 16
SEVERITY_COLORS = {3: DANGER, 2: WARN, 1: ACCENT}


def alert_card(c: Candidate, *, tz, now: datetime) -> bytes:
    color = SEVERITY_COLORS.get(c.severity, ACCENT)
    probe = Canvas(WIDTH, 10)
    title_lines = probe.wrap(c.title, WIDTH - 2 * PAD - 20, style="bold", size=20)[:2]
    detail = [ln for line in c.lines[:5] for ln in probe.wrap(line, WIDTH - 2 * PAD - 20, size=12)][:8]
    # Reports (METAR, SIGMET) are codes and read best monospaced; NWS text is prose.
    raw_style = "regular" if c.kind == "nws" else "mono"
    raw = probe.wrap(" ".join(c.raw.split()), WIDTH - 2 * PAD - 24, style=raw_style, size=10.5)[:10] if c.raw else []
    height = 50 + 26 * len(title_lines) + 4 + 18 * len(detail) + (16 + 16 * len(raw) if raw else 0) + PAD

    cv = Canvas(WIDTH, height)
    cv.rect(0, 0, WIDTH, 40, fill=color)
    label = {3: "SEVERE WEATHER", 2: "WEATHER HAZARD", 1: "ADVISORY"}.get(c.severity, "ADVISORY")
    cv.text(PAD, 20, label, style="bold", size=12, fill=(20, 20, 24), anchor="lm")
    if c.until:
        cv.text(WIDTH - PAD, 20, f"until {c.until.astimezone(tz).strftime('%H:%M %Z')}", style="bold", size=12,
                fill=(20, 20, 24), anchor="rm")
    else:
        cv.text(WIDTH - PAD, 20, now.astimezone(tz).strftime("observed %H:%M %Z"), style="bold", size=12,
                fill=(20, 20, 24), anchor="rm")

    y = 50
    for line in title_lines:
        cv.text(PAD, y, line, style="bold", size=20, fill=TEXT)
        y += 26
    y += 4
    for line in detail:
        cv.text(PAD, y, line, size=12, fill=MUTED)
        y += 18
    if raw:
        y += 6
        h = 10 + 16 * len(raw)
        cv.rect(PAD, y, WIDTH - 2 * PAD, h, fill=PANEL, radius=8)
        for i, line in enumerate(raw):
            cv.text(PAD + 10, y + 6 + 16 * i, line, style=raw_style, size=10.5, fill=MUTED)
    return cv.png()


def alert_alt(c: Candidate, tz) -> str:
    until = f", until {c.until.astimezone(tz).strftime('%H:%M %Z')}" if c.until else ""
    return f"{c.title}{until}. " + " ".join(c.lines[:4])
