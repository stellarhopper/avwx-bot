from datetime import datetime, timedelta, timezone

import pytest

from bot.sources.windsaloft import Wind, interpolate
from bot.wx import navlog

T0 = datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc)


def test_wind_triangle_by_hand():
    # Course 090, TAS 100, wind from the north at 20: correct 11.5° left, GS 98.
    wca, gs = navlog.wind_triangle(90, 100, 360, 20)
    assert wca == pytest.approx(-11.54, abs=0.01)
    assert gs == pytest.approx(97.98, abs=0.01)
    assert navlog.wind_triangle(360, 100, 360, 20) == pytest.approx((0, 80))
    assert navlog.wind_triangle(360, 100, 180, 20) == pytest.approx((0, 120))


def test_wind_stronger_than_tas_is_an_error():
    with pytest.raises(navlog.NavLogError):
        navlog.wind_triangle(90, 50, 360, 60)


def test_flights_split_at_stops_with_fuel_and_times():
    pts = [
        navlog.Waypoint("A", 40.0, -105.0, 5000, 8.0),
        navlog.Waypoint("B", 40.0, -104.0),  # no variation: borrows A's
        navlog.Waypoint("C", 41.0, -104.0, 4000, 8.0),
    ]
    log = navlog.build(
        pts, departure=T0, tas_kt=100, burn_gph=10, cruise_alt_ft=9000,
        winds=[None, None], stops={1}, taxi_gal=1, climb_gal_per_1000ft=0.5,
        reserve_minutes=30, ground_minutes=30,
    )
    first, second = log.flights
    assert (first.origin, first.dest, second.origin, second.dest) == ("A", "B", "B", "C")
    assert first.legs[0].magnetic_course == round(first.legs[0].true_course - 8)
    assert first.fuel_taxi_climb_gal == 1 + 0.5 * 4.0
    assert second.fuel_taxi_climb_gal == 1 + 0.5 * 9.0  # B's elevation unknown: from sea level
    assert first.reserve_gal == 5
    assert first.fuel_required_gal == pytest.approx(first.fuel_burned_gal + 5, abs=0.11)
    assert second.departs == first.arrives + timedelta(minutes=30)
    assert log.etas[2] == second.arrives
    assert any("B: field elevation unknown" in n for n in log.notes)


def test_wind_is_interpolated_as_a_vector_through_north():
    levels = {9000: Wind(9000, 350, 20, 0), 12000: Wind(12000, 10, 20, -6)}
    w = interpolate(levels, 10500)
    assert w.direction == 360 and w.speed_kt == 20 and w.temp_c == -3
    assert interpolate(levels, 6000) is levels[9000]  # below the lowest: the lowest
    assert interpolate(levels, 18000) is None
