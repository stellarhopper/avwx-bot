import types

import pytest

from bot import context, tree
from bot.store import Store


def test_parse_stations():
    assert context.parse_stations("kbjc apa, KDEN;KBJC", limit=4) == ["KBJC", "KAPA", "KDEN"]
    assert context.parse_stations("00C", limit=4) == ["00C"]  # FAA-only IDs are left alone


@pytest.mark.parametrize("text, fragment", [("", "at least one"), ("KBJC!!", "doesn't look"), ("KAAA KBBB KCCC KDDD KEEE", "up to 4")])
def test_parse_stations_errors(text, fragment):
    with pytest.raises(context.StationInputError, match=fragment):
        context.parse_stations(text, limit=4)


class Thread:
    pass


def test_resolve_prefers_typed_then_thread_then_home(monkeypatch):
    store = Store(":memory:")
    monkeypatch.setattr(context.discord, "Thread", Thread)
    thread = Thread()
    thread.id = 77
    store.set_post_context(77, 1, "brief", ["KASE", "KEGE"])
    i = types.SimpleNamespace(guild_id=1, channel=thread)
    assert context.resolve_stations(store, i, "kbjc", limit=4, fallback=["KDEN"]) == ["KBJC"]
    assert context.resolve_stations(store, i, None, limit=4, fallback=["KDEN"]) == ["KASE", "KEGE"]
    elsewhere = types.SimpleNamespace(guild_id=1, channel=object())
    assert context.resolve_stations(store, elsewhere, " ", limit=4, fallback=["KDEN"]) == ["KDEN"]
    with pytest.raises(context.StationInputError):
        context.resolve_stations(store, elsewhere, None, limit=4, fallback=[])


def test_command_switches():
    assert not tree.command_disabled(None, "metar")
    assert tree.command_disabled({"schema_version": 1, "commands": {"metar": False}}, "metar")
    assert not tree.command_disabled({"schema_version": 1, "commands": {"metar": False}}, "avwx")
