from bot.store import HISTORY_KEEP, SentAlert, Store


def store():
    return Store(":memory:")


def test_config_is_per_guild():
    s = store()
    s.set_config(1, {"schema_version": 1, "home_airports": ["KBJC"]}, actor_id=9)
    s.set_config(2, {"schema_version": 1, "home_airports": ["KASE"]}, actor_id=9)
    assert s.get_config(1)["home_airports"] == ["KBJC"]
    assert s.get_config(2)["home_airports"] == ["KASE"]
    assert s.get_config(3) is None
    assert sorted(s.configured_guilds()) == [1, 2]


def test_history_newest_first_and_pruned():
    s = store()
    for i in range(HISTORY_KEEP + 5):
        s.set_config(1, {"schema_version": 1, "n": i}, actor_id=None)
    s.set_config(2, {"schema_version": 1, "n": "other"}, actor_id=None)
    history = s.config_history(1, limit=100)
    assert len(history) == HISTORY_KEEP
    assert history[0].config["n"] == HISTORY_KEEP + 4
    assert all(v.guild_id == 1 for v in history)


def test_brief_log():
    s = store()
    assert not s.brief_sent(1, "2026-10-24")
    s.record_brief(1, "2026-10-24", channel_id=5, message_id=6)
    assert s.brief_sent(1, "2026-10-24")
    assert not s.brief_sent(2, "2026-10-24")


def test_alerts_open_update_close():
    s = store()
    s.record_alert(SentAlert(1, "k", 10, 11, None, "fp", "nws", 100, 200, None))
    assert [a.alert_key for a in s.open_alerts(1)] == ["k"]
    assert s.open_alerts(2) == []
    s.update_alert(1, "k", fingerprint="fp2")
    assert s.get_alert(1, "k").fingerprint == "fp2"
    s.update_alert(1, "k", closed_at=300)
    assert s.open_alerts(1) == []
    assert s.last_alert_at(1, "nws") == 100
    assert s.prune_alerts(older_than=400) == 1


def test_post_context_scoped_by_guild():
    s = store()
    s.set_post_context(55, 1, "brief", ["KBJC", "KAPA"])
    assert s.get_post_context(1, 55).airports == ["KBJC", "KAPA"]
    assert s.get_post_context(2, 55) is None


def test_forget_guild():
    s = store()
    s.set_config(1, {"schema_version": 1}, actor_id=None)
    s.set_config(2, {"schema_version": 1}, actor_id=None)
    s.forget_guild(1)
    assert s.get_config(1) is None and s.config_history(1) == []
    assert s.get_config(2) is not None


def test_dev_synced_guilds():
    s = store()
    assert s.dev_synced_guilds() == []
    s.set_dev_synced_guilds([3, 3, 1])
    assert s.dev_synced_guilds() == [1, 3]
