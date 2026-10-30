import json

import pytest

from bot import guildcfg, regions
from bot.guildcfg import ChannelInfo, ConfigError

GOOD = {
    "schema_version": 1,
    "timezone": "America/Denver",
    "home_airports": ["KBJC", "KAPA"],
    "brief": {"enabled": True, "channel_id": "123456789012345678", "time": "06:30", "airports": ["KBJC"]},
    "alerts": {"enabled": True, "channel_id": "223456789012345678", "area": {"airports": ["KBJC"], "radius_nm": 30}},
    "mountain": {"enabled": True, "region": "co_front_range", "passes": ["berthoud", "kenosha"]},
}


def test_defaults_are_valid():
    assert guildcfg.validate(guildcfg.defaults()) == []


def test_defaults_come_from_schema():
    d = guildcfg.defaults()
    assert d["brief"]["time"] == "06:30"
    assert d["brief"]["enabled"] is False
    assert d["alerts"]["triggers"]["metar_gust_kt"] == 35
    assert d["commands"]["metar"] is True


def test_effective_overlays_stored_values():
    eff = guildcfg.effective({"schema_version": 1, "brief": {"time": "07:15"}})
    assert eff["brief"]["time"] == "07:15"
    assert eff["brief"]["include"]["tafs"] is True


def test_good_config_validates():
    assert guildcfg.validate(GOOD) == []


@pytest.mark.parametrize(
    "patch, fragment",
    [
        ({"brief": {"time": "6:30"}}, "brief.time"),
        ({"home_airports": ["K!X"]}, "home_airports.0"),
        ({"surprise": 1}, "Additional properties"),
        ({"brief": {"channel_id": 123456789012345678}}, "brief.channel_id"),
        ({"alerts": {"area": {"radius_nm": 500}}}, "alerts.area.radius_nm"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_bad_configs_name_the_setting(patch, fragment):
    doc = {"schema_version": 1, **patch}
    problems = guildcfg.validate(doc)
    assert problems and any(fragment in p for p in problems), problems


def test_parse_import_uppercases_and_strips_hints():
    raw = json.dumps({**GOOD, "home_airports": ["kbjc"], "_guild": {"name": "x"}}).encode()
    doc = guildcfg.parse_import(raw)
    assert doc["home_airports"] == ["KBJC"]
    assert "_guild" not in doc


def test_parse_import_errors():
    with pytest.raises(ConfigError, match="valid JSON"):
        guildcfg.parse_import(b"{nope")
    with pytest.raises(ConfigError, match="JSON object"):
        guildcfg.parse_import(b"[]")
    with pytest.raises(ConfigError, match="KB"):
        guildcfg.parse_import(b" " * (guildcfg.MAX_IMPORT_BYTES + 1))


def test_diff_lists_only_effective_changes():
    before = {"schema_version": 1}
    after = {"schema_version": 1, "brief": {"time": "07:00", "enabled": False}}
    assert guildcfg.diff(before, after) == [("brief.time", "06:30", "07:00")]
    assert guildcfg.diff(guildcfg.defaults(), {"schema_version": 1}) == []


def test_fragment_round_trip_with_unicode():
    doc = {**GOOD, "_guild": {"name": "Front Range ✈️ Pilots", "channels": [{"id": "1", "name": "météo"}]}}
    text = guildcfg.encode_fragment(doc)
    assert all(c.isalnum() or c in "-_" for c in text)
    assert guildcfg.decode_fragment(text) == doc


def _channels(mapping):
    return lambda cid: mapping.get(cid)


def check(doc, *, channels=None, roles=(), unknown=(), chat_allowed=True):
    return guildcfg.semantic_problems(
        doc,
        channel=_channels(channels or {}),
        role_exists=lambda rid: rid in roles,
        unknown_stations=unknown,
        known_regions=regions.all_regions().keys(),
        known_passes=regions.pass_ids,
        chat_allowed=chat_allowed,
    )


TEXT_OK = ChannelInfo("wx", "text", True, True, True)
FORUM_OK = ChannelInfo("briefs", "forum", True, True, True)


def test_semantic_ok():
    problems, warnings = check(GOOD, channels={123456789012345678: TEXT_OK, 223456789012345678: FORUM_OK})
    assert problems == [] and warnings == []


def test_semantic_missing_channel_and_permissions():
    no_attach = ChannelInfo("wx", "text", True, False, True)
    problems, _ = check(GOOD, channels={123456789012345678: no_attach})
    assert any("attach" in p for p in problems)
    assert any("223456789012345678" in p for p in problems)


def test_semantic_enabled_without_channel():
    problems, _ = check({"schema_version": 1, "home_airports": ["KBJC"], "brief": {"enabled": True}})
    assert problems == ["Morning brief is on but has no channel."]


def test_semantic_forum_tag_on_text_channel_warns():
    doc = {"schema_version": 1, "home_airports": ["KBJC"],
           "brief": {"enabled": True, "channel_id": "123456789012345678", "forum_tag_id": "323456789012345678"}}
    problems, warnings = check(doc, channels={123456789012345678: TEXT_OK})
    assert problems == [] and any("forum tag" in w for w in warnings)


def test_semantic_unknown_things():
    doc = {**GOOD, "mountain": {"enabled": True, "region": "co_front_range", "passes": ["nope"]},
           "alerts": {**GOOD["alerts"], "mention_role_id": "423456789012345678"}, "timezone": "Mars/Base"}
    problems, _ = check(doc, channels={123456789012345678: TEXT_OK, 223456789012345678: TEXT_OK}, unknown=["KXYZ"])
    text = " ".join(problems)
    assert "KXYZ" in text and "nope" in text and "mention role" in text and "Mars/Base" in text


def test_region_file_is_consistent():
    region = regions.get("co_front_range")
    ids = [p["id"] for p in region["passes"]]
    assert len(ids) == len(set(ids))
    for p in region["passes"]:
        assert p["winds"] in region["winds_aloft_sites"]
        assert p["on_pass"] or p["east"] or p["west"]


def test_semantic_chat_channels():
    doc = {"schema_version": 1, "chat": {"enabled": True}}
    assert check(doc)[0] == ["Chat is on but no channels are chosen."]
    no_threads = ChannelInfo("general", "text", True, True, True, can_create_threads=False)
    doc = {"schema_version": 1, "chat": {"enabled": True, "channel_ids": ["123456789012345678", "223456789012345678"]}}
    problems, _ = check(doc, channels={123456789012345678: no_threads, 223456789012345678: FORUM_OK})
    assert problems == ["Chat: I need Create Public Threads in #general to start a conversation there."]
    problems, _ = check(doc, channels={123456789012345678: TEXT_OK})
    assert problems == ["Chat: channel `223456789012345678` isn't in this server, or I can't see it."]


def test_chat_on_for_a_server_the_host_has_not_allowed_warns():
    doc = {"schema_version": 1, "chat": {"enabled": True, "channel_ids": ["223456789012345678"]}}
    channels = {223456789012345678: FORUM_OK}
    problems, warnings = check(doc, channels=channels, chat_allowed=False)
    assert problems == []  # it can still be applied
    assert any("CHAT_GUILD_IDS" in w for w in warnings)
    assert check(doc, channels=channels, chat_allowed=True) == ([], [])
    off = {"schema_version": 1, "chat": {"enabled": False}}
    assert check(off, chat_allowed=False) == ([], [])
