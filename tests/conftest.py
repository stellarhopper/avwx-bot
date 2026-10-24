"""Lets integration tests be written as `async def test_...`.

Eight lines here beats adding pytest-asyncio as a dependency for the handful of
tests that drive command callbacks.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function):
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(func(**kwargs))
    return True


import json
from datetime import datetime, timezone
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# When the fixtures were captured. Code under test that asks "what's in force
# now?" sees this, so a warning in a fixture doesn't expire in the middle of CI.
FIXTURE_TIME = datetime(2026, 10, 24, 6, 40, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    from bot import clock

    monkeypatch.setattr(clock, "now", lambda: FIXTURE_TIME)
