"""Pressure and density altitude.

Uses the standard approximations pilots use (and the E6B does):

    pressure altitude = field elevation + (29.92 − altimeter inHg) × 1000
    ISA temperature   = 15 °C − 2 °C per 1000 ft of pressure altitude
    density altitude  = pressure altitude + 120 × (OAT − ISA temperature)

Good to well within 100 ft at the elevations and temperatures that matter in
the mountains, which is far finer than anyone plans to.
"""

from __future__ import annotations

HPA_PER_INHG = 33.8639
FT_PER_M = 3.28084


def hpa_to_inhg(hpa: float) -> float:
    return hpa / HPA_PER_INHG


def pressure_altitude(elevation_ft: float, altimeter_inhg: float) -> float:
    return elevation_ft + (29.92 - altimeter_inhg) * 1000


def isa_temp_c(pressure_alt_ft: float) -> float:
    return 15 - 2 * pressure_alt_ft / 1000


def density_altitude(elevation_ft: float, altimeter_inhg: float, temp_c: float) -> float:
    pa = pressure_altitude(elevation_ft, altimeter_inhg)
    return pa + 120 * (temp_c - isa_temp_c(pa))


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32
