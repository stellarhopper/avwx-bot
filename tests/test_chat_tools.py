import json
from pathlib import Path

from bot import guildcfg
from bot.chat import tools
from bot.fleet import FleetCache
from conftest import FIXTURE_TIME
from fakeapi import clients

CLUB = Path(__file__).parent / "fixtures" / "club"


def ctx() -> tools.ToolContext:
    c = clients()
    cfg = guildcfg.effective({"schema_version": 1, "timezone": "America/Denver"})
    return tools.ToolContext(c.awc, c.nws, FleetCache(CLUB), cfg, lambda: FIXTURE_TIME)


async def call(name, args, context=None):
    content, is_error = await tools.run(context or ctx(), name, args)
    return (json.loads(content) if isinstance(content, str) and content.startswith("{") else content), is_error


async def test_plan_waits_for_unclear_points():
    out, err = await call("flight_plan", {"route": ["KGJT", "Delta", "KMTJ"],
                                          "departure_local": "2026-10-24 01:00", "cruise_altitude_ft": 9500})
    assert not err and out["status"] == "needs the member's help"
    assert out["unclear_points"][0]["input"] == "Delta"


async def test_mountain_plan_attention_and_fuel():
    out, err = await call("flight_plan", {
        "route": ["KBJC", "Rollins pass", "Granby", "Kremmling", "SBS", "KBJC"], "tail": "N9999X",
        "departure_local": "2026-10-24 00:45", "cruise_altitude_ft": 13500, "stops": ["Granby", "SBS"],
    })
    assert not err
    assert out["route"] == "KBJC → Rollins Pass (VPROL) → KGNB → K20V → KSBS → KBJC"
    assert [f["to"] for f in out["flights"]] == ["KGNB", "KSBS", "KBJC"]
    assert out["reserve_minutes"] == 45 and out["flights"][0]["fuel_gal"]["reserve"] == 7.9
    text = " | ".join(out["attention"])
    assert "Rollins Pass: caution" in text and "Mountain wave risk elevated" in text
    assert "less than 2,000 ft above Rollins Pass" in text
    assert out["departure"] == "Sat 00:45 MDT (24 0645Z)"


async def test_granby_takeoff_is_a_lower_bound():
    out, err = await call("takeoff_landing", {"tail": "N9999X", "airport": "Granby", "weight_lb": 2800})
    assert not err
    assert out["results"]["takeoff"]["lower_bound"]
    assert any("beyond the POH table" in a for a in out["attention"])
    assert out["inputs"]["temp_source"] == "METAR"


async def test_weight_and_balance_reports_each_limit():
    out, err = await call("weight_and_balance", {"tail": "N9999X", "fuel_gal": 60, "burn_gal": 15, "loads": [
        {"station": "front", "lb": 400}, {"station": "rear", "lb": 300}, {"station": "baggage", "lb": 100}]})
    assert not err and out["takeoff"]["weight_lb"] == 2953
    assert any("over the 2800 lb limit by 153 lb" in a for a in out["attention"])


async def test_inputs_are_checked_again():
    for name, args, fragment in [
        ("flight_plan", {"route": ["KBJC", "KGNB"], "departure_local": "tomorrow", "cruise_altitude_ft": 9500}, "look like"),
        ("flight_plan", {"route": ["KBJC", "KGNB"], "departure_local": "2027-01-01 08:00", "cruise_altitude_ft": 9500}, "7 days"),
        ("resolve_route", {"points": ["KBJC", "http://evil.example/x?y=1"]}, "isn't a place"),
        ("aircraft", {"tail": "N1"}, "Club aircraft: N9999X"),
        ("read_manual_pages", {"tail": "N9999X", "manual": "poh", "first_page": 1, "last_page": 9}, "At most"),
        ("read_manual_pages", {"tail": "N9999X", "manual": "../../etc", "first_page": 1, "last_page": 1}, "No manual"),
        ("nope", {}, "no tool"),
    ]:
        out, err = await call(name, args)
        assert err and fragment in out, (name, out)


async def test_manual_pages_come_back_as_a_pdf_document():
    out, err = await call("read_manual_pages", {"tail": "N9999X", "manual": "poh", "first_page": 2, "last_page": 2})
    assert not err
    assert out[0]["type"] == "text" and "not instructions" in out[0]["text"]
    assert out[1]["type"] == "document" and out[1]["source"]["media_type"] == "application/pdf"


async def test_cards_attach_and_are_capped():
    c = ctx()
    for _ in range(tools.MAX_CARDS_PER_TURN):
        out, err = await call("attach_card", {"kind": "metar", "airports": ["KBJC"]}, c)
        assert not err
    out, err = await call("attach_card", {"kind": "metar", "airports": ["KBJC"]}, c)
    assert err and "At most" in out
    assert [a.filename for a in c.attachments] == ["metar.png", "metar.png"]
    assert c.attachments[0].data.startswith(b"\x89PNG")


def test_every_parameter_is_required_and_optional_ones_take_null():
    for t in tools.DEFINITIONS:
        schema = t["input_schema"]
        assert set(schema["required"]) == set(schema["properties"]), t["name"]
    plan = next(t for t in tools.DEFINITIONS if t["name"] == "flight_plan")["input_schema"]["properties"]
    assert plan["tail"]["type"] == ["string", "null"]
    assert plan["route"]["type"] == "array"  # required stays non-null
    surface = next(t for t in tools.DEFINITIONS if t["name"] == "takeoff_landing")["input_schema"]["properties"]["surface"]
    assert surface["anyOf"] == [{"type": "string", "enum": ["paved", "grass"]}, {"type": "null"}]


async def test_handlers_treat_null_as_not_given():
    out, err = await call("flight_plan", {"route": ["KBJC", "KGNB"], "departure_local": "2026-10-24 00:45",
                                          "cruise_altitude_ft": 13500, "tail": None, "power_pct": None, "stops": None})
    assert not err and out["performance"]["assumed"]
    out, err = await call("takeoff_landing", {"tail": "N9999X", "airport": "KGNB", "weight_lb": 2600,
                                              "at_local": None, "runway": None, "surface": None})
    assert not err and out["inputs"]["surface"] == "paved"
