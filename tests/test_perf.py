from pathlib import Path

import pytest

from bot import fleet
from bot.wx import perf

CLUB = Path(__file__).parent / "fixtures" / "club"
PLANE = fleet.load(CLUB).get("N9999X")
TAKEOFF = PLANE.tables["takeoff"]
LANDING = PLANE.tables["landing"]


def isa(pa):
    return perf.isa_temp_c(pa)


def test_corner_and_midpoint_values():
    r = perf.lookup(TAKEOFF, weight_lb=2800, headwind_kt=0, pressure_alt_ft=0, temp_c=15)
    assert r.values == {"ground_roll": 705, "over_50ft": 1350}
    r = perf.lookup(TAKEOFF, weight_lb=2600, headwind_kt=0, pressure_alt_ft=2500, temp_c=isa(2500))
    assert r.values["ground_roll"] == (590 + 835) / 2


def test_trilinear_is_the_mean_of_eight_corners_at_the_centre():
    r = perf.lookup(TAKEOFF, weight_lb=2600, headwind_kt=5, pressure_alt_ft=3750, temp_c=isa(3750))
    assert r.values["ground_roll"] == pytest.approx((590 + 700 + 410 + 490 + 835 + 1000 + 600 + 730) / 8, abs=0.05)
    assert not r.lower_bound and not r.warnings


def test_heat_adds_ten_percent_per_25_f_above_standard():
    # 25 °F = 13.9 °C above standard at sea level.
    r = perf.lookup(TAKEOFF, weight_lb=2800, headwind_kt=0, pressure_alt_ft=0, temp_c=15 + 25 * 5 / 9)
    assert r.values["ground_roll"] == pytest.approx(705 * 1.10, abs=0.1)
    assert [s.what.split()[0] for s in r.steps] == ["table", "temp_above_standard"]


def test_cold_earns_no_credit():
    r = perf.lookup(TAKEOFF, weight_lb=2800, headwind_kt=0, pressure_alt_ft=0, temp_c=-10)
    assert r.values["ground_roll"] == 705


def test_grass_adds_a_share_of_the_obstacle_figure_to_both():
    r = perf.lookup(TAKEOFF, weight_lb=2800, headwind_kt=0, pressure_alt_ft=0, temp_c=15, grass=1)
    assert r.values == {"ground_roll": 705 + 94.5, "over_50ft": 1350 + 94.5}


def test_past_the_top_of_the_table_is_a_lower_bound_not_an_extrapolation():
    # Granby: 8,200 ft and above, where a 1964 table has nothing.
    r = perf.lookup(TAKEOFF, weight_lb=2800, headwind_kt=0, pressure_alt_ft=8500, temp_c=isa(8500))
    assert r.lower_bound
    assert r.values["ground_roll"] == 1210  # the 7,500 ft column, no more
    assert "ends at 7500" in r.warnings[0] and "worse" in r.warnings[0]


def test_a_tailwind_off_a_table_starting_at_calm_is_a_lower_bound():
    r = perf.lookup(TAKEOFF, weight_lb=2800, headwind_kt=-5, pressure_alt_ft=0, temp_c=15)
    assert r.lower_bound


def test_a_tailwind_rule_covers_it_when_the_table_has_one():
    table = perf.Table(
        "takeoff", "", ("headwind_kt",), {"headwind_kt": (0.0, 10.0)},
        {"ground_roll": [1000, 800]}, "ft", (perf.Correction("tailwind", 10, 2),),
    )
    r = perf.lookup(table, headwind_kt=-4)
    assert not r.lower_bound and r.values["ground_roll"] == 1200


def test_more_headwind_than_the_table_is_merely_conservative():
    r = perf.lookup(LANDING, headwind_kt=25, pressure_alt_ft=0, temp_c=15)
    assert not r.lower_bound and "conservative" in r.warnings[0]
    assert r.values["ground_roll"] == 270


def test_missing_input_is_an_error():
    with pytest.raises(perf.PerfError, match="weight_lb"):
        perf.lookup(TAKEOFF, headwind_kt=0, pressure_alt_ft=0)


def test_shape_is_checked():
    bad = perf.Table("t", "", ("weight_lb",), {"weight_lb": (1.0, 2.0)}, {"x": [1]}, "ft")
    with pytest.raises(perf.PerfError, match="expected 2 entries"):
        perf.check_shape(bad)


def test_grass_without_a_grass_rule_is_a_lower_bound():
    r = perf.lookup(LANDING, headwind_kt=0, pressure_alt_ft=0, temp_c=15, grass=1)
    assert not r.lower_bound  # this table has a grass rule
    table = perf.Table("t", "", ("pressure_alt_ft",), {"pressure_alt_ft": (0.0, 1000.0)}, {"x": [1, 2]}, "ft")
    r = perf.lookup(table, pressure_alt_ft=0, grass=1)
    assert r.lower_bound and "hard surface" in r.warnings[0]
