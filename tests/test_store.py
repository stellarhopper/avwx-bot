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


def test_chat_threads_and_messages_are_per_guild():
    from bot.store import ChatUsage

    s = Store(":memory:")
    s.open_chat_thread(1, 50, 7)
    s.append_chat_messages(1, 50, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}])
    assert s.get_chat_thread(2, 50) is None
    assert s.chat_messages(2, 50) == []
    assert s.chat_messages(1, 50)[1]["content"][0]["text"] == "hello"

    s.close_chat_thread(1, 50, "idle")
    assert s.get_chat_thread(1, 50).close_reason == "idle"
    s.open_chat_thread(1, 50, 8)  # picked up again: history kept, starter kept
    t = s.get_chat_thread(1, 50)
    assert t.closed_at is None and t.started_by == 7 and len(s.chat_messages(1, 50)) == 2
    s.open_chat_thread(1, 50, 8, fresh=True)  # after running too long: start over
    assert s.chat_messages(1, 50) == [] and s.get_chat_thread(1, 50).started_by == 8

    s.add_chat_usage(1, "2026-10-30", 7, ChatUsage(requests=1, cost_micro_usd=120_000))
    s.add_chat_usage(1, "2026-10-30", 7, ChatUsage(requests=1, cost_micro_usd=30_000))
    s.add_chat_usage(1, "2026-10-30", 8, ChatUsage(requests=1, cost_micro_usd=1_000))
    s.add_chat_usage(2, "2026-10-30", 7, ChatUsage(requests=5, cost_micro_usd=9_000_000))
    assert s.chat_usage(1, "2026-10-30", user_id=7).requests == 2
    assert s.chat_usage(1, "2026-10-30").cost_usd == 0.151
    assert s.chat_usage(1, "2026-10-01", "2026-10-31").requests == 3

    s.forget_guild(1)
    assert s.get_chat_thread(1, 50) is None and s.chat_usage(1, "2026-10-30").requests == 0
    assert s.chat_usage(2, "2026-10-30").requests == 5


def test_prune_chat_keeps_open_threads():
    s = Store(":memory:")
    s.open_chat_thread(1, 50, 7)
    s.open_chat_thread(1, 51, 7)
    s.append_chat_messages(1, 51, [{"role": "user", "content": "x"}])
    s.close_chat_thread(1, 51, "idle")
    assert s.prune_chat(older_than=10**12) == 1
    assert s.get_chat_thread(1, 50) is not None
    assert s.get_chat_thread(1, 51) is None and s.chat_messages(1, 51) == []
