"""The one place the bot reads the time.

Everything that asks "what's in force now?" goes through here, so tests can
pin the clock to the moment the fixtures in tests/fixtures were captured.
Otherwise a test about a warning that expires at 07:30Z starts failing at
07:31Z.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)
