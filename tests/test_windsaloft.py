from bot.sources import windsaloft
from conftest import fixture_text


def forecast():
    return windsaloft.parse(fixture_text("windtemp_all.txt") + "\n" + fixture_text("windtemp_high.txt"))


def test_header_fields():
    f = forecast()
    assert (f.valid, f.use_from, f.use_to) == ("240600Z", "0200", "0900")


def test_denver_row_columns_line_up():
    den = forecast().site("KDEN")
    # DEN is too high for 3000 and 6000; its first column is 9000.
    assert 3000 not in den and 6000 not in den
    assert den[9000].light_variable and den[9000].temp_c == 10
    assert (den[12000].direction, den[12000].speed_kt, den[12000].temp_c) == (210, 17, 3)
    assert (den[24000].direction, den[24000].speed_kt, den[24000].temp_c) == (220, 62, -24)


def test_unsigned_temperature_above_24000_is_negative():
    den = forecast().site("DEN")
    assert (den[30000].direction, den[30000].speed_kt, den[30000].temp_c) == (200, 77, -38)


def test_high_levels_merge_into_the_same_site():
    den = forecast().site("DEN")
    assert den[45000].speed_kt == 60 and den[53000].temp_c == -65


def test_3000_has_no_temperature():
    w = forecast().site("ABR")[3000]
    assert (w.direction, w.speed_kt, w.temp_c) == (200, 48, None)


def test_decode_over_100_knots():
    w = windsaloft.decode("7315-45", 34000)
    assert (w.direction, w.speed_kt, w.temp_c) == (230, 115, -45)


def test_decode_light_and_variable():
    w = windsaloft.decode("9900+05", 6000)
    assert w.light_variable and w.speed_kt == 0 and w.temp_c == 5


def test_decode_rejects_garbage():
    assert windsaloft.decode("ABCD", 3000) is None
