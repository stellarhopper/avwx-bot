#!/usr/bin/env python3
"""Draw the bot's profile banner: assets/banner.png (680x240) and banner@2x.

Night-toned sky over three ridgelines, faint wind lines bending over them the
way mountain wave does, and a high-wing Cessna silhouette trailing a dashed
track, all in the cards' palette. Discord puts the avatar over the lower
left of the banner, so the aircraft keeps to the right.

    python scripts/make_banner.py
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.render import theme  # noqa: E402

W, H = 680, 240
SS = 4  # drawn larger, scaled down for smooth edges


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def ridge(seed: int, base: float, height: float, roughness: float, width: int,
          peaks: tuple[tuple[float, float, float], ...] = ()) -> list[tuple[float, float]]:
    """A ridgeline by midpoint displacement: jagged like the Front Range,
    and the same every run, since the seed is fixed."""
    rnd = random.Random(seed)
    n = 1024  # midpoint displacement needs a power of two; stretched to `width` below
    pts = {0: base - rnd.random() * height, n: base - rnd.random() * height}
    step, spread = n, height
    while step > 2:
        for x in range(0, n, step):
            mid = x + step // 2
            if mid not in pts:
                pts[mid] = (pts[x] + pts[x + step]) / 2 + (rnd.random() - 0.5) * spread
        step //= 2
        spread *= roughness
    ys = sorted(pts.items())
    out = []
    for x, y in ys:
        # Summits: (where, how high, width to the west, width to the east),
        # in fractions of the ridge. Lopsided, like real ones: a steep face
        # and a long shoulder.
        t = x / n
        lift = max(
            (hgt * max(0.0, 1 - (at_ - t) / west if t < at_ else 1 - (t - at_) / east) ** 1.15
             for at_, hgt, west, east in peaks),
            default=0.0,
        )
        out.append((x * width / n, y - lift * height))
    return out


def cessna(length: float, at: tuple[float, float], climb_deg: float):
    """A Cessna 182 in side view, nose to the right, in units of its length
    (29 ft): x from the tail (0) to the spinner (1), y down from the fin top.
    Rotated nose-up by `climb_deg` about `at`, the middle of the cabin."""
    a = math.radians(-climb_deg)

    def p(x: float, y: float) -> tuple[float, float]:
        x, y = (x - 0.55) * length, (y - 0.17) * length
        return at[0] + x * math.cos(a) - y * math.sin(a), at[1] + x * math.sin(a) + y * math.cos(a)

    shapes = {
        "fuselage": [
            (0.035, 0.0), (0.085, 0.0),                    # fin top
            (0.215, 0.168),                                # swept fin leading edge to the spine
            (0.47, 0.108), (0.50, 0.098),                  # tailcone up to the cabin roof
            (0.685, 0.098), (0.765, 0.152),                # roof, windshield
            (0.93, 0.166), (0.975, 0.180), (1.0, 0.203),   # cowling, spinner
            (0.975, 0.226), (0.93, 0.238),
            (0.80, 0.258), (0.56, 0.262),                  # belly
            (0.30, 0.232), (0.10, 0.205),                  # underside of the tailcone
            (0.0, 0.195), (0.012, 0.165),                  # rudder
        ],
        "wing": [(0.468, 0.084), (0.69, 0.088), (0.695, 0.101), (0.466, 0.104)],
        "stabilizer": [(-0.01, 0.177), (0.15, 0.175), (0.15, 0.186), (0.0, 0.188)],
    }
    windows = [
        [(0.53, 0.118), (0.66, 0.118), (0.725, 0.158), (0.53, 0.162)],   # front side window
        [(0.455, 0.124), (0.515, 0.118), (0.515, 0.160), (0.445, 0.156)],  # rear side window
    ]
    lines = [
        ((0.61, 0.102), (0.575, 0.245), 0.011),   # wing strut
        ((0.585, 0.255), (0.545, 0.315), 0.012),  # main gear spring leg
        ((0.885, 0.24), (0.89, 0.31), 0.012),     # nose gear
    ]
    wheels = [((0.545, 0.322), 0.026), ((0.89, 0.318), 0.022)]
    return (
        {k: [p(*q) for q in v] for k, v in shapes.items()},
        [[p(*q) for q in w] for w in windows],
        [(p(*a0), p(*a1), wd * length) for a0, a1, wd in lines],
        [(p(*c), r * length) for c, r in wheels],
    )


def main() -> None:
    w, h = W * SS, H * SS
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)

    # Sky: the cards' background at the top, a little bluer at the horizon.
    top, horizon = theme.BG, (36, 42, 56)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=lerp(top, horizon, (y / h) ** 1.6))

    # Wind lines, drawn on an overlay so they can be faint. They rise and
    # fall over the ridges, the way air does in mountain wave.
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    o = ImageDraw.Draw(overlay)
    for i, (y0, amp, phase, alpha) in enumerate(
        [(0.20, 0.035, 0.0, 34), (0.30, 0.045, 0.7, 42), (0.40, 0.055, 1.3, 50), (0.50, 0.06, 1.9, 40)]
    ):
        pts = []
        for k in range(0, 241):
            t = k / 240
            y = y0 + amp * math.sin(t * math.tau * 1.6 + phase) * (0.4 + 0.6 * t)
            pts.append((t * w, y * h))
        o.line(pts, fill=(*theme.ACCENT, alpha), width=round(1.2 * SS), joint="curve")
    img.paste(overlay, (0, 0), overlay)
    d = ImageDraw.Draw(img)

    # Three ridgelines: far ones lighter with haze, near ones darker.
    for seed, base, height, rough, color, peaks in [
        (11, 0.68, 0.36, 0.62, (54, 60, 74),
         ((0.13, 0.42, 0.07, 0.16), (0.42, 0.58, 0.16, 0.07), (0.57, 0.38, 0.05, 0.12), (0.87, 0.50, 0.08, 0.18))),
        (23, 0.85, 0.20, 0.60, (40, 45, 56), ((0.28, 0.5, 0.18, 0.09), (0.70, 0.42, 0.08, 0.20))),
        (37, 0.96, 0.12, 0.58, (27, 29, 35), ()),
    ]:
        line = ridge(seed, base * h, height * h, rough, w, peaks)
        d.polygon(line + [(w, h), (0, h)], fill=color)
        # A faint highlight along the far ridge's crest, as if moonlit.
        if seed == 11:
            d.line(line, fill=(78, 86, 102), width=round(0.8 * SS), joint="curve")

    # The aircraft, climbing out to the right, trailing a dashed track from
    # its tail back across the sky.
    length = 0.15 * w
    plane_at = (0.70 * w, 0.29 * h)
    climb = 5
    shapes, windows, lines, wheels = cessna(length, plane_at, climb)
    back = math.radians(180 - climb)
    tail = length * 0.58
    track_len = 0.52 * w
    dash, gap = 7 * SS, 6 * SS
    s_ = tail
    while s_ < track_len:
        e = min(s_ + dash, track_len)
        p0 = (plane_at[0] + math.cos(back) * s_, plane_at[1] - math.sin(back) * s_ + 0.03 * length)
        p1 = (plane_at[0] + math.cos(back) * e, plane_at[1] - math.sin(back) * e + 0.03 * length)
        fade = 1 - (s_ - tail) / (track_len - tail)
        d.line([p0, p1], fill=lerp(horizon, theme.ACCENT, 0.15 + 0.85 * fade), width=round(1.8 * SS))
        s_ += dash + gap

    color = theme.TEXT
    for shape in shapes.values():
        d.polygon(shape, fill=color)
    for a0, a1, width in lines:
        d.line([a0, a1], fill=color, width=round(width))
    for (x, y), r in wheels:
        d.ellipse([x - r, y - r, x + r, y + r], fill=color)
    sky = lerp(top, horizon, 0.29 ** 1.6)
    for window in windows:
        d.polygon(window, fill=sky)

    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    img.resize((W * 2, H * 2), Image.LANCZOS).save(out / "banner@2x.png", optimize=True)
    img.resize((W, H), Image.LANCZOS).save(out / "banner.png", optimize=True)
    print(f"wrote {out / 'banner.png'} and {out / 'banner@2x.png'}")


if __name__ == "__main__":
    main()
