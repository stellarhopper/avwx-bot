"""One turn of a conversation: member messages in, a reply out.

The conversation lives in the store, exactly as sent to and received from the
API, and is only ever appended to. That matters beyond tidiness: Claude's
thinking blocks are valid only in the exact conversation that produced them,
so editing or reordering stored history would make the API reject it.

A turn is a short loop. Claude may call tools (all read-only, see tools.py);
their results go back in one message; after at most MAX_TOOL_ROUNDS rounds
the last request offers no tools, so a turn always ends in an answer.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from datetime import datetime

import anthropic

from ..store import ChatUsage, Store
from . import prompt, tools

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
MAX_TOOL_ROUNDS = 8
# A thread past either of these is closed after its reply, with a note to
# start a new conversation. Simpler than compacting, and flight-planning
# threads rarely run this long.
MAX_CONTEXT_TOKENS = 150_000
MAX_MEMBER_TURNS = 40
# Asks for Anthropic's recommended fallback model when a request is declined
# by a safety classifier, instead of returning the refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# US dollars per million tokens for claude-opus-5-5, which is also micro-dollars
# per token. A request served by a fallback model is billed at that model's
# rates; counting it at these is close enough for a daily cap.
PRICE_INPUT = 4.00
PRICE_OUTPUT = 20.00
PRICE_CACHE_READ = 0.20
PRICE_CACHE_WRITE = 5.00

SORRY = "Sorry, I couldn't get an answer just now. Try again in a minute."


@dataclass(frozen=True, slots=True)
class MemberMessage:
    author: str
    text: str
    sent: datetime


@dataclass
class Reply:
    text: str | None  # None: nothing to post (NO_REPLY)
    attachments: list[tools.Attachment] = field(default_factory=list)
    usage: ChatUsage = field(default_factory=ChatUsage)
    close_reason: str | None = None  # set when the thread should stop listening after this
    failed: bool = False


def wrap(messages: list[MemberMessage], tz, context: str | None = None) -> str:
    """Member messages as data for the model, with who said it and when.

    Angle brackets in what members type are escaped, so nobody can close the
    <member> tag and write something that looks like it came from elsewhere.
    """
    parts = []
    if context:
        parts.append(f"<thread_context>{html.escape(context)}</thread_context>")
    for m in messages:
        at = m.sent.astimezone(tz).strftime("%a %d %b %Y %H:%M %Z")
        parts.append(f'<member name="{html.escape(m.author, quote=True)}" at="{at}">{html.escape(m.text)}</member>')
    return "\n".join(parts)


def cost_micro_usd(usage) -> int:
    return round(
        (getattr(usage, "input_tokens", 0) or 0) * PRICE_INPUT
        + (getattr(usage, "output_tokens", 0) or 0) * PRICE_OUTPUT
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * PRICE_CACHE_READ
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * PRICE_CACHE_WRITE
    )


def _add(total: ChatUsage, usage) -> ChatUsage:
    return ChatUsage(
        requests=total.requests + 1,
        input_tokens=total.input_tokens + (usage.input_tokens or 0),
        cache_write=total.cache_write + (getattr(usage, "cache_creation_input_tokens", 0) or 0),
        cache_read=total.cache_read + (getattr(usage, "cache_read_input_tokens", 0) or 0),
        output_tokens=total.output_tokens + (usage.output_tokens or 0),
        cost_micro_usd=total.cost_micro_usd + cost_micro_usd(usage),
    )


def _why(exc: Exception) -> str:
    """What to tell members. Never includes the key or the raw error."""
    text = str(exc)
    if "credit balance" in text:
        return "The club's Anthropic account is out of credit, so I can't answer until an admin tops it up."
    if isinstance(exc, anthropic.AuthenticationError):
        return "My Anthropic API key was rejected. An admin needs to check it."
    if isinstance(exc, anthropic.RateLimitError):
        return "I'm being rate-limited. Try again in a minute."
    if isinstance(exc, anthropic.BadRequestError):
        return "The request was rejected. Starting a new conversation may help."
    return SORRY


def _member_turns(history: list[dict]) -> int:
    return sum(
        1 for m in history
        if m["role"] == "user" and isinstance(m["content"], list)
        and any(b.get("type") == "text" for b in m["content"])
    )


class Conversation:
    def __init__(self, client, store: Store, guild_id: int, thread_id: int, *, effort: str = "medium") -> None:
        self.client = client
        self.store = store
        self.guild_id = guild_id
        self.thread_id = thread_id
        self.effort = effort

    def _append(self, *messages: dict) -> None:
        self.store.append_chat_messages(self.guild_id, self.thread_id, messages)

    async def respond(
        self,
        members: list[MemberMessage],
        ctx: tools.ToolContext,
        *,
        context: str | None = None,
    ) -> Reply:
        system = prompt.system(ctx.fleet.get())
        history = self.store.chat_messages(self.guild_id, self.thread_id)
        user = {"role": "user", "content": [{"type": "text", "text": wrap(members, ctx.tz, context)}]}
        self._append(user)
        messages = [*history, user]
        usage = ChatUsage()
        response = None

        for round_no in range(MAX_TOOL_ROUNDS + 1):
            last_round = round_no == MAX_TOOL_ROUNDS
            try:
                response = await self.client.beta.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    tools=tools.DEFINITIONS,
                    tool_choice={"type": "none"} if last_round else {"type": "auto"},
                    messages=messages,
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.effort},
                    cache_control={"type": "ephemeral"},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                )
            except anthropic.APIError as exc:
                log.warning("chat request failed in thread %s: %r", self.thread_id, exc)
                return Reply(_why(exc), usage=usage, failed=True)
            usage = _add(usage, response.usage)
            log.info(
                "chat %s round %d: %s, in %s (+%s cached, %s written), out %s, request %s",
                self.thread_id, round_no, response.stop_reason, response.usage.input_tokens,
                getattr(response.usage, "cache_read_input_tokens", 0),
                getattr(response.usage, "cache_creation_input_tokens", 0),
                response.usage.output_tokens, getattr(response, "_request_id", None),
            )

            if response.stop_reason == "refusal":
                # Nothing stored: a declined answer shouldn't become history.
                return Reply("I can't help with that one.", usage=usage)

            assistant = {"role": "assistant", "content": [b.to_dict(mode="json") for b in response.content]}
            calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                self._append(assistant)
                messages.append(assistant)
                break

            outputs = await asyncio.gather(*(tools.run(ctx, c.name, c.input) for c in calls))
            results = {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": c.id, "content": content,
                     **({"is_error": True} if is_error else {})}
                    for c, (content, is_error) in zip(calls, outputs)
                ],
            }
            # The call and its results go in together, so a failure between
            # them can't leave a tool call without its result in the history.
            self._append(assistant, results)
            messages += [assistant, results]

        text = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            text += "\n\n*(I ran out of room there. Ask me to continue.)*"
        reply = Reply(None if text in ("", prompt.NO_REPLY) else text, ctx.attachments, usage)
        if reply.text and reply.text.endswith(prompt.NO_REPLY):
            reply.text = reply.text.removesuffix(prompt.NO_REPLY).strip() or None

        used = (
            (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0)
            + (getattr(response.usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
        )
        if used >= MAX_CONTEXT_TOKENS or _member_turns(messages) >= MAX_MEMBER_TURNS:
            reply.close_reason = "long"
        return reply
