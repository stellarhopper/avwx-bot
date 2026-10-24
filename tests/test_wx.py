import pytest

from bot.wx import density_alt, flightcat, fmt, geo


@pytest.mark.parametrize(
    "ceiling, vis, expected",
    [
        (None, 10, "VFR"),
        (3100, 6, "VFR"),
        (3000, 10, "MVFR"),
        (5000, 5, "MVFR"),
        (999, 10, "IFR"),
        (5000, 2.5, "IFR"),
        (499, 10, "LIFR"),
        (5000, 0.75, "LIFR"),
        (800, 0.5, "LIFR"),
    ],
)
def test_flight_category(ceiling, vis, expected):
    assert flightcat.category(ceiling, vis) == expected


def test_ceiling_ignores_few_and_sct():
    layers = [{"cover": "FEW", "base": 800}, {"cover": "SCT", "base": 1500}, {"cover": "BKN", "base": 4000}]
    assert flightcat.ceiling(layers) == 4000


def test_vertical_visibility_is_a_ceiling():
    assert flightcat.ceiling([], 200) == 200


@pytest.mark.parametrize("raw, sm", [("10+", 10), ("6+", 6), ("1/2", 0.5), ("1 1/2", 1.5), (3, 3), ("P6SM", 6), (None, None)])
def test_parse_visibility(raw, sm):
    assert flightcat.parse_visibility(raw) == sm


def test_density_altitude_hot_day_at_leadville():
    # 9,927 ft field, 30.10 inHg, 25 °C: about 13,000 ft by any E6B.
    da = density_alt.density_altitude(9927, 30.10, 25)
    assert 12800 < da < 13300


def test_density_altitude_standard_day_is_field_elevation():
    assert abs(density_alt.density_altitude(0, 29.92, 15)) < 1


def test_distance_den_to_apa():
    d = geo.distance_nm((39.8466, -104.6562), (39.5599, -104.8484))
    assert 18 < d < 20


def test_point_in_polygon_and_distance():
    square = [(39, -106), (39, -105), (40, -105), (40, -106)]
    assert geo.point_in_polygon((39.5, -105.5), square)
    assert not geo.point_in_polygon((41, -105.5), square)
    assert geo.distance_to_polygon_nm((39.5, -105.5), square) == 0
    assert 59 < geo.distance_to_polygon_nm((41, -105.5), square) < 61


def test_fmt_wind():
    assert fmt.wind(0, 0) == "Calm"
    assert fmt.wind(230, 10, 17) == "230° 10 kt G17"
    assert fmt.wind(None, 4, variable=True) == "VRB 4 kt"


def test_fmt_visibility():
    assert fmt.visibility(10, "10+") == "10+ SM"
    assert fmt.visibility(6, "6+") == "6+ SM"
    assert fmt.visibility(0.5) == "1/2 SM"
    assert fmt.visibility(1.5) == "1 1/2 SM"
