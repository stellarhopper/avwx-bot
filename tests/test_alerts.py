import json
import types
from datetime import datetime, timedelta, timezone

import discord

from bot import alerting, guildcfg
from bot.cogs import alerts as alerts_cog
from bot.sources.nws import Alert
from bot.store import Store
from conftest import fixture_json
from fakeapi import clients
from fakes import FakeChannel, FakeGuild, FakeInteraction, role

NOW = datetime(2026, 10, 24, 6, 40, tzinfo=timezone.utc)
UTC = timezone.utc


def warning(event="Severe Thunderstorm Warning", etn="0045", action="NEW", expires="2026-10-24T07:30:00+00:00",
            polygon=((39.8, -105.2), (39.8, -104.9), (40.0, -104.9), (40.0, -105.2)), gust="60 MPH"):
    ring = [[lon, lat] for lat, lon in polygon] + [[polygon[0][1], polygon[0][0]]]
    return {
        "id": f"urn:{etn}:{action}:{expires}",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {
            "id": f"urn:{etn}:{action}:{expires}",
            "event": event,
            "messageType": "Alert" if action == "NEW" else "Update",
            "urgency": "Immediate",
            "severity": "Severe",
            "certainty": "Observed",
            "areaDesc": "Boulder, CO; Broomfield, CO",
            "expires": expires,
            "description": "At 1240 AM MDT, a severe thunderstorm was located over Boulder.",
            "parameters": {"VTEC": [f"/O.{action}.KBOU.SV.W.{etn}.261024T0640Z-261024T0730Z/"],
                           "maxWindGust": [gust], "maxHailSize": ["1.00"],
                           "NWSheadline": ["SEVERE THUNDERSTORM WARNING FOR BOULDER COUNTY"]},
            "references": [],
        },
    }


def alerts_payload(*features):
    return 200, json.dumps({"features": list(features)})


def metar_payload(raw_overrides):
    def respond(params):
        ids = set(params["ids"].split(","))
        out = []
        for d in fixture_json("metar.json"):
            if d["icaoId"] in ids:
                out.append({**d, **raw_overrides.get(d["icaoId"], {}), "obsTime": int(NOW.timestamp()) - 300})
        return 200, json.dumps(out)
    return respond


# ------------------------------------------------------------------ pure


def test_vtec_key_is_stable_across_updates():
    new = Alert.from_feature(warning(action="NEW"))
    con = Alert.from_feature(warning(action="CON", expires="2026-10-24T07:45:00+00:00"))
    assert alerting.vtec_key(new) == ("vtec:KBOU.SV.W.0045", False)
    assert alerting.vtec_key(con)[0] == "vtec:KBOU.SV.W.0045"
    assert alerting.vtec_key(Alert.from_feature(warning(action="CAN")))[1] is True


def test_nws_candidates_filter_by_event_and_end():
    items = [Alert.from_feature(warning()), Alert.from_feature(warning(event="Flood Watch", etn="0001")),
             Alert.from_feature(warning(etn="0046", action="EXP"))]
    found = alerting.nws_candidates(items, ["Severe Thunderstorm Warning"], NOW)
    assert [c.key for c in found] == ["vtec:KBOU.SV.W.0045"]
    assert "Gusts 60 mph" in found[0].lines[1] and "hail 1.00 in" in found[0].lines[1]


def test_real_feed_flood_warnings_only_when_asked():
    feed = [Alert.from_feature(f) for f in fixture_json("nws_alerts_co.json")["features"]]
    assert alerting.nws_candidates(feed, ["Tornado Warning"], NOW) == []
    floods = alerting.nws_candidates(feed, ["Flood Warning"], NOW)
    assert floods and all(c.key.startswith("vtec:") for c in floods)


def test_metar_candidates():
    from bot.sources.awc import Metar
    base = fixture_json("metar.json")[3]  # KDEN, gusting 17
    m = Metar.from_api({**base, "wxString": "+TSRA", "wgst": 41, "obsTime": int(NOW.timestamp()) - 600})
    found = alerting.metar_candidates([m], thunderstorm=True, gust_kt=35, funnel=True, now=NOW)
    assert {c.key for c in found} == {"metar-ts:KDEN", "metar-gust:KDEN"}
    stale = Metar.from_api({**base, "wxString": "TS", "obsTime": int(NOW.timestamp()) - 3 * 3600})
    assert alerting.metar_candidates([stale], thunderstorm=True, gust_kt=0, funnel=True, now=NOW) == []


def test_quiet_hours_wrap_midnight():
    q = {"start": "22:00", "end": "06:00"}
    assert alerting.in_quiet_hours(datetime(2026, 1, 1, 23, 0), q)
    assert alerting.in_quiet_hours(datetime(2026, 1, 1, 5, 59), q)
    assert not alerting.in_quiet_hours(datetime(2026, 1, 1, 6, 0), q)
    assert not alerting.in_quiet_hours(datetime(2026, 1, 1, 12, 0), None)


def test_cooldown():
    assert alerting.cooled_down(None, NOW, 30)
    assert not alerting.cooled_down(int(NOW.timestamp()) - 600, NOW, 30)
    assert alerting.cooled_down(int(NOW.timestamp()) - 1900, NOW, 30)


def test_message_text_pings_only_when_asked():
    c = alerting.Candidate("k", "nws", "Tornado Warning", ["Boulder"], NOW + timedelta(minutes=30), 3)
    assert "<@&42>" in alerting.message_text(c, UTC, ping_role=42)
    assert "<@&" not in alerting.message_text(c, UTC, ping_role=None)


# ---------------------------------------------------------------- watcher


def setup(channel_kind=discord.ChannelType.text, **http):
    api = clients(**http)
    channel = FakeChannel("alerts", kind=channel_kind)
    pilots = role("Pilots")
    guild = FakeGuild([channel], [pilots])
    bot = types.SimpleNamespace(store=Store(":memory:"), awc=api.awc, nws=api.nws, http_client=api.http, guilds=[guild])
    cfg = guildcfg.effective({
        "schema_version": 1,
        "timezone": "America/Denver",
        "alerts": {"enabled": True, "channel_id": str(channel.id), "mention_role_id": str(pilots.id),
                   "area": {"airports": ["KBJC", "KDEN"], "radius_nm": 20},
                   "triggers": {"convective_sigmet": False, "cwa": False}},
    })
    return types.SimpleNamespace(api=api, bot=bot, guild=guild, channel=channel, cog=alerts_cog.Alerts(bot), cfg=cfg, role=pilots)


async def test_new_warning_is_posted_once_with_a_ping():
    s = setup(alerts=alerts_payload(warning()))
    await s.cog.check_guild(s.guild, s.cfg, NOW)
    await s.cog.check_guild(s.guild, s.cfg, NOW + timedelta(minutes=3))
    [msg] = s.channel.sent
    assert "Severe Thunderstorm Warning" in msg.content and f"<@&{s.role.id}>" in msg.content
    assert msg.file.filename == "alert.png"


async def test_updated_warning_edits_the_post_then_ends():
    s = setup(alerts=alerts_payload(warning()))
    await s.cog.check_guild(s.guild, s.cfg, NOW)
    s.api.http.overrides["alerts"] = alerts_payload(warning(action="CON", expires="2026-10-24T07:50:00+00:00", gust="70 MPH"))
    s.api.http._cache.clear()
    await s.cog.check_guild(s.guild, s.cfg, NOW + timedelta(minutes=3))
    [msg] = s.channel.sent
    assert msg.edits and "70 mph" in msg.edits[-1]["content"].lower()
    assert "updated" in msg.edits[-1]["content"]

    s.api.http.overrides["alerts"] = alerts_payload()
    s.api.http._cache.clear()
    await s.cog.check_guild(s.guild, s.cfg, NOW + timedelta(minutes=6))
    assert msg.edits[-1]["content"].startswith("✅ Ended")
    assert s.bot.store.open_alerts(s.guild.id) == []


async def test_failed_source_does_not_end_open_alerts():
    s = setup(alerts=alerts_payload(warning()))
    await s.cog.check_guild(s.guild, s.cfg, NOW)
    s.api.http.overrides["alerts"] = (503, "down")
    s.api.http._cache.clear()
    await s.cog.check_guild(s.guild, s.cfg, NOW + timedelta(minutes=3))
    assert len(s.bot.store.open_alerts(s.guild.id)) == 1


async def test_far_away_warning_is_ignored():
    far = warning(polygon=((37.0, -108.0), (37.0, -107.8), (37.2, -107.8), (37.2, -108.0)))
    s = setup(alerts=alerts_payload(far))
    await s.cog.check_guild(s.guild, s.cfg, NOW)
    assert s.channel.sent == []


async def test_metar_thunderstorm_respects_cooldown_and_quiet_hours():
    s = setup(alerts=alerts_payload(), metar=metar_payload({"KBJC": {"wxString": "TSRA"}}))
    s.cfg["alerts"]["quiet_hours"] = {"start": "00:00", "end": "06:00"}  # NOW is 00:40 MDT
    await s.cog.check_guild(s.guild, s.cfg, NOW)
    [msg] = s.channel.sent
    assert "Thunderstorm at KBJC" in msg.content and "<@&" not in msg.content
    # Storm clears, then comes back 10 minutes later: within the cooldown, no new post.
    s.api.http.overrides["metar"] = metar_payload({})
    s.api.http._cache.clear()
    await s.cog.check_guild(s.guild, s.cfg, NOW + timedelta(minutes=3))
    s.api.http.overrides["metar"] = metar_payload({"KBJC": {"wxString": "TSRA"}})
    s.api.http._cache.clear()
    await s.cog.check_guild(s.guild, s.cfg, NOW + timedelta(minutes=10))
    assert len(s.channel.sent) == 1


async def test_forum_alert_is_a_thread_that_gets_updates_and_locks():
    s = setup(channel_kind=discord.ChannelType.forum, alerts=alerts_payload(warning()))
    await s.cog.check_guild(s.guild, s.cfg, NOW)
    [thread] = s.channel.threads
    assert thread.name.startswith("⚠ Severe Thunderstorm Warning")
    s.api.http.overrides["alerts"] = alerts_payload()
    s.api.http._cache.clear()
    await s.cog.check_guild(s.guild, s.cfg, NOW + timedelta(minutes=3))
    assert thread.sent[-1].content.startswith("✅ Ended") and thread.locked


async def test_alerts_command_lists_without_posting():
    s = setup(alerts=alerts_payload(warning()))
    s.bot.store.set_config(s.guild.id, {"schema_version": 1, "home_airports": ["KBJC"]}, actor_id=None)
    i = FakeInteraction(s.guild)
    await s.cog.alerts.callback(s.cog, i)
    assert "Severe Thunderstorm Warning" in i.reply and "<@&" not in i.reply
    assert s.channel.sent == []
