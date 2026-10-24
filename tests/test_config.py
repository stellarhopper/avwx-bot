from pathlib import Path

from bot import config


def test_db_path_precedence(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/tmp/x.db")
    monkeypatch.setenv("STATE_DIRECTORY", "/var/lib/avwx-bot")
    assert config.database_path() == Path("/tmp/x.db")
    # An empty DB_PATH (copied from .env.example) must not win over systemd's directory.
    monkeypatch.setenv("DB_PATH", "")
    assert config.database_path() == Path("/var/lib/avwx-bot/avwx.db")
    monkeypatch.delenv("STATE_DIRECTORY")
    assert config.database_path() == config.ROOT / "data" / "avwx.db"
