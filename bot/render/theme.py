"""Shared drawing vocabulary: colours, fonts, and the few primitives every card uses.

Cards are drawn at 2× and laid out in logical pixels, so the numbers in the
renderers read like CSS while the PNG stays sharp when Discord shows it at
full size on a high-density screen.

The palette is dark to sit on Discord's dark theme without a glaring white
block, and the flight-category colours follow the convention pilots already
read on every sectional-chart overlay: VFR green, MVFR blue, IFR red, LIFR
magenta.
"""

from __future__ import annotations

import io
import math
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
SCALE = 2

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
def font(style: str, size: float) -> ImageFont.FreeTypeFont:
    px = round(size * SCALE)
    for path in (FONT_DIR / _FONT_FILES[style], Path(_FALLBACKS[style])):
        try:
            return ImageFont.truetype(str(path), px)
        except OSError:
            continue
    return ImageFont.load_default(px)


class Canvas:
    """An image plus a draw handle that take logical coordinates."""

    def __init__(self, width: float, height: float, background=BG) -> None:
        self.width = width
        self.height = height
        self.image = Image.new("RGB", (round(width * SCALE), round(height * SCALE)), background)
        self.draw = ImageDraw.Draw(self.image)

    @staticmethod
    def _s(*values: float) -> list[int]:
        return [round(v * SCALE) for v in values]

    def rect(self, x, y, w, h, *, fill=None, outline=None, radius=0, width=1) -> None:
        box = self._s(x, y, x + w, y + h)
        self.draw.rounded_rectangle(
            box, radius=round(radius * SCALE), fill=fill, outline=outline, width=round(width * SCALE)
        )

    def line(self, points, *, fill=BORDER, width=1) -> None:
        self.draw.line([tuple(self._s(*p)) for p in points], fill=fill, width=max(1, round(width * SCALE)))

    def polygon(self, points, *, fill=None, outline=None) -> None:
        self.draw.polygon([tuple(self._s(*p)) for p in points], fill=fill, outline=outline)

    def circle(self, cx, cy, r, *, fill=None, outline=None, width=1) -> None:
        self.draw.ellipse(self._s(cx - r, cy - r, cx + r, cy + r), fill=fill, outline=outline, width=round(width * SCALE))

    def text(self, x, y, value: str, *, style="regular", size=14, fill=TEXT, anchor="la") -> None:
        self.draw.text(tuple(self._s(x, y)), value, font=font(style, size), fill=fill, anchor=anchor)

    def text_width(self, value: str, *, style="regular", size=14) -> float:
        return self.draw.textlength(value, font=font(style, size)) / SCALE

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
        buf = io.BytesIO()
        self.image.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


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
    tip = (cx + math.sin(to) * (radius - 6), cy - math.cos(to) * (radius - 6))
    tail = (cx - math.sin(to) * (radius - 6), cy + math.cos(to) * (radius - 6))
    c.line([tail, tip], fill=color, width=3)
    head = 10
    left = (tip[0] - math.sin(to - 0.45) * head, tip[1] + math.cos(to - 0.45) * head)
    right = (tip[0] - math.sin(to + 0.45) * head, tip[1] + math.cos(to + 0.45) * head)
    c.polygon([tip, left, right], fill=color)


def wind_arrow(c: Canvas, cx: float, cy: float, radius: float, direction: int | None, *, color=TEXT) -> None:
    compass(c, cx, cy, radius)
    if direction is not None:
        arrow(c, cx, cy, radius, direction, color=color)


def runway(c: Canvas, cx: float, cy: float, length: float, heading: float, *, width=8, fill=BORDER) -> None:
    """A runway strip through (cx, cy) along `heading` (degrees true)."""
    rad = math.radians(heading)
    dx, dy = math.sin(rad) * length / 2, -math.cos(rad) * length / 2
    px, py = math.cos(rad) * width / 2, math.sin(rad) * width / 2
    c.polygon(
        [(cx - dx - px, cy - dy - py), (cx - dx + px, cy - dy + py), (cx + dx + px, cy + dy + py), (cx + dx - px, cy + dy - py)],
        fill=fill,
    )


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
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
