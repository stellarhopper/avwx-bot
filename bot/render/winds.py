"""Winds-aloft table: a section of the brief, and the /winds card."""

from __future__ import annotations

import math

from ..sources.windsaloft import Wind
from ..wx import fmt
from . import theme
from .theme import BORDER, FAINT, MUTED, PANEL_HI, TEXT, WARN, Canvas, arrowhead

LOW_LEVELS = (3000, 6000, 9000, 12000, 18000, 24000)
ALL_LEVELS = (3000, 6000, 9000, 12000, 18000, 24000, 30000, 34000, 39000)


def _level_label(ft: int) -> str:
    return f"{ft // 1000}k" if ft < 18000 else f"FL{ft // 100}"


def _speed_color(kt: int):
    if kt >= 50:
        return theme.DANGER
    if kt >= 30:
        return WARN
    return TEXT


def small_arrow(c: Canvas, cx: float, cy: float, direction: int | None, *, length=14, color=TEXT) -> None:
    if direction is None:
        c.circle(cx, cy, 3, outline=MUTED, width=1.5)
        return
    to = math.radians((direction + 180) % 360)
    dx, dy = math.sin(to) * length / 2, -math.cos(to) * length / 2
    tip = (cx + dx, cy + dy)
    tail = (cx - dx, cy - dy)
    neck, left, right = arrowhead(tip, to, 5, 0.5)
    c.line([tail, neck], fill=color, width=2)
    c.polygon([tip, left, right], fill=color)


def table(
    c: Canvas,
    x: float,
    y: float,
    w: float,
    levels: dict[int, Wind],
    *,
    units: fmt.Units,
    columns=LOW_LEVELS,
) -> float:
    """Draw the table at (x, y), `w` wide. Returns the y below it."""
    cols = [lvl for lvl in columns if lvl in levels] or list(columns)
    label_w = 52
    cw = (w - label_w) / max(1, len(cols))
    row_h = 24
    h = row_h * 4 + 6
    c.rect(x, y, w, h, fill=PANEL_HI, radius=8)
    rows = ("", "Wind", "", "Temp")
    for i, name in enumerate(rows):
        if name:
            c.text(x + 10, y + 6 + row_h * i + row_h / 2, name, style="semibold", size=10, fill=FAINT, anchor="lm")
    for j, lvl in enumerate(cols):
        cx = x + label_w + cw * j + cw / 2
        c.text(cx, y + 6 + row_h / 2, _level_label(lvl), style="semibold", size=10.5, fill=MUTED, anchor="mm")
        wind = levels.get(lvl)
        if wind is None:
            c.text(cx, y + 6 + row_h * 1.5, "—", size=11, fill=FAINT, anchor="mm")
            continue
        color = _speed_color(wind.speed_kt)
        small_arrow(c, cx, y + 6 + row_h * 1.5, wind.direction, color=color)
        text = "L/V" if wind.light_variable else f"{wind.direction:03d}/{wind.speed_kt}"
        c.text(cx, y + 6 + row_h * 2.5, text, style="mono-bold", size=10.5, fill=color, anchor="mm")
        temp = fmt.temperature(wind.temp_c, units) if wind.temp_c is not None else ""
        c.text(cx, y + 6 + row_h * 3.5, temp, size=10.5, fill=TEXT, anchor="mm")
    for j in range(1, len(cols)):
        lx = x + label_w + cw * j
        c.line([(lx, y + 8), (lx, y + h - 8)], fill=BORDER)
    return y + h


WIDTH = 560
PAD = 16


def winds_card(site: str, valid: str | None, use: str | None, levels: dict[int, Wind], *, units: fmt.Units) -> bytes:
    height = 60 + 2 * (24 * 4 + 6) + 30 + PAD
    c = Canvas(WIDTH, height)
    c.text(PAD, PAD - 2, site, style="bold", size=26)
    c.text(PAD + c.text_width(site, style="bold", size=26) + 10, PAD + 11, "Winds & temperatures aloft",
           size=12, fill=MUTED)
    c.text(WIDTH - PAD, PAD + 2, f"Valid {valid or '—'}", style="semibold", size=12, anchor="ra")
    c.text(WIDTH - PAD, PAD + 19, f"For use {use or '—'}", size=11, fill=MUTED, anchor="ra")
    y = table(c, PAD, 60, WIDTH - 2 * PAD, levels, units=units, columns=LOW_LEVELS)
    y = table(c, PAD, y + 10, WIDTH - 2 * PAD, levels, units=units, columns=ALL_LEVELS[6:])
    c.text(PAD, y + 12, "Arrows point where the wind blows to. Orange 30 kt+, red 50 kt+.", size=10.5, fill=FAINT)
    return c.png()


def winds_alt(site: str, levels: dict[int, Wind]) -> str:
    parts = []
    for lvl in ALL_LEVELS:
        w = levels.get(lvl)
        if w is None:
            continue
        wind = "light and variable" if w.light_variable else f"{w.direction:03d} at {w.speed_kt} kt"
        temp = f", {w.temp_c}°C" if w.temp_c is not None else ""
        parts.append(f"{_level_label(lvl)} {wind}{temp}")
    return f"Winds aloft at {site}: " + "; ".join(parts)

