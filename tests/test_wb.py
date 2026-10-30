from pathlib import Path

import pytest

from bot import fleet
from bot.wx import wb

PLANE = fleet.load(Path(__file__).parent / "fixtures" / "club").get("N9999X")
LIMITS = PLANE.limits


def test_normal_loading_by_hand():
    load = wb.compute(LIMITS, {"front": 380, "baggage": 50}, 60, taxi_gal=1.5, burn_gal=20)
    assert load.ramp.weight_lb == 1780 + 430 + 360
    assert load.ramp.cg_in == pytest.approx((64080 + 13680 + 4750 + 17280) / 2570)
    assert load.takeoff.fuel_gal == 58.5 and load.landing.fuel_gal == 38.5
    assert load.ok


def test_over_gross_is_reported_with_the_margin():
    load = wb.compute(LIMITS, {"front": 400, "rear": 300, "baggage": 100}, 60, taxi_gal=1.5, burn_gal=20)
    assert not load.ok
    assert any("Takeoff weight 2931 lb is over the 2800 lb limit by 131 lb" in p for p in load.problems)


def test_station_limit_and_unknown_station():
    load = wb.compute(LIMITS, {"baggage": 150}, 30, taxi_gal=1.5, burn_gal=10)
    assert any("Baggage: 150 lb is over its 120 lb limit" in p for p in load.problems)
    with pytest.raises(wb.WbError, match="cargo_pod"):
        wb.compute(LIMITS, {"cargo_pod": 10}, 30, taxi_gal=1.5, burn_gal=10)


def test_burning_more_than_aboard():
    load = wb.compute(LIMITS, {"front": 200}, 10, taxi_gal=1.5, burn_gal=20)
    assert any("only 10 gal is aboard" in p for p in load.problems)


def test_envelope_edges_count_as_inside():
    env = LIMITS.envelope
    assert wb.in_envelope(env, 47.3, 2800)  # max gross at the aft limit
    assert wb.in_envelope(env, 39.5, 2800)
    assert wb.in_envelope(env, 36.25, 2525)  # on the sloping forward limit
    assert not wb.in_envelope(env, 35.0, 2600)  # forward of it
    assert not wb.in_envelope(env, 48.0, 2000)


def test_cg_moving_out_of_the_envelope_as_fuel_burns():
    # Fuel forward of the loaded CG and a heavy aft load: burning the fuel moves
    # the CG aft, past the aft limit. Takeoff is legal, landing isn't.
    limits = wb.Limits(
        empty_lb=1700, empty_arm=40.0, max_ramp_lb=2810, max_takeoff_lb=2800, max_landing_lb=2800,
        fuel_arm=20.0, usable_gal=60, lb_per_gal=6.0, envelope=LIMITS.envelope, stations=LIMITS.stations,
    )
    load = wb.compute(limits, {"rear": 380, "baggage": 120}, 60, taxi_gal=0, burn_gal=50)
    assert load.takeoff.cg_in == pytest.approx(44.22, abs=0.01)
    assert load.landing.cg_in == pytest.approx(47.43, abs=0.01)
    assert load.problems == ["Landing CG 47.4 in at 2260 lb is outside the envelope."]
