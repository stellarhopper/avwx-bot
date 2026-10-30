#!/usr/bin/env python3
"""Draw the bot's logo: assets/logo.png (1024 px) and assets/logo-512.png.

The METAR card's wind rose, reduced to an emblem: a compass ring, a runway
with painted numbers, and the wind arrow crossing it. Same palette and
arrowhead geometry as the cards, so the avatar looks like the images the bot
posts. Discord shows avatars as circles, so everything sits inside one.

    python scripts/make_logo.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.render import theme  # noqa: E402

SIZE = 1024
SS = 4  # drawn this many times larger, then scaled down for smooth edges
RUNWAY_BEARING = 70  # degrees true: runway 07/25, lying across the arrow
WIND_TO = 25  # the arrow points this way: a wind from 205°


def main() -> None:
    n = SIZE * SS
    c = n / 2
    img = Image.new("RGB", (n, n), theme.BG)
    d = ImageDraw.Draw(img)

    def at(bearing_deg: float, r: float, origin=(c, c)) -> tuple[float, float]:
        a = math.radians(bearing_deg)
        return origin[0] + math.sin(a) * r, origin[1] - math.cos(a) * r

    # A panel disk filling the circle Discord crops to, with a thin rim just
    # inside it. Every pixel of radius counts at 32 px.
    disk = n * 0.5
    d.ellipse([c - disk, c - disk, c + disk, c + disk], fill=theme.PANEL)
    rim = n * 0.475
    d.ellipse([c - rim, c - rim, c + rim, c + rim], outline=theme.BORDER, width=round(n * 0.006))

    # Compass ring, with ticks every 30 degrees and a longer one at north.
    ring = n * 0.385
    d.ellipse([c - ring, c - ring, c + ring, c + ring], outline=theme.FAINT, width=round(n * 0.008))
    for b in range(0, 360, 30):
        inner = ring - (n * 0.05 if b == 0 else n * 0.022)
        d.line([at(b, inner), at(b, ring)], fill=theme.MUTED if b == 0 else theme.FAINT,
               width=round(n * (0.012 if b == 0 else 0.008)))
    north = theme.font("bold", n * 0.056, scale=1)
    d.text(at(0, ring + n * 0.048), "N", font=north, fill=theme.MUTED, anchor="mm")

    # The runway: asphalt, threshold bars and a dashed centreline.
    half_len, half_w = n * 0.33, n * 0.07
    ux, uy = math.sin(math.radians(RUNWAY_BEARING)), -math.cos(math.radians(RUNWAY_BEARING))
    vx, vy = -uy, ux  # across the runway

    def rw(u: float, v: float) -> tuple[float, float]:
        return c + ux * u + vx * v, c + uy * u + vy * v

    def quad(u0, u1, v0, v1):
        return [rw(u0, v0), rw(u1, v0), rw(u1, v1), rw(u0, v1)]

    d.polygon(quad(-half_len, half_len, -half_w, half_w), fill=theme.ASPHALT)
    for end in (-1, 1):
        for k in range(-2, 3):
            if k == 0:
                continue  # the gap the centreline runs through
            v = k * half_w * 0.36
            u0 = end * (half_len - n * 0.012)
            u1 = end * (half_len - n * 0.062)
            d.polygon(quad(min(u0, u1), max(u0, u1), v - half_w * 0.1, v + half_w * 0.1), fill=theme.PAINT)
    dash, gap = n * 0.04, n * 0.03
    u = -half_len + n * 0.085
    while u + dash < half_len - n * 0.085:
        d.polygon(quad(u, u + dash, -n * 0.0055, n * 0.0055), fill=theme.PAINT)
        u += dash + gap

    # The wind arrow, with a dark outline where it crosses the runway.
    to = math.radians(WIND_TO)
    reach = ring - n * 0.035
    tip = at(WIND_TO, reach)
    tail = at(WIND_TO + 180, reach * 0.92)
    end, left, right = theme.arrowhead(tip, to, n * 0.16, 0.44)
    halo = n * 0.011
    shaft = n * 0.04
    d.line([tail, end], fill=theme.PANEL, width=round(shaft + 2 * halo))
    d.polygon([tip, left, right], fill=theme.PANEL)
    d.line([tip, left, right, tip], fill=theme.PANEL, width=round(2 * halo), joint="curve")
    d.line([tail, end], fill=theme.ACCENT, width=round(shaft))
    d.polygon([tip, left, right], fill=theme.ACCENT)

    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    logo = img.resize((SIZE, SIZE), Image.LANCZOS)
    logo.save(out / "logo.png", optimize=True)
    logo.resize((512, 512), Image.LANCZOS).save(out / "logo-512.png", optimize=True)
    print(f"wrote {out / 'logo.png'} and {out / 'logo-512.png'}")


if __name__ == "__main__":
    main()
