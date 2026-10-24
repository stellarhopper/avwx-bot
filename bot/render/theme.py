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

    def rotated_text(self, x, y, value: str, degrees: float, *, style="bold", size=10, fill=TEXT, spacing=0.0) -> None:
        """Text centred on (x, y), turned `degrees` clockwise. Multi-line is stacked and centred."""
        f = font(style, size, self.scale)
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        box = probe.multiline_textbbox((0, 0), value, font=f, align="center", spacing=round(spacing * self.scale))
        w, h = math.ceil(box[2] - box[0]) + 4, math.ceil(box[3] - box[1]) + 4
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).multiline_text(
            (2 - box[0], 2 - box[1]), value, font=f, fill=255, align="center", spacing=round(spacing * self.scale)
        )
        mask = mask.rotate(-degrees, resample=Image.BICUBIC, expand=True)
        cx, cy = self._s(x, y)
        self.image.paste(Image.new("RGB", mask.size, fill), (cx - mask.width // 2, cy - mask.height // 2), mask)

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
    c: Canvas, cx: float, cy: float, length: float, heading: float, name: str, opposite: str, *, width=26
) -> None:
    """A runway with threshold bars, painted numbers and a dashed centreline.

    `name` is the end pointing along `heading`: you land on it from the far
    end (u = -length/2), so that's where its bars and number are painted,
    turned to read upright for a pilot on final. The opposite end mirrors it.
    """
    at = _runway_frame(cx, cy, heading)
    half = length / 2
    c.polygon(_quad(at, -half, half, -width / 2, width / 2), fill=ASPHALT)
    stripe_len, inset = 7.0, 2.0
    stripe_vs = [v for k in (2.6, 5.4, 8.2, 11.0) for v in (k, -k)]
    for sign in (-1, 1):  # -1: `name`'s threshold, +1: `opposite`'s
        u_edge = sign * (half - inset)
        u_inner = sign * (half - inset - stripe_len)
        for v in stripe_vs:
            c.polygon(_quad(at, u_edge, u_inner, v - 0.8, v + 0.8), fill=PAINT)
    number_offset = half - inset - stripe_len - 12
    for sign, label, facing in ((-1, name, heading), (1, opposite, heading + 180)):
        digits = label.rstrip("LCR")
        letter = label[len(digits):]
        text = f"{digits}\n{letter}" if letter else digits
        x, y = at(sign * number_offset, 0)
        c.rotated_text(x, y, text, facing % 360, style="bold", size=9, fill=PAINT, spacing=0.5)
    u, dash, gap = -(number_offset - 13), 5.0, 4.0
    while u + dash <= number_offset - 13:
        c.polygon(_quad(at, u, u + dash, -0.6, 0.6), fill=PAINT)
        u += dash + gap


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
