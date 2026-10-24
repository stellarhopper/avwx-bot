"""The mountain board (/mountain) and its compact section in the brief."""

from __future__ import annotations

from ..mountain import STATUS_LABELS, MountainBoard, PassStatus
from ..wx import fmt
from . import theme, winds
from .theme import BORDER, DANGER, FAINT, MUTED, OK, PANEL, PANEL_HI, TEXT, WARN, Canvas

WIDTH = 720
PAD = 16
STATUS_COLORS = {"go": OK, "caution": WARN, "no-go": DANGER, "unknown": theme.UNKNOWN_CATEGORY}
WAVE_LABELS = {"low": "Low", "elevated": "Elevated", "high": "High", "unknown": "Unknown"}
RIDGE_LEVELS = (9000, 12000, 18000)


def wave_banner(c: Canvas, y: float, board: MountainBoard, *, compact: bool = False) -> float:
    color = theme.RISK_COLORS.get(board.wave.level, theme.UNKNOWN_CATEGORY)
    reasons = board.wave.reasons[: 2 if compact else 4]
    lines = [ln for r in reasons for ln in c.wrap(r, WIDTH - 2 * PAD - 190, size=11)]
    h = max(46, 16 + 17 * len(lines))
    c.rect(PAD, y, WIDTH - 2 * PAD, h, fill=PANEL, radius=8)
    c.rect(PAD, y, 6, h, fill=color, radius=2)
    c.text(PAD + 18, y + 10, "MOUNTAIN WAVE RISK", style="bold", size=9.5, fill=FAINT)
    c.text(PAD + 18, y + 24, WAVE_LABELS[board.wave.level], style="bold", size=16, fill=color)
    for i, line in enumerate(lines):
        c.text(PAD + 180, y + 9 + 17 * i, line, size=11, fill=TEXT if i == 0 else MUTED)
    return y + h


def _pass_obs(p: PassStatus) -> str:
    m = p.on_pass
    if m is None:
        sides = [f"{s.station} {s.flight_category}" for s in p.sides.values() if s is not None]
        return "No station on the pass · " + (", ".join(sides) if sides else "no nearby reports")
    return "  ·  ".join([
        f"{m.station} {m.flight_category}",
        fmt.wind(m.wind_dir, m.wind_speed, m.wind_gust, variable=m.wind_variable),
        fmt.visibility(m.visibility_sm, m.visibility_text),
        fmt.ceiling(m.ceiling_ft) if m.ceiling_ft else "no ceiling",
    ])


def pass_rows(c: Canvas, y: float, passes: list[PassStatus]) -> float:
    row_h = 40
    h = 8 + row_h * len(passes)
    c.rect(PAD, y, WIDTH - 2 * PAD, h, fill=PANEL, radius=8)
    for i, p in enumerate(passes):
        ry = y + 4 + row_h * i
        if i:
            c.line([(PAD + 12, ry), (WIDTH - PAD - 12, ry)], fill=BORDER)
        color = STATUS_COLORS[p.status]
        c.circle(PAD + 20, ry + row_h / 2, 6, fill=color)
        c.text(PAD + 34, ry + 7, p.name, style="semibold", size=12)
        c.text(PAD + 34, ry + 23, f"{p.elevation_ft:,} ft", size=10, fill=FAINT)
        c.text(PAD + 210, ry + 7, _pass_obs(p)[:72], size=10.5, fill=TEXT)
        why = p.why()
        note = STATUS_LABELS[p.status] + (": " + "; ".join(why) if why else "")
        c.text(PAD + 210, ry + 23, note[:80], size=10, fill=color if p.status != "go" else MUTED)
        if p.ridge_wind and not p.ridge_wind.light_variable:
            w = p.ridge_wind
            c.text(WIDTH - PAD - 12, ry + 7, f"{w.direction:03d}/{w.speed_kt}", style="mono-bold", size=10.5,
                   fill=color if w.speed_kt >= 15 else MUTED, anchor="ra")
            c.text(WIDTH - PAD - 12, ry + 23, f"at {w.altitude_ft // 1000}k", size=9.5, fill=FAINT, anchor="ra")
    return y + h


def ridge_table(c: Canvas, y: float, board: MountainBoard, units) -> float:
    sites = list(board.ridge_winds.items())
    if not sites:
        return y
    col_w = (WIDTH - 2 * PAD) / len(sites)
    h = 26 + 22 * len(RIDGE_LEVELS)
    c.rect(PAD, y, WIDTH - 2 * PAD, h, fill=PANEL_HI, radius=8)
    for j, (site, levels) in enumerate(sites):
        x = PAD + col_w * j
        c.text(x + col_w / 2, y + 13, site, style="semibold", size=11, fill=MUTED, anchor="mm")
        for i, alt in enumerate(RIDGE_LEVELS):
            ry = y + 26 + 22 * i + 11
            w = levels.get(alt)
            c.text(x + 14, ry, f"{alt // 1000}k", size=10, fill=FAINT, anchor="lm")
            if w is None:
                c.text(x + col_w / 2 + 10, ry, "—", size=10.5, fill=FAINT, anchor="mm")
                continue
            color = DANGER if w.speed_kt >= 40 else WARN if w.speed_kt >= 25 else TEXT
            winds.small_arrow(c, x + 52, ry, w.direction, length=12, color=color)
            text = "L/V" if w.light_variable else f"{w.direction:03d}/{w.speed_kt}"
            c.text(x + 66, ry, text, style="mono-bold", size=10.5, fill=color, anchor="lm")
        if j:
            c.line([(x, y + 6), (x, y + h - 6)], fill=BORDER)
    return y + h


def density_row(c: Canvas, y: float, board: MountainBoard, units) -> float:
    items = [d for d in board.density if d.density_alt_ft is not None]
    if not items:
        return y
    per_row = 4
    cell_w = (WIDTH - 2 * PAD - (per_row - 1) * 8) / per_row
    rows = (len(items) + per_row - 1) // per_row
    for i, d in enumerate(items):
        col, row = i % per_row, i // per_row
        x = PAD + col * (cell_w + 8)
        ry = y + row * 52
        delta = d.density_alt_ft - d.elevation_ft
        color = DANGER if delta >= 3000 else WARN if delta >= 1500 else TEXT
        c.rect(x, ry, cell_w, 44, fill=PANEL, radius=8)
        c.text(x + 10, ry + 7, d.station, style="semibold", size=10.5, fill=MUTED)
        c.text(x + cell_w - 10, ry + 7, fmt.temperature(d.temp_c, units), size=10, fill=FAINT, anchor="ra")
        c.text(x + 10, ry + 22, f"DA {fmt.feet(d.density_alt_ft)}", style="semibold", size=12, fill=color)
    return y + rows * 52 - 8


def section_title(c: Canvas, y: float, title: str, note: str = "") -> float:
    c.text(PAD, y, title.upper(), style="bold", size=11, fill=MUTED)
    if note:
        c.text(WIDTH - PAD, y, note, size=10.5, fill=FAINT, anchor="ra")
    return y + 20


def render(board: MountainBoard, *, tz, units) -> bytes:
    c = Canvas(WIDTH, 4000)
    c.text(PAD, PAD, "Mountain flying", style="bold", size=22)
    c.text(PAD, PAD + 30, f"{board.region_name} · {board.generated.astimezone(tz).strftime('%a %-d %b %H:%M %Z')}",
           size=12, fill=MUTED)
    y = PAD + 58
    y = wave_banner(c, y, board) + 16
    y = section_title(c, y, "Passes", "status · on-pass observation · wind at pass level")
    y = pass_rows(c, y, board.passes) + 16
    if board.ridge_winds:
        y = section_title(c, y, "Ridge-level winds", "forecast, kt")
        y = ridge_table(c, y, board, units) + 16
    if board.density:
        y = section_title(c, y, "Density altitude", "mountain airports, now")
        y = density_row(c, y, board, units) + 16
    for problem in board.problems:
        c.text(PAD, y, f"⚠ {problem}", size=10.5, fill=WARN)
        y += 16
    c.line([(PAD, y + 4), (WIDTH - PAD, y + 4)], fill=BORDER)
    c.text(PAD, y + 12, "Heuristics for awareness, not a go/no-go decision. Check the cams and get a briefing.",
           size=10, fill=FAINT)
    y += 34
    c.image = c.image.crop((0, 0, c.image.width, round(y * theme.SCALE)))
    return c.png()


def section(c: Canvas, y: float, board: MountainBoard, data) -> float:
    """The brief's compact version: wave risk, then passes as status chips."""
    y = section_title(c, y, "Mountain flying", board.region_name)
    y = wave_banner(c, y, board, compact=True) + 8
    per_row = 3
    chip_w = (WIDTH - 2 * PAD - (per_row - 1) * 8) / per_row
    for i, p in enumerate(board.passes):
        col, row = i % per_row, i // per_row
        x = PAD + col * (chip_w + 8)
        cy = y + row * 34
        color = STATUS_COLORS[p.status]
        c.rect(x, cy, chip_w, 28, fill=PANEL, radius=8)
        c.circle(x + 14, cy + 14, 5, fill=color)
        c.text(x + 26, cy + 14, p.name, style="semibold", size=10.5, anchor="lm")
        c.text(x + chip_w - 10, cy + 14, STATUS_LABELS[p.status], size=10, fill=color, anchor="rm")
    rows = (len(board.passes) + per_row - 1) // per_row
    return y + rows * 34 - 6


def board_alt(board: MountainBoard) -> str:
    parts = [f"Mountain wave risk {board.wave.level}: {'; '.join(board.wave.reasons)}"]
    for p in board.passes:
        parts.append(f"{p.name} {STATUS_LABELS[p.status]}")
    return ". ".join(parts)
