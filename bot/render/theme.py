"""Shared drawing vocabulary: colours, fonts, and the few primitives every card uses.

Renderers lay out in logical pixels, so their numbers read like CSS. The PNG
comes out at 3× (SCALE), sharp when Discord shows it full size on a
high-density screen.

Pillow's shape drawing has no anti-aliasing, so lines, circles and polygons
drawn straight at 3× come out jagged. Everything is therefore drawn at twice
the output size (SUPERSAMPLE) and scaled down with a Lanczos filter on the way
out, which smooths every edge.

The palette is dark to sit on Discord's dark theme without a glaring white
block, and the flight-category colours follow the convention pilots already
read on every sectional-chart overlay: VFR green, MVFR blue, IFR red, LIFR
magenta.
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
SCALE = 3  # output pixels per logical pixel
SUPERSAMPLE = 2
DRAW_SCALE = SCALE * SUPERSAMPLE

BG = (24, 26, 31)
PANEL = (34, 37, 44)
PANEL_HI = (43, 47, 56)
BORDER = (56, 61, 72)
TEXT = (236, 238, 242)
MUTED = (160, 166, 178)
FAINT = (110, 116, 128)
ACCENT = (110, 168, 254)
WARN = (250, 176, 5)
DANGER = (240, 82, 82)
OK = (64, 192, 87)

CATEGORY_COLORS = {
    "VFR": (47, 179, 68),
    "MVFR": (51, 132, 245),
    "IFR": (230, 57, 70),
    "LIFR": (204, 61, 204),
}
UNKNOWN_CATEGORY = (100, 106, 118)

RISK_COLORS = {
    "low": OK,
    "elevated": WARN,
    "high": DANGER,
    "unknown": UNKNOWN_CATEGORY,
}

_FONT_FILES = {
    "regular": "Inter-Regular.ttf",
    "semibold": "Inter-SemiBold.ttf",
    "bold": "Inter-Bold.ttf",
    "mono": "JetBrainsMono-Regular.ttf",
    "mono-bold": "JetBrainsMono-Bold.ttf",
}
# If the bundled fonts are ever missing, DejaVu (fonts-dejavu-core on the Pi)
# still gives legible output rather than Pillow's tiny bitmap default.
_FALLBACKS = {
    "regular": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "semibold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "mono": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "mono-bold": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
}


def category_color(cat: str | None) -> tuple[int, int, int]:
    return CATEGORY_COLORS.get(cat or "", UNKNOWN_CATEGORY)


@cache
def _font_px(style: str, px: int) -> ImageFont.FreeTypeFont:
    for path in (FONT_DIR / _FONT_FILES[style], Path(_FALLBACKS[style])):
        try:
            return ImageFont.truetype(str(path), px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def font(style: str, size: float, scale: float = DRAW_SCALE) -> ImageFont.FreeTypeFont:
    return _font_px(style, max(1, round(size * scale)))


class Canvas:
    """An image plus a draw handle that take logical coordinates."""

    def __init__(self, width: float, height: float, background=BG, *, scale: float = DRAW_SCALE) -> None:
        self.width = width
        self.height = height
        self.scale = scale
        self.image = Image.new("RGB", (round(width * scale), round(height * scale)), background)
        self.draw = ImageDraw.Draw(self.image)

    def _s(self, *values: float) -> list[int]:
        return [round(v * self.scale) for v in values]

    def _w(self, width: float) -> int:
        return max(1, round(width * self.scale))

    def rect(self, x, y, w, h, *, fill=None, outline=None, radius=0, width=1) -> None:
        box = self._s(x, y, x + w, y + h)
        self.draw.rounded_rectangle(box, radius=round(radius * self.scale), fill=fill, outline=outline, width=self._w(width))

    def line(self, points, *, fill=BORDER, width=1) -> None:
        self.draw.line([tuple(self._s(*p)) for p in points], fill=fill, width=self._w(width), joint="curve")

    def polygon(self, points, *, fill=None, outline=None) -> None:
        self.draw.polygon([tuple(self._s(*p)) for p in points], fill=fill, outline=outline)

    def circle(self, cx, cy, r, *, fill=None, outline=None, width=1) -> None:
        self.draw.ellipse(self._s(cx - r, cy - r, cx + r, cy + r), fill=fill, outline=outline, width=self._w(width))

    def text(self, x, y, value: str, *, style="regular", size=14, fill=TEXT, anchor="la") -> None:
        self.draw.text(tuple(self._s(x, y)), value, font=font(style, size, self.scale), fill=fill, anchor=anchor)

    def text_width(self, value: str, *, style="regular", size=14) -> float:
        # Always measured at the drawing scale, so word wrapping comes out the
        # same on a cheap measuring canvas as on the real one (see compose()).
        return self.draw.textlength(value, font=font(style, size)) / DRAW_SCALE

    def pill(self, x, y, label: str, color, *, size=12, text_color=(255, 255, 255), anchor="left") -> float:
        """A filled rounded badge. Returns its width."""
        pad = size * 0.65
        w = self.text_width(label, style="bold", size=size) + 2 * pad
        h = size * 1.75
        if anchor == "right":
            x -= w
        self.rect(x, y, w, h, fill=color, radius=h / 2)
        self.text(x + w / 2, y + h / 2, label, style="bold", size=size, fill=text_color, anchor="mm")
        return w

    def wrap(self, value: str, max_width: float, *, style="regular", size=14) -> list[str]:
        """Greedy word wrap to a logical width. Preserves blank-line paragraphs."""
        lines: list[str] = []
        for paragraph in value.split("\n"):
            words = paragraph.split()
            if not words:
                lines.append("")
                continue
            current = words[0]
            for word in words[1:]:
                trial = f"{current} {word}"
                if self.text_width(trial, style=style, size=size) <= max_width:
                    current = trial
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
        return lines

    def png(self) -> bytes:
        image = self.image
        if self.scale != SCALE:
            size = (round(self.width * SCALE), round(self.height * SCALE))
            image = image.resize(size, Image.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, format="PNG", compress_level=6)
        return buf.getvalue()


def compose(width: float, draw: Callable[[Canvas], float], *, max_height: float = 6000) -> bytes:
    """Render a card whose height isn't known until it's laid out.

    `draw` lays everything out and returns the height it used. It runs twice:
    once on a cheap 1× canvas to measure, then on a full-size one. Allocating
    the full-size canvas at the maximum height and cropping would cost
    gigabytes for a tall brief at 6×.
    """
    probe = Canvas(width, max_height, scale=1)
    height = min(max_height, math.ceil(draw(probe)))
    canvas = Canvas(width, height)
    draw(canvas)
    return canvas.png()


def compass(c: Canvas, cx: float, cy: float, radius: float, *, letters=("N", "E", "S", "W")) -> None:
    """The ring and tick marks a wind arrow sits on."""
    c.circle(cx, cy, radius, outline=BORDER, width=1.5)
    for deg, label in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        if label not in letters:
            continue
        rad = math.radians(deg)
        lx = cx + math.sin(rad) * (radius + 9)
        ly = cy - math.cos(rad) * (radius + 9)
        c.text(lx, ly, label, style="semibold", size=9, fill=FAINT, anchor="mm")
    for deg in range(0, 360, 30):
        rad = math.radians(deg)
        inner = radius - (5 if deg % 90 == 0 else 3)
        c.line(
            [
                (cx + math.sin(rad) * inner, cy - math.cos(rad) * inner),
                (cx + math.sin(rad) * radius, cy - math.cos(rad) * radius),
            ],
            fill=BORDER,
            width=1,
        )


def arrow(c: Canvas, cx: float, cy: float, radius: float, direction: int, *, color=TEXT) -> None:
    """An arrow pointing where the wind is going *to*.

    Wind is reported as where it comes *from*, so a 270° wind draws an arrow
    pointing east, the way it would push the aircraft.
    """
    to = math.radians((direction + 180) % 360)
    reach = radius - 6
    tip = (cx + math.sin(to) * reach, cy - math.cos(to) * reach)
    tail = (cx - math.sin(to) * reach, cy + math.cos(to) * reach)
    head = 12
    neck = (tip[0] - math.sin(to) * head * 0.8, tip[1] + math.cos(to) * head * 0.8)
    left = (tip[0] - math.sin(to - 0.42) * head, tip[1] + math.cos(to - 0.42) * head)
    right = (tip[0] - math.sin(to + 0.42) * head, tip[1] + math.cos(to + 0.42) * head)
    # A dark halo first, so the arrow stays legible where it crosses a runway.
    c.line([tail, neck], fill=BG, width=6)
    c.polygon([tip, left, right], fill=BG, outline=BG)
    c.line([tail, neck], fill=color, width=3)
    c.polygon([tip, left, right], fill=color)


def wind_arrow(c: Canvas, cx: float, cy: float, radius: float, direction: int | None, *, color=TEXT) -> None:
    compass(c, cx, cy, radius)
    if direction is not None:
        arrow(c, cx, cy, radius, direction, color=color)


ASPHALT = (60, 64, 73)
ASPHALT_FAINT = (45, 48, 56)
PAINT = (228, 231, 236)


def _runway_frame(cx: float, cy: float, heading: float):
    """Map runway-local (u along the heading, v to its right) to canvas coordinates."""
    rad = math.radians(heading)
    du, dv = (math.sin(rad), -math.cos(rad)), (math.cos(rad), math.sin(rad))

    def at(u: float, v: float) -> tuple[float, float]:
        return (cx + u * du[0] + v * dv[0], cy + u * du[1] + v * dv[1])

    return at


def _quad(at, u0: float, u1: float, v0: float, v1: float) -> list[tuple[float, float]]:
    return [at(u0, v0), at(u1, v0), at(u1, v1), at(u0, v1)]


def runway(c: Canvas, cx: float, cy: float, length: float, heading: float, *, width=10, fill=ASPHALT_FAINT) -> None:
    """A plain runway strip through (cx, cy) along `heading` (degrees true)."""
    at = _runway_frame(cx, cy, heading)
    c.polygon(_quad(at, -length / 2, length / 2, -width / 2, width / 2), fill=fill)


def marked_runway(
    c: Canvas,
    cx: float,
    cy: float,
    length: float,
    heading: float,
    name: str,
    opposite: str,
    *,
    faa: bool = True,
    glyph_height: float = 10.0,
) -> None:
    """A runway with threshold bars, its designators and a dashed centreline.

    `name` is the end pointing along `heading`. You land on it from the far
    end (u = -length/2), so that's where its bars and number are painted,
    upright for a pilot on final, with the L/C/R nearest the threshold, as on
    the real marking. The opposite end mirrors it. The characters are the
    FAA's own (see runway_font), and the runway is only as wide as they need.
    `faa` drops a leading zero, as US markings do; ICAO markings keep it.
    """
    from . import runway_font

    at = _runway_frame(cx, cy, heading)
    half = length / 2
    k = glyph_height / runway_font.HEIGHT  # canvas units per foot
    ends = [(name, at), (opposite, lambda u, v: at(-u, -v))]
    rows = {label: runway_font.designator(label, faa=faa) for label, _ in ends}
    widest = max(runway_font.row_width(r) for digits, letter in rows.values() for r in (digits, letter))
    width = widest * k + 4.0
    c.polygon(_quad(at, -half, half, -width / 2, width / 2), fill=ASPHALT)

    inset, stripe_len, gap, row_gap = 1.5, 4.5, 1.5, 1.5
    stripe_w = 1.15
    stripe_vs = [v for o in (width * 0.16, width * 0.36) for v in (o, -o)]
    painted = 0.0  # how far in from the threshold the markings reach
    for label, frame in ends:
        for v in stripe_vs:
            c.polygon(_quad(frame, -half + inset, -half + inset + stripe_len, v - stripe_w / 2, v + stripe_w / 2),
                      fill=PAINT)
        digits, letter = rows[label]
        row_top = -half + inset + stripe_len + gap + glyph_height
        for row in [r for r in (letter, digits) if r]:
            top = row_top

            def place(x: float, y: float, top=top, frame=frame) -> tuple[float, float]:
                return frame(top - y, x)

            runway_font.draw(
                lambda pts, hole: c.polygon(pts, fill=ASPHALT if hole else PAINT), row, place, k
            )
            row_top += row_gap + glyph_height
        painted = max(painted, row_top - row_gap + half)

    # The centreline runs from just past one end's number to just short of the other's.
    end_u = half - painted - 1.2
    u, dash, dash_gap = -end_u, 4.5, 3.0
    while u + dash <= end_u + 0.01:
        c.polygon(_quad(at, u, u + dash, -0.55, 0.55), fill=PAINT)
        u += dash + dash_gap


def stack(images: list[bytes], *, gap: float = 10) -> bytes:
    """Stack PNGs vertically into one image, so several stations are one attachment."""
    if len(images) == 1:
        return images[0]
    opened = [Image.open(io.BytesIO(b)) for b in images]
    width = max(i.width for i in opened)
    height = sum(i.height for i in opened) + round(gap * SCALE) * (len(opened) - 1)
    out = Image.new("RGB", (width, height), BG)
    y = 0
    for img in opened:
        out.paste(img, (0, y))
        y += img.height + round(gap * SCALE)
    buf = io.BytesIO()
    out.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()
