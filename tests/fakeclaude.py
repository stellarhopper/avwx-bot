"""A scripted stand-in for the Anthropic client, returning real SDK objects.

Each scripted response is the JSON the API would send. Requests are recorded
as deep copies, so a test can check exactly what the bot sent each time,
which is how append-only replay is verified.
"""

from __future__ import annotations

import copy
import itertools
import types

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
    def __init__(self, *script) -> None:
        self.script = list(script)
        self.requests: list[dict] = []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create))

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
