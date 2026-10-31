"""A scripted stand-in for the Anthropic client, returning real SDK objects.

Each scripted response is the JSON the API would send. Requests are recorded
as deep copies, so a test can check exactly what the bot sent each time,
which is how append-only replay is verified.
"""

from __future__ import annotations

import copy
import itertools
import types

from anthropic.types import Message
from anthropic.types.beta import BetaMessage

_ids = itertools.count(1)


def message(*content, stop="end_turn", usage=None) -> dict:
    return {
        "id": f"msg_{next(_ids)}", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "stop_reason": stop, "stop_sequence": None, "content": list(content),
        "usage": usage or {"input_tokens": 1000, "output_tokens": 200,
                           "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 0},
    }


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def thinking(sig: str = "sig-1") -> dict:
    return {"type": "thinking", "thinking": "", "signature": sig}


def tool(name: str, args: dict, call_id: str | None = None) -> dict:
    return {"type": "tool_use", "id": call_id or f"toolu_{next(_ids)}", "name": name, "input": args}


class FakeClaude:
    """`script` answers the main model's requests in order. `gate` answers
    the reply check (bot/chat/gate.py) in order: True, False, or an
    exception. With `gate` used up, the check says reply."""

    def __init__(self, *script, gate=()) -> None:
        self.script = list(script)
        self.gate = list(gate)
        self.requests: list[dict] = []
        self.gate_requests: list[dict] = []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create))
        self.messages = types.SimpleNamespace(create=self._gate_create)

    async def _gate_create(self, **kwargs):
        self.gate_requests.append(copy.deepcopy(kwargs))
        verdict = self.gate.pop(0) if self.gate else True
        if isinstance(verdict, Exception):
            raise verdict
        body = message(text('{"reply": %s}' % ("true" if verdict else "false")),
                       usage={"input_tokens": 400, "output_tokens": 6})
        body["model"] = "claude-haiku-5-5"
        return Message.model_validate(body)

    async def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if not self.script:
            raise AssertionError("FakeClaude ran out of scripted responses")
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if callable(nxt):
            nxt = nxt(kwargs)
        return BetaMessage.model_validate(nxt)
