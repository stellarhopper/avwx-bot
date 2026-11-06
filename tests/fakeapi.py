"""The real Awc and Nws clients, served from tests/fixtures instead of the network.

Routing by URL keeps the clients' own request building and parsing under test;
only the socket is replaced.
"""

from __future__ import annotations

import json
import types

from bot.sources.awc import Awc
from bot.sources.http import Http
from bot.sources.nws import Nws
from conftest import fixture_json, fixture_text


class FixtureHttp(Http):
    def __init__(self, **overrides) -> None:
        super().__init__("tests")
        self.retry_delays = (0.0, 0.0)  # same number of attempts, no waiting
        self.overrides = overrides  # product name → (status, body) or callable(params)
        self.requests: list[tuple[str, dict | None]] = []

    async def _fetch(self, url, params, accept):
        self.requests.append((url, params))
        params = params or {}
        if "open-meteo" in url:
            product = "openmeteo"
        else:
            product = url.rsplit("/", 1)[-1] if "aviationweather" in url else _nws_product(url)
        if product in self.overrides:
            o = self.overrides[product]
            return o(params) if callable(o) else o
        if product == "metar":
            ids = set(params.get("ids", "").split(","))
            return 200, json.dumps([d for d in all_metars() if d["icaoId"] in ids])
        if product == "taf":
            ids = set(params.get("ids", "").split(","))
            return 200, json.dumps([d for d in fixture_json("taf.json") if d["icaoId"] in ids])
        if product == "stationinfo":
            ids = set(params.get("ids", "").split(","))
            out = [
                {"icaoId": d["icaoId"], "site": d["name"], "lat": d["lat"], "lon": d["lon"], "elev": d["elev"],
                 "state": "CO" if d["icaoId"].startswith("K") else "", "country": "US" if d["icaoId"].startswith("K") else "GB"}
                for d in all_metars()
                if d["icaoId"] in ids
            ]
            return 200, json.dumps(out)
        if product == "airport":
            ids = set(params.get("ids", "").split(","))
            return 200, json.dumps([d for d in fixture_json("airport.json") if d["icaoId"] in ids])
        if product in ("fix", "navaid"):
            ids = set(params.get("ids", "").split(","))
            return 200, json.dumps([d for d in fixture_json(f"{product}.json") if d["id"] in ids])
        if product == "pirep":
            return 200, fixture_text("pirep.json")
        if product == "airsigmet":
            return 200, fixture_text("airsigmet.json")
        if product == "gairmet":
            return 200, fixture_text("gairmet.json")
        if product == "cwa":
            return 204, ""
        if product == "windtemp":
            name = "windtemp_high.txt" if params.get("level") == "high" else "windtemp_all.txt"
            return 200, fixture_text(name)
        if product == "openmeteo":
            return 200, json.dumps(model_winds(params))
        if product == "alerts":
            return 200, fixture_text("nws_alerts_co.json")
        if product == "points":
            # Every point is Granby's grid cell: forecast office BOU, which is
            # also right for everything else the fixtures cover.
            return 200, fixture_text("nws_points_gnb.json")
        if product == "hourly":
            return 200, fixture_text("nws_hourly_gnb.json")
        if product == "afd_list":
            return 200, json.dumps({"@graph": [{"id": "afd-1"}]})
        if product == "product":
            return 200, json.dumps(fixture_json("afd_bou.json"))
        if product == "zone":
            return 200, json.dumps({"geometry": None})
        return 404, "not routed in tests/fakeapi.py"


def all_metars() -> list[dict]:
    """The airport METARs plus the mountain stations, one report per station."""
    out = {d["icaoId"]: d for d in fixture_json("metar_mountain.json")}
    out.update({d["icaoId"]: d for d in fixture_json("metar.json")})
    return list(out.values())


def model_winds(params: dict) -> list[dict]:
    """Open-Meteo's reply for the requested points. The fixture is the real
    reply for five sites (DEN, GJT, PUB, ALS, ABQ) on the fixture date; every
    point gets the nearest of them, so the one-entry-per-point shape is real
    and the sites the tests look at get their own data."""
    captured = fixture_json("openmeteo_gfs.json")
    lats = [float(v) for v in params["latitude"].split(",")]
    lons = [float(v) for v in params["longitude"].split(",")]
    return [
        min(captured, key=lambda e: (e["latitude"] - lat) ** 2 + (e["longitude"] - lon) ** 2)
        for lat, lon in zip(lats, lons)
    ]


def _nws_product(url: str) -> str:
    if "/alerts/active" in url:
        return "alerts"
    if url.endswith("/forecast/hourly"):
        return "hourly"
    if "/points/" in url:
        return "points"
    if "/products/types/AFD" in url:
        return "afd_list"
    if "/products/" in url:
        return "product"
    if "/zones/" in url:
        return "zone"
    return url


def clients(**overrides):
    http = FixtureHttp(**overrides)
    return types.SimpleNamespace(http=http, awc=Awc(http), nws=Nws(http))
