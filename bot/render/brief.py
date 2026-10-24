"""The brief dashboard: airports, hazards, alerts, winds, PIREPs, discussion."""

from __future__ import annotations

from datetime import datetime, timedelta

from .. import hazards as hz
from ..briefing import AirportBrief, BriefData, notable_pireps
from ..sources.awc import Taf
from ..wx import flightcat, fmt
from . import theme, winds
from .theme import ACCENT, BORDER, DANGER, FAINT, MUTED, PANEL, TEXT, WARN, Canvas

WIDTH = 720
PAD = 16
GAP = 12
MAX_HEIGHT = 6000
TAF_HOURS = 12

WEIGHT_COLORS = {3: DANGER, 2: WARN, 1: ACCENT}


def section_title(c: Canvas, y: float, title: str, note: str = "") -> float:
    c.text(PAD, y, title.upper(), style="bold", size=11, fill=MUTED)
    if note:
        c.text(WIDTH - PAD, y, note, size=10.5, fill=FAINT, anchor="ra")
    return y + 20


def empty_line(c: Canvas, y: float, text: str) -> float:
    c.rect(PAD, y, WIDTH - 2 * PAD, 30, fill=PANEL, radius=8)
    c.text(PAD + 12, y + 15, text, size=11.5, fill=MUTED, anchor="lm")
    return y + 30


def _taf_strip(c: Canvas, x: float, y: float, w: float, taf: Taf | None, now: datetime) -> None:
    """Hour-by-hour category for the next TAF_HOURS: prevailing on top, TEMPO/PROB beneath."""
    if taf is None:
        c.text(x, y + 7, "No TAF", size=9.5, fill=FAINT, anchor="lm")
        return
    seg = w / TAF_HOURS
    start = now.replace(minute=0, second=0, microsecond=0)
    for i in range(TAF_HOURS):
        when = start + timedelta(hours=i, minutes=30)
        base = taf.prevailing_at(when)
        color = theme.category_color(base.flight_category) if base else BORDER
        c.rect(x + i * seg, y, seg - 1, 9, fill=color)
        cond = taf.conditional_at(when)
        if cond:
            worst = flightcat.worst(p.flight_category for p in cond)
            c.rect(x + i * seg, y + 11, seg - 1, 3, fill=theme.category_color(worst))


def airport_tile(c: Canvas, x: float, y: float, w: float, h: float, a: AirportBrief, data: BriefData) -> None:
    m = a.metar
    cat = m.flight_category if m else None
    color = theme.category_color(cat)
    c.rect(x, y, w, h, fill=PANEL, radius=10)
    c.rect(x, y, 5, h, fill=color, radius=2)
    c.text(x + 16, y + 10, a.station, style="bold", size=17)
    if m:
        c.pill(x + w - 10, y + 10, cat or "?", color, size=10, anchor="right")
        line1 = "  ·  ".join([
            fmt.wind(m.wind_dir, m.wind_speed, m.wind_gust, variable=m.wind_variable),
            fmt.visibility(m.visibility_sm, m.visibility_text),
            "Ceiling " + fmt.ceiling(m.ceiling_ft) if m.ceiling_ft else "No ceiling",
        ])
        c.text(x + 16, y + 38, line1, style="semibold", size=11.5)
        line2 = "  ·  ".join(filter(None, [
            f"{fmt.temperature(m.temp_c, data.units)}/{fmt.temperature(m.dewpoint_c, data.units)}",
            fmt.altimeter(m.altimeter_inhg, m.altimeter_hpa, data.units),
            f"DA {fmt.feet(m.density_altitude_ft)}" if m.density_altitude_ft is not None else None,
        ]))
        c.text(x + 16, y + 56, line2, size=10.5, fill=MUTED)
        if m.weather:
            c.text(x + w - 10, y + 56, m.weather, style="mono-bold", size=10.5, fill=WARN, anchor="ra")
        age = m.age_minutes(data.generated)
        if age is not None and age > 75:
            c.text(x + w - 10, y + 38, "STALE", style="bold", size=9.5, fill=WARN, anchor="ra")
    else:
        c.text(x + 16, y + 40, "No current METAR", size=11.5, fill=MUTED)
    c.text(x + 16, y + h - 13, f"TAF next {TAF_HOURS} h", size=8.5, fill=FAINT, anchor="lm")
    _taf_strip(c, x + 92, y + h - 20, w - 92 - 12, a.taf, data.generated)


def _until(when: datetime | None, tz) -> str:
    if when is None:
        return ""
    return "until " + when.astimezone(tz).strftime("%H:%M")


def hazards_section(c: Canvas, y: float, data: BriefData, radius: int) -> float:
    y = section_title(c, y, "AIRMETs, SIGMETs & CWAs", f"within {radius} nm, next 6 h")
    if not data.hazards:
        return empty_line(c, y, "None affecting these airports.")
    rows = data.hazards[:8]
    h = 12 + 26 * len(rows)
    c.rect(PAD, y, WIDTH - 2 * PAD, h, fill=PANEL, radius=8)
    for i, haz in enumerate(rows):
        ry = y + 6 + 26 * i + 13
        c.rect(PAD + 12, ry - 5, 10, 10, fill=WEIGHT_COLORS.get(hz.weight(haz), ACCENT), radius=2)
        c.text(PAD + 30, ry, hz.label(haz), style="semibold", size=11.5, anchor="lm")
        lw = c.text_width(hz.label(haz), style="semibold", size=11.5)
        c.text(PAD + 30 + lw + 10, ry, hz.detail(haz)[:60], size=10.5, fill=MUTED, anchor="lm")
        c.text(WIDTH - PAD - 12, ry, _until(haz.valid_to, data.tz), size=10.5, fill=MUTED, anchor="rm")
    if len(data.hazards) > len(rows):
        c.text(WIDTH - PAD, y + h + 4, f"+{len(data.hazards) - len(rows)} more", size=10, fill=FAINT, anchor="ra")
        h += 16
    return y + h


def alerts_section(c: Canvas, y: float, data: BriefData) -> float:
    y = section_title(c, y, "NWS watches & warnings")
    if not data.alerts:
        return empty_line(c, y, "None in effect nearby.")
    rows = data.alerts[:6]
    h = 12 + 34 * len(rows)
    c.rect(PAD, y, WIDTH - 2 * PAD, h, fill=PANEL, radius=8)
    for i, a in enumerate(rows):
        ry = y + 6 + 34 * i
        color = DANGER if "Warning" in a.event else WARN if "Watch" in a.event else ACCENT
        c.text(PAD + 12, ry + 3, a.event, style="semibold", size=11.5, fill=color)
        c.text(WIDTH - PAD - 12, ry + 3, _until(a.until, data.tz), size=10.5, fill=MUTED, anchor="ra")
        c.text(PAD + 12, ry + 19, a.area[:100], size=10, fill=MUTED)
    return y + h


def pireps_section(c: Canvas, y: float, data: BriefData) -> float:
    reps = data.pireps or []
    turb = [p for p in reps if p.turbulence and p.turbulence not in ("NEG",)]
    modplus = [p for p in turb if "MOD" in p.turbulence or "SEV" in p.turbulence]
    icing = [p for p in reps if p.icing and p.icing not in ("NEG",)]
    note = f"{len(reps)} in last 3 h, 100 nm"
    y = section_title(c, y, "PIREPs", note)
    if not reps:
        return empty_line(c, y, "No reports.")
    summary = f"Turbulence {len(turb)} ({len(modplus)} moderate+)  ·  Icing {len(icing)}"
    notable = notable_pireps(reps)[:4]
    lines = [c.wrap(p.raw, WIDTH - 2 * PAD - 24, style="mono", size=10)[0] for p in notable]
    h = 34 + 17 * len(lines)
    c.rect(PAD, y, WIDTH - 2 * PAD, h, fill=PANEL, radius=8)
    c.text(PAD + 12, y + 17, summary, style="semibold", size=11.5, anchor="lm")
    for i, line in enumerate(lines):
        c.text(PAD + 12, y + 34 + 17 * i, line, style="mono", size=10, fill=MUTED)
    return y + h


def afd_section(c: Canvas, y: float, data: BriefData, max_lines: int = 12) -> float:
    afd = data.afd
    issued = f"NWS {afd.office}, issued {afd.issued.astimezone(data.tz).strftime('%a %H:%M')}" if afd and afd.issued else ""
    y = section_title(c, y, "Forecast discussion — aviation", issued)
    if afd is None:
        return empty_line(c, y, "Not available.")
    lines = c.wrap(" ".join(afd.aviation.split()), WIDTH - 2 * PAD - 24, size=11)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".") + " …"
    h = 14 + 17 * len(lines)
    c.rect(PAD, y, WIDTH - 2 * PAD, h, fill=PANEL, radius=8)
    for i, line in enumerate(lines):
        c.text(PAD + 12, y + 8 + 17 * i, line, size=11, fill=TEXT)
    return y + h


def render(data: BriefData, *, radius: int = 30) -> bytes:
    return theme.compose(WIDTH, lambda c: _draw(c, data, radius), max_height=MAX_HEIGHT)


def _draw(c: Canvas, data: BriefData, radius: int) -> float:
    local = data.generated.astimezone(data.tz)
    c.text(PAD, PAD, data.title, style="bold", size=22)
    c.text(PAD, PAD + 30, local.strftime("%A %-d %B %Y · %H:%M %Z"), size=12, fill=MUTED)
    c.text(WIDTH - PAD, PAD + 4, data.generated.strftime("%d %H%MZ"), style="mono-bold", size=12, fill=MUTED, anchor="ra")
    y = PAD + 58

    # Airports, two per row.
    tile_w = (WIDTH - 2 * PAD - GAP) / 2
    tile_h = 104
    for i, a in enumerate(data.airports):
        col, row = i % 2, i // 2
        airport_tile(c, PAD + col * (tile_w + GAP), y + row * (tile_h + GAP), tile_w, tile_h, a, data)
    rows = (len(data.airports) + 1) // 2
    y += rows * (tile_h + GAP) + 8

    if data.hazards is not None:
        y = hazards_section(c, y, data, radius) + 18
    if data.alerts is not None:
        y = alerts_section(c, y, data) + 18
    if data.winds is not None:
        y = section_title(c, y, f"Winds aloft — {data.winds.site}", f"valid {data.winds.valid or '—'}, use {data.winds.use or '—'}")
        y = winds.table(c, PAD, y, WIDTH - 2 * PAD, data.winds.levels, units=data.units) + 18
    if data.mountain is not None:
        from . import mountain as mountain_render

        y = mountain_render.section(c, y, data.mountain, data) + 18
    if data.pireps is not None:
        y = pireps_section(c, y, data) + 18
    if data.afd is not None or "Forecast discussion" in " ".join(data.problems):
        y = afd_section(c, y, data) + 18

    for problem in data.problems:
        c.text(PAD, y, f"⚠ {problem}", size=10.5, fill=WARN)
        y += 16
    c.line([(PAD, y + 4), (WIDTH - PAD, y + 4)], fill=BORDER)
    c.text(PAD, y + 12, "aviationweather.gov · weather.gov — for awareness, not an official briefing", size=10, fill=FAINT)
    return y + 34


def summary_line(data: BriefData) -> str:
    """One line for the message text: each airport's category, plus hazard counts."""
    cats = []
    for a in data.airports:
        cat = a.metar.flight_category if a.metar else "—"
        cats.append(f"{a.station} {cat}")
    bits = [" · ".join(cats)]
    if data.hazards:
        bits.append(f"{len(data.hazards)} area hazard{'s' if len(data.hazards) != 1 else ''}")
    if data.alerts:
        bits.append(f"{len(data.alerts)} NWS alert{'s' if len(data.alerts) != 1 else ''}")
    return "  |  ".join(bits)


def alt_text(data: BriefData) -> str:
    parts = [data.title]
    for a in data.airports:
        if a.metar:
            m = a.metar
            parts.append(
                f"{a.station} {m.flight_category}, wind {fmt.wind(m.wind_dir, m.wind_speed, m.wind_gust, variable=m.wind_variable)}, "
                f"visibility {fmt.visibility(m.visibility_sm, m.visibility_text)}, ceiling {fmt.ceiling(m.ceiling_ft)}"
            )
    if data.hazards is not None:
        parts.append("Hazards: " + (", ".join(hz.label(h) for h in data.hazards) or "none"))
    if data.alerts is not None:
        parts.append("NWS alerts: " + (", ".join(a.event for a in data.alerts) or "none"))
    return ". ".join(parts)

