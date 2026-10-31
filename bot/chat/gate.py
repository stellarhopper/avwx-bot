"""Whether a message in an open chat thread is for the bot at all.

In an open thread the bot sees every message, and most aren't for it:
"thanks!", "see you at the fly-in", members talking to each other. Asking
the main model about each one costs an Opus call and shows a typing
indicator while it decides to say nothing. So a small, fast model looks at
the recent conversation first and answers one question: should the
assistant reply? Only a yes goes on to the main model.

Mentions never come here: a member who mentions the bot wants it.

If this check fails for any reason (an API error, a refusal, output that
doesn't parse), it answers yes. The main model can still decline with
NO_REPLY, so a failure costs an Opus call, never a missed question.
"""

from __future__ import annotations

import json
import logging
import re

import anthropic

from ..store import ChatUsage

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
# US dollars per million tokens (prompts under 100K), which is micro-dollars per token.
PRICE_INPUT = 0.10
PRICE_OUTPUT = 0.50
PRICE_CACHE_READ = 0.01
PRICE_CACHE_WRITE = 0.125
RECENT = 8  # conversation entries shown for context
CLIP = 500  # characters kept of each

SYSTEM = """\
You screen messages in a Discord thread where a flying club talks with an aviation assistant. \
The assistant answers questions about flight planning, weather, the club's aircraft, regulations \
and club operations. Members also talk among themselves in the same thread.

Decide whether the assistant should reply to the latest message(s).

Reply: yes when they ask the assistant something, follow up on its last answer (including short \
follow-ups like "what about at noon?" or "and Steamboat?"), correct it, or ask a flying or weather \
question the group would expect the assistant to answer.

Reply: no when members are talking to each other, making plans, joking, thanking or acknowledging \
("thanks", "ok", "got it", a reaction), or saying something that needs nothing from the assistant.

When unsure, say yes."""

SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "boolean"}},
    "required": ["reply"],
    "additionalProperties": False,
}

_TAG = re.compile(r"</?(member|thread_context|earlier_messages)\b[^>]*>")


def _text_of(message: dict) -> str:
    content = message["content"]
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def transcript(history: list[dict], latest: str) -> str:
    """The recent conversation as plain lines, tool traffic left out."""
    lines = []
    for m in history:
        if m["role"] not in ("user", "assistant"):
            continue
        text = " ".join(_TAG.sub(" ", _text_of(m)).split())
        if not text:
            continue  # tool calls and results
        who = "Assistant" if m["role"] == "assistant" else "Members"
        lines.append(f"{who}: {text[:CLIP]}")
    recent = "\n".join(lines[-RECENT:]) or "(no earlier messages)"
    return f"<conversation>\n{recent}\n</conversation>\n<latest>\n{latest}\n</latest>"


def cost_micro_usd(usage) -> int:
    return round(
        (getattr(usage, "input_tokens", 0) or 0) * PRICE_INPUT
        + (getattr(usage, "output_tokens", 0) or 0) * PRICE_OUTPUT
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * PRICE_CACHE_READ
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * PRICE_CACHE_WRITE
    )


async def should_reply(client, history: list[dict], latest: str) -> tuple[bool, ChatUsage]:
    """(reply?, what the check cost). `latest` is the new messages, already
    wrapped as the main model would see them."""
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=64,
            system=SYSTEM,
            messages=[{"role": "user", "content": transcript(history, latest)}],
            thinking={"type": "disabled"},
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except anthropic.APIError as exc:
        log.warning("reply check failed, so replying: %r", exc)
        return True, ChatUsage()
    # Gate calls aren't questions answered, so they don't count against a
    # member's daily requests; only their cost is kept.
    usage = ChatUsage(
        input_tokens=response.usage.input_tokens or 0,
        output_tokens=response.usage.output_tokens or 0,
        cost_micro_usd=cost_micro_usd(response.usage),
    )
    if response.stop_reason == "refusal":
        return True, usage
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        verdict = bool(json.loads(text)["reply"])
    except (ValueError, KeyError, TypeError):
        log.warning("reply check gave unreadable output, so replying: %r", text[:200])
        return True, usage
    log.info("reply check: %s", "reply" if verdict else "stay quiet")
    return verdict, usage
