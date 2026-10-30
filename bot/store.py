"""SQLite persistence.

**Everything is keyed by guild_id.** The bot is built so any server can add it
and configure it for itself, which means no query may ever reach across
servers. A missing ``guild_id`` in a WHERE clause would be silent and severe,
so tests/test_store.py checks isolation explicitly.

A guild's configuration is stored as **one JSON document**, not as rows of
settings. The configurator produces a whole document, the import validates a
whole document against schema/config.schema.json, and rollback restores a
whole document — so the unit of storage matches the unit of change. Every
version that was ever applied is kept in ``guild_config_history``.

Calls are synchronous. A handful of writes per brief or alert, each well under
a millisecond, doesn't justify an async driver and the failure modes it brings.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id   INTEGER PRIMARY KEY,
    config     TEXT    NOT NULL,
    updated_by INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_config_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    config     TEXT    NOT NULL,
    updated_by INTEGER,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_history_guild
    ON guild_config_history (guild_id, id DESC);

-- One row per guild per local calendar day a brief went out. The scheduler
-- checks this before posting, so a restart at 06:30 can't post twice.
CREATE TABLE IF NOT EXISTS brief_log (
    guild_id   INTEGER NOT NULL,
    day        TEXT    NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    posted_at  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, day)
);

-- Alerts already posted, so each hazard is announced once per guild and later
-- updates edit that post instead of making a new one.
CREATE TABLE IF NOT EXISTS sent_alerts (
    guild_id    INTEGER NOT NULL,
    alert_key   TEXT    NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    thread_id   INTEGER,
    fingerprint TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    posted_at   INTEGER NOT NULL,
    expires_at  INTEGER,
    closed_at   INTEGER,
    PRIMARY KEY (guild_id, alert_key)
);
CREATE INDEX IF NOT EXISTS idx_sent_alerts_open
    ON sent_alerts (guild_id, closed_at);

-- Threads with a conversation with Claude. While one is open the bot answers
-- every message in it; closed_at is set when it goes idle or runs too long.
CREATE TABLE IF NOT EXISTS chat_threads (
    guild_id     INTEGER NOT NULL,
    thread_id    INTEGER NOT NULL,
    started_by   INTEGER NOT NULL,
    started_at   INTEGER NOT NULL,
    last_active  INTEGER NOT NULL,
    closed_at    INTEGER,
    close_reason TEXT,
    PRIMARY KEY (guild_id, thread_id)
);

-- The conversation as sent to the API, one row per message, in order.
-- Append-only: thinking blocks are only valid in the exact conversation that
-- produced them, so a message is never edited once stored.
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    thread_id  INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_thread
    ON chat_messages (guild_id, thread_id, id);

-- Spend per member per day, for the caps and for /avwx chat usage.
CREATE TABLE IF NOT EXISTS chat_usage (
    guild_id       INTEGER NOT NULL,
    day            TEXT    NOT NULL,
    user_id        INTEGER NOT NULL,
    requests       INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write    INTEGER NOT NULL DEFAULT 0,
    cache_read     INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_micro_usd INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, day, user_id)
);

-- Threads the bot created (forum posts for briefs and alerts), and which
-- airports they were about, so a bare /metar typed in one knows what to show.
CREATE TABLE IF NOT EXISTS post_context (
    thread_id  INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    airports   TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add a
# column to a database that already exists, so every one of these has to be
# applied separately.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = ()

# `meta` key, not a guild setting — see Store.dev_synced_guilds.
DEV_SYNC_KEY = "dev_synced_guilds"

# Old history rows past this many per guild are pruned on write. Rollback only
# ever needs the last few; the rest is just a slowly growing table.
HISTORY_KEEP = 20


def now() -> int:
    return int(time.time())


@dataclass(frozen=True, slots=True)
class ConfigVersion:
    id: int
    guild_id: int
    config: dict
    updated_by: int | None
    updated_at: int


@dataclass(frozen=True, slots=True)
class SentAlert:
    guild_id: int
    alert_key: str
    channel_id: int
    message_id: int
    thread_id: int | None
    fingerprint: str
    kind: str
    posted_at: int
    expires_at: int | None
    closed_at: int | None


@dataclass(frozen=True, slots=True)
class ChatThread:
    guild_id: int
    thread_id: int
    started_by: int
    started_at: int
    last_active: int
    closed_at: int | None
    close_reason: str | None


@dataclass(frozen=True, slots=True)
class ChatUsage:
    requests: int = 0
    input_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output_tokens: int = 0
    cost_micro_usd: int = 0

    @property
    def cost_usd(self) -> float:
        return self.cost_micro_usd / 1_000_000


@dataclass(frozen=True, slots=True)
class PostContext:
    thread_id: int
    guild_id: int
    kind: str
    airports: list[str]
    created_at: int


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _migrate(self) -> None:
        """Add columns missing from an existing database. Idempotent."""
        for table, column, declaration in _MIGRATIONS:
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                )

    def close(self) -> None:
        self._conn.close()

    # -------------------------------------------------------- internal state

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def dev_synced_guilds(self) -> list[int]:
        """Guilds that currently hold a guild-scoped copy of the command tree.

        Remembered across restarts because leaving dev mode has to undo it, and
        by then DEV_GUILD_ID is gone from the environment — without this, the
        guild copies linger next to the global ones and every command appears
        twice.
        """
        raw = self.get_meta(DEV_SYNC_KEY)
        if not raw:
            return []
        try:
            return [int(x) for x in json.loads(raw)]
        except (ValueError, TypeError):
            return []

    def set_dev_synced_guilds(self, guild_ids: Iterable[int]) -> None:
        self.set_meta(DEV_SYNC_KEY, json.dumps(sorted(set(guild_ids))))

    # ------------------------------------------------------------ guild config

    def get_config(self, guild_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT config FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return json.loads(row["config"]) if row else None

    def configured_guilds(self) -> list[int]:
        return [
            row["guild_id"]
            for row in self._conn.execute("SELECT guild_id FROM guild_config")
        ]

    def set_config(self, guild_id: int, config: dict, *, actor_id: int | None) -> None:
        raw = json.dumps(config, sort_keys=True)
        at = now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO guild_config (guild_id, config, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (guild_id) DO UPDATE SET "
                "config = excluded.config, updated_by = excluded.updated_by, "
                "updated_at = excluded.updated_at",
                (guild_id, raw, actor_id, at),
            )
            self._conn.execute(
                "INSERT INTO guild_config_history (guild_id, config, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, raw, actor_id, at),
            )
            self._conn.execute(
                "DELETE FROM guild_config_history WHERE guild_id = ? AND id NOT IN ("
                "SELECT id FROM guild_config_history WHERE guild_id = ? "
                "ORDER BY id DESC LIMIT ?)",
                (guild_id, guild_id, HISTORY_KEEP),
            )

    def config_history(self, guild_id: int, limit: int = 10) -> list[ConfigVersion]:
        rows = self._conn.execute(
            "SELECT * FROM guild_config_history WHERE guild_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
        return [
            ConfigVersion(
                id=row["id"],
                guild_id=row["guild_id"],
                config=json.loads(row["config"]),
                updated_by=row["updated_by"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def forget_guild(self, guild_id: int) -> None:
        """Remove everything held for a guild, e.g. after the bot is kicked."""
        with self._conn:
            for table in (
                "guild_config",
                "guild_config_history",
                "brief_log",
                "sent_alerts",
                "post_context",
                "chat_threads",
                "chat_messages",
                "chat_usage",
            ):
                self._conn.execute(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))

    # ------------------------------------------------------------------ briefs

    def brief_sent(self, guild_id: int, day: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM brief_log WHERE guild_id = ? AND day = ?",
                (guild_id, day),
            ).fetchone()
            is not None
        )

    def record_brief(
        self, guild_id: int, day: str, *, channel_id: int | None, message_id: int | None
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO brief_log "
                "(guild_id, day, channel_id, message_id, posted_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, day, channel_id, message_id, now()),
            )

    # ------------------------------------------------------------------ alerts

    def get_alert(self, guild_id: int, alert_key: str) -> SentAlert | None:
        row = self._conn.execute(
            "SELECT * FROM sent_alerts WHERE guild_id = ? AND alert_key = ?",
            (guild_id, alert_key),
        ).fetchone()
        return SentAlert(**dict(row)) if row else None

    def open_alerts(self, guild_id: int) -> list[SentAlert]:
        rows = self._conn.execute(
            "SELECT * FROM sent_alerts WHERE guild_id = ? AND closed_at IS NULL",
            (guild_id,),
        ).fetchall()
        return [SentAlert(**dict(row)) for row in rows]

    def last_alert_at(self, guild_id: int, kind: str) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(posted_at) AS at FROM sent_alerts WHERE guild_id = ? AND kind = ?",
            (guild_id, kind),
        ).fetchone()
        return row["at"] if row else None

    def record_alert(self, alert: SentAlert) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sent_alerts (guild_id, alert_key, channel_id, "
                "message_id, thread_id, fingerprint, kind, posted_at, expires_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    alert.guild_id,
                    alert.alert_key,
                    alert.channel_id,
                    alert.message_id,
                    alert.thread_id,
                    alert.fingerprint,
                    alert.kind,
                    alert.posted_at,
                    alert.expires_at,
                    alert.closed_at,
                ),
            )

    def update_alert(
        self,
        guild_id: int,
        alert_key: str,
        *,
        fingerprint: str | None = None,
        expires_at: int | None = None,
        closed_at: int | None = None,
    ) -> None:
        sets, args = [], []
        if fingerprint is not None:
            sets.append("fingerprint = ?")
            args.append(fingerprint)
        if expires_at is not None:
            sets.append("expires_at = ?")
            args.append(expires_at)
        if closed_at is not None:
            sets.append("closed_at = ?")
            args.append(closed_at)
        if not sets:
            return
        with self._conn:
            self._conn.execute(
                f"UPDATE sent_alerts SET {', '.join(sets)} "
                "WHERE guild_id = ? AND alert_key = ?",
                (*args, guild_id, alert_key),
            )

    def prune_alerts(self, older_than: int) -> int:
        """Drop closed alerts past retention. Returns how many went."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM sent_alerts WHERE closed_at IS NOT NULL AND closed_at < ?",
                (older_than,),
            )
        return cur.rowcount

    # ------------------------------------------------------------ post context

    def set_post_context(
        self, thread_id: int, guild_id: int, kind: str, airports: Iterable[str]
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO post_context "
                "(thread_id, guild_id, kind, airports, created_at) VALUES (?, ?, ?, ?, ?)",
                (thread_id, guild_id, kind, json.dumps(list(airports)), now()),
            )

    def get_post_context(self, guild_id: int, thread_id: int) -> PostContext | None:
        row = self._conn.execute(
            "SELECT * FROM post_context WHERE guild_id = ? AND thread_id = ?",
            (guild_id, thread_id),
        ).fetchone()
        if row is None:
            return None
        return PostContext(
            thread_id=row["thread_id"],
            guild_id=row["guild_id"],
            kind=row["kind"],
            airports=json.loads(row["airports"]),
            created_at=row["created_at"],
        )

    # -------------------------------------------------------------------- chat

    def get_chat_thread(self, guild_id: int, thread_id: int) -> ChatThread | None:
        row = self._conn.execute(
            "SELECT * FROM chat_threads WHERE guild_id = ? AND thread_id = ?",
            (guild_id, thread_id),
        ).fetchone()
        return ChatThread(**dict(row)) if row else None

    def open_chat_thread(self, guild_id: int, thread_id: int, user_id: int, *, fresh: bool = False) -> None:
        """Start listening to a thread, or start again after it went idle.

        `fresh` forgets the conversation so far: for a thread closed because it
        ran too long, picking it up again starts over.
        """
        at = now()
        with self._conn:
            if fresh:
                self._conn.execute(
                    "DELETE FROM chat_messages WHERE guild_id = ? AND thread_id = ?",
                    (guild_id, thread_id),
                )
            self._conn.execute(
                "INSERT INTO chat_threads (guild_id, thread_id, started_by, started_at, last_active) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (guild_id, thread_id) DO UPDATE SET "
                "last_active = excluded.last_active, closed_at = NULL, close_reason = NULL"
                + (", started_by = excluded.started_by, started_at = excluded.started_at" if fresh else ""),
                (guild_id, thread_id, user_id, at, at),
            )

    def touch_chat_thread(self, guild_id: int, thread_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE chat_threads SET last_active = ? WHERE guild_id = ? AND thread_id = ?",
                (now(), guild_id, thread_id),
            )

    def close_chat_thread(self, guild_id: int, thread_id: int, reason: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE chat_threads SET closed_at = ?, close_reason = ? "
                "WHERE guild_id = ? AND thread_id = ? AND closed_at IS NULL",
                (now(), reason, guild_id, thread_id),
            )

    def append_chat_messages(self, guild_id: int, thread_id: int, messages: Iterable[dict]) -> None:
        at = now()
        with self._conn:
            self._conn.executemany(
                "INSERT INTO chat_messages (guild_id, thread_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (guild_id, thread_id, m["role"], json.dumps(m["content"]), at)
                    for m in messages
                ],
            )

    def chat_messages(self, guild_id: int, thread_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT role, content FROM chat_messages WHERE guild_id = ? AND thread_id = ? ORDER BY id",
            (guild_id, thread_id),
        ).fetchall()
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    def add_chat_usage(self, guild_id: int, day: str, user_id: int, usage: ChatUsage) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO chat_usage (guild_id, day, user_id, requests, input_tokens, cache_write, "
                "cache_read, output_tokens, cost_micro_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (guild_id, day, user_id) DO UPDATE SET "
                "requests = requests + excluded.requests, "
                "input_tokens = input_tokens + excluded.input_tokens, "
                "cache_write = cache_write + excluded.cache_write, "
                "cache_read = cache_read + excluded.cache_read, "
                "output_tokens = output_tokens + excluded.output_tokens, "
                "cost_micro_usd = cost_micro_usd + excluded.cost_micro_usd",
                (guild_id, day, user_id, usage.requests, usage.input_tokens, usage.cache_write,
                 usage.cache_read, usage.output_tokens, usage.cost_micro_usd),
            )

    def chat_usage(self, guild_id: int, since_day: str, until_day: str | None = None,
                   user_id: int | None = None) -> ChatUsage:
        """Totals from `since_day` to `until_day` inclusive (ISO dates), for one
        member or the whole guild."""
        sql = (
            "SELECT COALESCE(SUM(requests), 0) AS requests, COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(cache_write), 0) AS cache_write, COALESCE(SUM(cache_read), 0) AS cache_read, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cost_micro_usd), 0) AS cost_micro_usd "
            "FROM chat_usage WHERE guild_id = ? AND day >= ? AND day <= ?"
        )
        args: list = [guild_id, since_day, until_day or since_day]
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        return ChatUsage(**dict(self._conn.execute(sql, args).fetchone()))

    def prune_chat(self, older_than: int) -> int:
        """Forget conversations closed before `older_than`. Usage stays."""
        with self._conn:
            stale = self._conn.execute(
                "SELECT guild_id, thread_id FROM chat_threads WHERE closed_at IS NOT NULL AND closed_at < ?",
                (older_than,),
            ).fetchall()
            for row in stale:
                self._conn.execute(
                    "DELETE FROM chat_messages WHERE guild_id = ? AND thread_id = ?",
                    (row["guild_id"], row["thread_id"]),
                )
                self._conn.execute(
                    "DELETE FROM chat_threads WHERE guild_id = ? AND thread_id = ?",
                    (row["guild_id"], row["thread_id"]),
                )
        return len(stale)
