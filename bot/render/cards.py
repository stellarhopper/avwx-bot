"""METAR and TAF cards."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import clock
from ..sources.awc import Metar, Taf, TafPeriod
from ..wx import fmt, runways
from ..wx.runways import RunwayEnd
from . import theme
from .theme import BORDER, FAINT, MUTED, PANEL, PANEL_HI, TEXT, WARN, Canvas

WIDTH = 560
PAD = 16


def _tile(c: Canvas, x, y, w, h, label: str, value: str, *, value_color=TEXT, note: str = "") -> None:
    c.rect(x, y, w, h, fill=PANEL_HI, radius=8)
    c.text(x + 10, y + 9, label.upper(), style="semibold", size=9.5, fill=FAINT)
    c.text(x + 10, y + 25, value, style="semibold", size=15, fill=value_color)
    if note:
        c.text(x + 10, y + 46, note, size=10, fill=MUTED)


def _header(c: Canvas, station: str, name: str, right_top: str, right_bottom: str) -> None:
    c.text(PAD, PAD - 2, station, style="bold", size=26)
    sw = c.text_width(station, style="bold", size=26)
    c.text(PAD + sw + 10, PAD + 11, name[:48], size=12, fill=MUTED)
    c.text(WIDTH - PAD, PAD + 2, right_top, style="semibold", size=12, fill=TEXT, anchor="ra")
    c.text(WIDTH - PAD, PAD + 19, right_bottom, size=11, fill=MUTED, anchor="ra")


def _raw_block(c: Canvas, y: float, raw: str) -> float:
    lines = c.wrap(raw, WIDTH - 2 * PAD - 20, style="mono", size=11)
    h = 14 + 16 * len(lines)
    c.rect(PAD, y, WIDTH - 2 * PAD, h, fill=PANEL, radius=8)
    for i, line in enumerate(lines):
        c.text(PAD + 10, y + 8 + 16 * i, line, style="mono", size=11, fill=MUTED)
    return y + h


def metar_card(
    m: Metar,
    *,
    units: fmt.Units,
    tz: ZoneInfo,
    now: datetime | None = None,
    runways: list[RunwayEnd] | None = None,
) -> bytes:
    now = now or clock.now()
    probe = Canvas(WIDTH, 10)
    raw_lines = len(probe.wrap(m.raw, WIDTH - 2 * PAD - 20, style="mono", size=11))
    wx_strip = 34 if m.weather else 0
    body = 172
    height = 60 + body + 12 + wx_strip + (14 + 16 * raw_lines) + PAD
    c = Canvas(WIDTH, height)
    color = theme.category_color(m.flight_category)
    c.rect(0, 0, 5, height, fill=color)

    age = m.age_minutes(now)
    _header(
        c,
        m.station,
        m.name,
        f"{m.kind} {fmt.zulu(m.observed)}",
        f"{fmt.local(m.observed, tz, '%H:%M %Z')} · {fmt.ago(m.observed, now)}",
    )
    if age is not None and age > 75:
        c.text(WIDTH - PAD, PAD + 34, "STALE", style="bold", size=10, fill=WARN, anchor="ra")

    top = 60
    # Wind rose on the left, with the runways under the arrow.
    c.rect(PAD, top, 150, body, fill=PANEL, radius=10)
    _wind_rose(c, PAD + 75, top + 64, 40, m, color, runways or [])
    c.text(PAD + 75, top + body - 34, fmt.wind(m.wind_dir, m.wind_speed, m.wind_gust, variable=m.wind_variable),
           style="semibold", size=12, anchor="mm")
    note, note_color = _runway_note(m, runways or [])
    if note:
        c.text(PAD + 75, top + body - 15, note, style="semibold", size=_fit(c, note, 136, 11), fill=note_color,
               anchor="mm")

    # A 3×2 grid of values to its right.
    gx = PAD + 150 + 10
    gap = 8
    tw = (WIDTH - PAD - gx - 2 * gap) / 3
    th = (body - gap) / 2
    cells = [
        ("Flight cat", m.flight_category or "—", color, ""),
        ("Visibility", fmt.visibility(m.visibility_sm, m.visibility_text), TEXT, ""),
        ("Ceiling", fmt.ceiling(m.ceiling_ft), TEXT, fmt.clouds(m.clouds)[:24]),
        ("Temp / Dew", f"{fmt.temperature(m.temp_c, units)} / {fmt.temperature(m.dewpoint_c, units)}", TEXT,
         f"spread {m.spread_c:.0f}°C" if m.spread_c is not None else ""),
        ("Altimeter", fmt.altimeter(m.altimeter_inhg, m.altimeter_hpa, units), TEXT, ""),
        ("Density alt", fmt.feet(m.density_altitude_ft), _da_color(m), _da_note(m)),
    ]
    for i, (label, value, vcolor, note) in enumerate(cells):
        col, row = i % 3, i // 3
        _tile(c, gx + col * (tw + gap), top + row * (th + gap), tw, th, label, value,
              value_color=vcolor, note=note)

    y = top + body + 12
    if m.weather:
        c.rect(PAD, y, WIDTH - 2 * PAD, 26, fill=PANEL_HI, radius=8)
        c.text(PAD + 10, y + 13, "WEATHER", style="semibold", size=9.5, fill=FAINT, anchor="lm")
        c.text(PAD + 80, y + 13, m.weather, style="mono-bold", size=12, fill=WARN, anchor="lm")
        y += wx_strip
    _raw_block(c, y, m.raw)
    return c.png()


def _wind_rose(c: Canvas, cx: float, cy: float, r: float, m: Metar, color, ends: list[RunwayEnd]) -> None:
    calm = not m.wind_speed and not m.wind_gust
    choice = runways.best(ends, None if m.wind_variable else m.wind_dir, m.wind_speed, m.wind_gust)
    labels: list[tuple[float, str, object]] = []
    if choice:
        # A runway end's number is painted at the end you land *from*, which is
        # the opposite side of the airport from where it points.
        labels.append(((choice.end.heading + 180) % 360, choice.end.name, TEXT))
        labels.append((choice.end.heading, choice.end.opposite, FAINT))
    near_north = any(min(b, 360 - b) < 25 for b, _, _ in labels)
    theme.compass(c, cx, cy, r, letters=() if near_north else ("N",))
    for e in ends:
        theme.runway(c, cx, cy, 2 * r - 12, e.heading, width=6, fill=PANEL_HI)
    if choice:
        theme.runway(c, cx, cy, 2 * r - 12, choice.end.heading, width=8, fill=MUTED)
    for bearing, name, fill in labels:
        rad = math.radians(bearing)
        c.text(cx + math.sin(rad) * (r + 11), cy - math.cos(rad) * (r + 10), name, style="semibold", size=9,
               fill=fill, anchor="mm")
    if not (m.wind_variable or calm) and m.wind_dir is not None:
        theme.arrow(c, cx, cy, r, m.wind_dir, color=color)


def _runway_note(m: Metar, ends: list[RunwayEnd]) -> tuple[str, object]:
    """Short components line under the rose: "30R  HW 8 · XW 6L G10"."""
    if not ends:
        return "", MUTED
    if not m.wind_speed and not m.wind_gust:
        return "", MUTED  # the wind line above already says Calm
    if m.wind_variable or m.wind_dir is None:
        return f"Variable · XW up to {m.wind_gust or m.wind_speed}", WARN
    choice = runways.best(ends, m.wind_dir, m.wind_speed, m.wind_gust)
    if choice is None:
        return "", MUTED
    head = round(choice.headwind)
    head_text = f"HW {head}" if head >= 0 else f"TW {-head}"
    cross = round(choice.crosswind)
    cross_text = f"XW {cross}{choice.cross_from}" if cross else "XW 0"
    if choice.gust_crosswind is not None and round(choice.gust_crosswind) > cross:
        cross_text += f" G{round(choice.gust_crosswind)}"
    worst = choice.gust_crosswind if choice.gust_crosswind is not None else choice.crosswind
    note_color = theme.DANGER if worst >= 20 or head < 0 else WARN if worst >= 12 else TEXT
    return f"{choice.end.name}  {head_text} · {cross_text}", note_color


def _fit(c: Canvas, text: str, width: float, size: float, *, style="semibold") -> float:
    """The largest size up to `size` at which `text` fits in `width`."""
    while size > 8 and c.text_width(text, style=style, size=size) > width:
        size -= 0.5
    return size


def _da_color(m: Metar):
    da, elev = m.density_altitude_ft, m.elevation_ft
    if da is None or elev is None:
        return TEXT
    if da - elev >= 3000:
        return theme.DANGER
    if da - elev >= 1500:
        return WARN
    return TEXT


def _da_note(m: Metar) -> str:
    if m.density_altitude_ft is None or m.elevation_ft is None:
        return ""
    delta = m.density_altitude_ft - m.elevation_ft
    sign = "+" if delta >= 0 else "−"
    return f"{sign}{abs(round(delta / 10) * 10):,.0f} vs field"


def metar_alt(m: Metar, units: fmt.Units, ends: list[RunwayEnd] | None = None) -> str:
    parts = [
        f"{m.station} {m.flight_category}",
        f"wind {fmt.wind(m.wind_dir, m.wind_speed, m.wind_gust, variable=m.wind_variable)}",
        f"visibility {fmt.visibility(m.visibility_sm, m.visibility_text)}",
        f"ceiling {fmt.ceiling(m.ceiling_ft)}",
        f"temperature {fmt.temperature(m.temp_c, units)}",
        f"altimeter {fmt.altimeter(m.altimeter_inhg, m.altimeter_hpa, units)}",
    ]
    if m.density_altitude_ft is not None:
        parts.append(f"density altitude {fmt.feet(m.density_altitude_ft)}")
    if m.weather:
        parts.append(f"weather {m.weather}")
    choice = runways.best(ends or [], None if m.wind_variable else m.wind_dir, m.wind_speed, m.wind_gust)
    if choice:
        parts.append(runways.describe(choice))
    return ", ".join(parts)


def _period_label(p: TafPeriod) -> str:
    start, end = p.start.strftime("%d/%H"), p.end.strftime("%d/%H")
    if p.probability:
        head = f"PROB{p.probability}"
        if p.change == "TEMPO":
            head += " TEMPO"
        return f"{head} {start}–{end}Z"
    if p.change == "TEMPO":
        return f"TEMPO {start}–{end}Z"
    if p.change == "BECMG":
        return f"BECMG {start}–{end}Z"
    if p.change == "FM":
        return f"FM {p.start.strftime('%d/%H%M')}Z"
    return f"{start}–{end}Z"


def _period_values(p: TafPeriod) -> str:
    bits = []
    if p.wind_speed is not None:
        bits.append(fmt.wind(p.wind_dir, p.wind_speed, p.wind_gust))
    if p.visibility_sm is not None:
        bits.append(fmt.visibility(p.visibility_sm, p.visibility_text))
    if p.clouds:
        bits.append(fmt.clouds(p.clouds))
    if p.weather:
        bits.append(p.weather)
    if p.wind_shear:
        bits.append(p.wind_shear)
    return "  ·  ".join(bits)


ROW_H = 24


def taf_card(t: Taf, *, tz: ZoneInfo, now: datetime | None = None) -> bytes:
    now = now or clock.now()
    probe = Canvas(WIDTH, 10)
    raw_lines = len(probe.wrap(t.raw, WIDTH - 2 * PAD - 20, style="mono", size=11))
    timeline_h = 98
    rows = len(t.periods)
    height = 60 + timeline_h + 10 + rows * ROW_H + 12 + (14 + 16 * raw_lines) + PAD
    c = Canvas(WIDTH, height)

    issued = f"Issued {fmt.zulu(t.issued)}"
    valid = f"Valid {fmt.zulu(t.valid_from)} → {fmt.zulu(t.valid_to)}"
    _header(c, t.station, t.name, issued, valid)

    top = 60
    c.rect(PAD, top, WIDTH - 2 * PAD, timeline_h, fill=PANEL, radius=10)
    if t.valid_from and t.valid_to and t.valid_to > t.valid_from:
        x0, x1 = PAD + 12, WIDTH - PAD - 12
        span = (t.valid_to - t.valid_from).total_seconds()

        def x_at(when: datetime) -> float:
            frac = (when - t.valid_from).total_seconds() / span
            return x0 + max(0.0, min(1.0, frac)) * (x1 - x0)

        bar_y, bar_h = top + 10, 20
        for p in t.periods:
            if p.is_conditional:
                continue
            c.rect(x_at(p.start), bar_y, max(1, x_at(p.end) - x_at(p.start)), bar_h,
                   fill=theme.category_color(p.flight_category))
        cond_y = bar_y + bar_h + 4
        for p in t.periods:
            if not p.is_conditional:
                continue
            col = theme.category_color(p.flight_category)
            c.rect(x_at(p.start), cond_y, max(2, x_at(p.end) - x_at(p.start)), 7, fill=col, radius=3)

        # Hour ticks every 3 h in Z, with local time underneath.
        tick = t.valid_from.replace(minute=0, second=0, microsecond=0)
        while tick.hour % 3:
            tick += timedelta(hours=1)
        while tick <= t.valid_to:
            x = x_at(tick)
            c.line([(x, cond_y + 10), (x, cond_y + 14)], fill=BORDER)
            c.text(x, cond_y + 16, tick.strftime("%HZ"), size=9, fill=MUTED, anchor="ma")
            c.text(x, cond_y + 28, tick.astimezone(tz).strftime("%-I%p").lower(), size=8.5, fill=FAINT, anchor="ma")
            tick += timedelta(hours=3)

        if t.valid_from <= now <= t.valid_to:
            nx = x_at(now)
            c.line([(nx, bar_y - 4), (nx, bar_y + bar_h + 4)], fill=TEXT, width=2)

        # Key: what the two bars are.
        ky = top + timeline_h - 13
        c.rect(x0, ky - 5, 14, 10, fill=MUTED)
        c.text(x0 + 20, ky, "Forecast", size=9.5, fill=MUTED, anchor="lm")
        kx = x0 + 20 + c.text_width("Forecast", size=9.5) + 18
        c.rect(kx, ky - 2, 14, 4, fill=MUTED, radius=2)
        c.text(kx + 20, ky, "TEMPO / PROB: temporary or possible conditions", size=9.5, fill=MUTED, anchor="lm")
        if t.valid_from <= now <= t.valid_to:
            c.text(x1, ky, "now", size=9.5, fill=MUTED, anchor="rm")
            lx = x1 - c.text_width("now", size=9.5) - 7
            c.line([(lx, ky - 6), (lx, ky + 6)], fill=TEXT, width=2)

    y = top + timeline_h + 10
    for p in t.periods:
        conditional = p.is_conditional
        label_color = MUTED if conditional else TEXT
        c.text(PAD + (14 if conditional else 0), y + ROW_H / 2, _period_label(p),
               style="mono-bold" if not conditional else "mono", size=10.5, fill=label_color, anchor="lm")
        pill_x = PAD + 168
        c.pill(pill_x, y + 3, p.flight_category, theme.category_color(p.flight_category), size=9.5)
        c.text(pill_x + 52, y + ROW_H / 2, _period_values(p)[:70], size=11,
               fill=label_color, anchor="lm")
        y += ROW_H

    _raw_block(c, y + 12, t.raw)
    return c.png()


def taf_alt(t: Taf) -> str:
    cats = [p.flight_category for p in t.periods if not p.is_conditional]
    worst = max(cats, key=lambda cat: ("VFR", "MVFR", "IFR", "LIFR").index(cat)) if cats else "unknown"
    return f"TAF for {t.station}: {len(t.periods)} periods, worst prevailing category {worst}. Raw text is in the message."
