import io
from datetime import datetime, timezone

from PIL import Image

from bot.render import cards, theme
from bot.sources.awc import Metar, Taf
from bot.wx import fmt
from conftest import fixture_json

NOW = datetime(2026, 10, 24, 6, 40, tzinfo=timezone.utc)
TZ = fmt.zone("America/Denver")


def size(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


def test_metar_cards_render_for_every_fixture():
    for d in fixture_json("metar.json"):
        png = cards.metar_card(Metar.from_api(d), units=fmt.Units(), tz=TZ, now=NOW)
        w, h = size(png)
        assert w == cards.WIDTH * theme.SCALE and 300 < h < 1400


def test_metar_card_handles_missing_values():
    sparse = {"icaoId": "KXXX", "rawOb": "KXXX 240600Z AUTO", "obsTime": 1792821600}
    png = cards.metar_card(Metar.from_api(sparse), units=fmt.Units("hPa", "F"), tz=TZ, now=NOW)
    assert size(png)[0] == cards.WIDTH * theme.SCALE


def test_taf_card_renders():
    t = Taf.from_api(fixture_json("taf.json")[0])
    w, h = size(cards.taf_card(t, tz=TZ, now=NOW))
    assert w == cards.WIDTH * theme.SCALE and h > 400


def test_stack_combines_images():
    a = cards.metar_card(Metar.from_api(fixture_json("metar.json")[1]), units=fmt.Units(), tz=TZ, now=NOW)
    combined = theme.stack([a, a])
    assert size(combined)[1] > 2 * size(a)[1]


def test_alt_text_is_meaningful():
    m = Metar.from_api(fixture_json("metar.json")[1])
    alt = cards.metar_alt(m, fmt.Units())
    assert "KAPA VFR" in alt and "230° 10 kt" in alt and "30.04 inHg" in alt


def test_bundled_fonts_load():
    f = theme.font("regular", 12)
    assert "Inter" in " ".join(f.getname())


def test_every_card_renders(tmp_path):
    """The preview script draws every renderer from fixtures; none may fail or come out blank."""
    from bot.render import preview

    paths = preview.render_all(tmp_path)
    assert {p.name for p in paths} == {"metar.png", "taf.png", "mountain.png", "brief.png", "winds.png", "alert.png"}
    for path in paths:
        img = Image.open(path)
        assert img.width >= 1000 and img.height >= 200
        assert len(img.convert("L").resize((40, 40)).getcolors(1600)) > 5  # not a blank fill


def test_runway_font_covers_every_designator_character():
    from bot.render import runway_font

    assert set("0123456789LCR") | {"1_single"} <= set(runway_font.GLYPHS)
    for name, rings in runway_font.GLYPHS.items():
        assert rings and all(len(ring) >= 4 for ring in rings), name
        for ring in rings:
            # Feet, from the AC figure: 60 tall, and none wider than the "4".
            assert all(-0.1 <= x <= 26 and -0.1 <= y <= 60.1 for x, y in ring), name


def test_runway_designator_rules():
    from bot.render import runway_font as f

    assert f.designator("09L", faa=True) == ("9", "L")  # FAA: no leading zero
    assert f.designator("09L", faa=False) == ("09", "L")  # ICAO keeps it
    assert f.glyph_names("1") == ["1_single"]  # a lone 1 gets its base bar
    assert f.glyph_names("17") == ["1", "7"]
    # Numerals sit 15 ft apart; "11" is spaced 27 ft left edge to left edge.
    assert abs(f.row_width("34") - (f._width("3") + 15 + f._width("4"))) < 1e-9
    assert abs(f.row_width("11") - (27 + f._width("1"))) < 1e-9
