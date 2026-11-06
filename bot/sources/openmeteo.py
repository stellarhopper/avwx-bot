"""Model winds aloft from Open-Meteo's GFS, standing in for the FB forecast.

No FB forecast for the US was published for days in November 2026, which left
the pass board without ridge winds and flight plans without winds at cruise.
This builds the same `windsaloft.Forecast` from the GFS instead, for the same
sites, so everything downstream works unchanged; `Forecast.model` says where it
came from, and everything that shows these winds says so.

The GFS gives winds on pressure levels. Each level is placed at its standard-
atmosphere pressure altitude, and the FB altitudes are interpolated between
them, as a vector, by `windsaloft.interpolate`. Like the FB, levels within
1,500 ft of the ground are left out (the model reports winds underground too).

Open-Meteo is free for non-commercial use up to 10,000 calls a day; a request
for many sites and variables counts as several. One request covers every site
for two days, and is cached for three hours (the GFS runs every six), which
stays well under it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..hazards import fb_sites
from .http import Http, SourceError
from .windsaloft import Forecast, Wind, interpolate

URL = "https://api.open-meteo.com/v1/gfs"
TTL_MODEL_WINDS = 3 * 3600
MODEL = "GFS model (Open-Meteo)"
PRESSURE_LEVELS_HPA = (900, 850, 800, 750, 700, 650, 500, 400, 300, 250, 200, 150)
FB_LEVELS_FT = (3000, 6000, 9000, 12000, 18000, 24000, 30000, 34000, 39000)
# The FB leaves out levels within 1,500 ft of the station.
GROUND_CLEARANCE_FT = 1500
# Below 5 kt the FB says light and variable (9900).
LIGHT_KT = 5
# Hours from now, rounded down to the hour, that each FB forecast's stand-in
# is valid and for use. They tile the next 24 hours, like the 06, 12 and 24 h
# FBs do.
WINDOWS = {"06": (3, 0, 6), "12": (9, 6, 12), "24": (18, 12, 24)}  # valid, use from, use to


def pressure_altitude_ft(hpa: float) -> float:
    return 145366.45 * (1 - (hpa / 1013.25) ** 0.190284)


class OpenMeteo:
    def __init__(self, http: Http) -> None:
        self.http = http

    async def winds_aloft(self, fcst: str, now: datetime) -> Forecast:
        if fcst not in WINDOWS:
            raise SourceError(f"No model stand-in for the {fcst} h forecast.")
        sites = fb_sites()
        idents = sorted(sites)
        variables = [
            f"{v}_{p}hPa" for p in PRESSURE_LEVELS_HPA
            for v in ("wind_speed", "wind_direction", "temperature")
        ]
        data = await self.http.get_json(
            URL,
            {
                "latitude": ",".join(f"{sites[s][0]:.3f}" for s in idents),
                "longitude": ",".join(f"{sites[s][1]:.3f}" for s in idents),
                "hourly": ",".join(variables),
                "wind_speed_unit": "kn",
                "timezone": "GMT",
                "forecast_days": 2,
            },
            ttl=TTL_MODEL_WINDS,
        )
        if isinstance(data, dict):  # one location comes back bare, not in a list
            data = [data]
        if not isinstance(data, list) or len(data) != len(idents):
            raise SourceError("The GFS model winds came back incomplete.")

        valid_h, from_h, to_h = WINDOWS[fcst]
        start = now.replace(minute=0, second=0, microsecond=0)
        valid = start + timedelta(hours=valid_h)
        forecast = Forecast(
            valid=f"{valid:%d%H%M}Z",
            use_from=f"{start + timedelta(hours=from_h):%H%M}",
            use_to=f"{start + timedelta(hours=to_h):%H%M}",
            model=MODEL,
        )
        stamp = f"{valid:%Y-%m-%dT%H:00}"
        for ident, entry in zip(idents, data):
            levels = _site(entry, stamp)
            if levels:
                forecast.sites[ident] = levels
        if not forecast.sites:
            raise SourceError("The GFS model winds don't cover the forecast time.")
        return forecast


def _site(entry: dict, stamp: str) -> dict[int, Wind]:
    hourly = entry.get("hourly") or {}
    try:
        i = hourly["time"].index(stamp)
    except (KeyError, ValueError):
        return {}
    ground_ft = (entry.get("elevation") or 0) * 3.28084
    by_pressure: dict[int, Wind] = {}
    for p in PRESSURE_LEVELS_HPA:
        alt = round(pressure_altitude_ft(p))
        speed = _at(hourly, f"wind_speed_{p}hPa", i)
        direction = _at(hourly, f"wind_direction_{p}hPa", i)
        temp = _at(hourly, f"temperature_{p}hPa", i)
        if speed is None or direction is None or alt < ground_ft:
            continue
        by_pressure[alt] = Wind(alt, round(direction) % 360 or 360, round(speed),
                                round(temp) if temp is not None else None)
    out: dict[int, Wind] = {}
    for level in FB_LEVELS_FT:
        if level < ground_ft + GROUND_CLEARANCE_FT:
            continue
        w = interpolate(by_pressure, level)
        if w is None:
            continue
        if w.light_variable or w.speed_kt < LIGHT_KT:
            w = Wind(level, None, 0, w.temp_c)
        out[level] = Wind(level, w.direction, w.speed_kt, w.temp_c)
    return out


def _at(hourly: dict, key: str, i: int) -> float | None:
    values = hourly.get(key)
    if not values or i >= len(values):
        return None
    return values[i]
