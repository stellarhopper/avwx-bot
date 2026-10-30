"""A server's configuration: validation, defaults, diffs, and the link format.

The schema in schema/config.schema.json is the single source of truth. The
configurator site validates against the same file before it lets an admin
download anything, and the import here validates again — the site is a
convenience, not a gate, since anyone can hand-edit a JSON file.

Defaults also come from the schema's ``default`` keywords, so a stored config
only needs to hold what the admin changed, and adding a setting with a default
later doesn't require migrating every stored document.
"""

from __future__ import annotations

import base64
import copy
import json
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cache
from typing import Any

import jsonschema

from .config import ROOT

SCHEMA_PATH = ROOT / "schema" / "config.schema.json"
SCHEMA_VERSION = 1

# What an import will accept. A real config is a couple of kilobytes; this is
# only here so a stray attachment can't make the bot parse a huge file.
MAX_IMPORT_BYTES = 64 * 1024

# The key the bot adds for the configurator's benefit — channel and role names
# it can't otherwise know. Never stored.
HINTS_KEY = "_guild"


class ConfigError(Exception):
    """An imported document the bot won't apply, with reasons the admin can act on."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@cache
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@cache
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(schema())


def _resolve(node: dict) -> dict:
    ref = node.get("$ref")
    if not ref:
        return node
    target = schema()
    for part in ref.removeprefix("#/").split("/"):
        target = target[part]
    return {**_resolve(target), **{k: v for k, v in node.items() if k != "$ref"}}


def _defaults_for(node: dict) -> Any:
    node = _resolve(node)
    if "default" in node:
        return copy.deepcopy(node["default"])
    if node.get("type") == "object" and "properties" in node:
        out = {}
        for key, child in node["properties"].items():
            value = _defaults_for(child)
            if value is not None:
                out[key] = value
        # An optional block whose fields have no defaults (quiet_hours) stays
        # absent: an empty one would fail its own `required` list.
        if not out and node.get("required"):
            return None
        return out
    return None


@cache
def _defaults() -> dict:
    out = _defaults_for(schema())
    out["schema_version"] = SCHEMA_VERSION
    return out


def defaults() -> dict:
    """Every setting at its default. A fresh copy each time; callers may mutate it."""
    return copy.deepcopy(_defaults())


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def effective(stored: dict | None) -> dict:
    """The stored document laid over the defaults: what the bot actually uses."""
    return _merge(_defaults(), stored or {})


def strip_hints(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != HINTS_KEY}


def _path(error: jsonschema.ValidationError) -> str:
    parts = [str(p) for p in error.absolute_path]
    return ".".join(parts) if parts else "(top level)"


def validate(doc: Any) -> list[str]:
    """Schema problems in `doc`, worded for an admin. Empty means valid."""
    if not isinstance(doc, dict):
        return ["The file should hold a JSON object — `{ … }` — at the top level."]
    problems = []
    for error in sorted(_validator().iter_errors(doc), key=lambda e: list(e.absolute_path)):
        problems.append(f"`{_path(error)}`: {error.message}")
    return problems


def parse_import(raw: bytes) -> dict:
    """Bytes from an attachment to a validated, hint-free document. Raises ConfigError."""
    if len(raw) > MAX_IMPORT_BYTES:
        raise ConfigError(
            [f"That file is {len(raw) // 1024} KB. A config is a few KB; "
             f"anything over {MAX_IMPORT_BYTES // 1024} KB isn't one."]
        )
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise ConfigError(["That file isn't text. Attach the config.json the configurator saved."]) from None
    except json.JSONDecodeError as exc:
        raise ConfigError([f"That isn't valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg})."]) from None
    if isinstance(doc, dict):
        doc = normalise(strip_hints(doc))
    problems = validate(doc)
    if problems:
        raise ConfigError(problems)
    return doc


def normalise(doc: dict) -> dict:
    """Uppercase station IDs, so `kbjc` typed by hand is the same as `KBJC`.

    Runs before validation and so tolerates any shape; whatever it can't make
    sense of is left for the schema to reject.
    """
    doc = copy.deepcopy(doc)

    def upper(block: Any, key: str) -> None:
        if not isinstance(block, dict):
            return
        value = block.get(key)
        if isinstance(value, list):
            block[key] = [v.strip().upper() if isinstance(v, str) else v for v in value]
        elif isinstance(value, str):
            block[key] = value.strip().upper()

    upper(doc, "home_airports")
    brief = doc.get("brief")
    upper(brief, "airports")
    upper(brief, "winds_aloft_station")
    alerts = doc.get("alerts")
    upper(alerts.get("area") if isinstance(alerts, dict) else None, "airports")
    return doc


# ------------------------------------------------------------------- diffing


def _flatten(doc: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(doc, dict) and doc:
        out: dict[str, Any] = {}
        for key, value in doc.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else key))
        return out
    return {prefix: doc}


def diff(old: dict | None, new: dict) -> list[tuple[str, Any, Any]]:
    """(path, before, after) for every effective setting that changes."""
    before = _flatten(strip_hints(effective(old)))
    after = _flatten(strip_hints(effective(new)))
    changes = []
    for key in sorted(before.keys() | after.keys()):
        if before.get(key) != after.get(key):
            changes.append((key, before.get(key), after.get(key)))
    return changes


# --------------------------------------------------------------- link format
#
# The configurator link carries the config in the URL *fragment* (after #).
# Browsers never send the fragment to the server, so GitHub Pages serves a
# static page and never sees a server's channel names. The encoding is raw
# DEFLATE then base64url without padding, which the browser undoes with
# DecompressionStream("deflate-raw") — no library needed on either side.


def encode_fragment(doc: dict) -> str:
    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = packer.compress(raw) + packer.flush()
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


def decode_fragment(text: str) -> dict:
    padded = text + "=" * (-len(text) % 4)
    raw = zlib.decompress(base64.urlsafe_b64decode(padded), -15)
    return json.loads(raw)


# ---------------------------------------------------------- semantic checks


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    """What the import needs to know about a channel, without discord objects."""

    name: str
    kind: str  # "text" | "forum" | "other"
    can_post: bool
    can_attach: bool
    can_post_in_threads: bool
    can_create_threads: bool = True
    can_read_history: bool = True


def config_stations(doc: dict) -> set[str]:
    eff = effective(doc)
    stations = set(eff.get("home_airports", []))
    stations.update(eff["brief"].get("airports", []))
    stations.update(eff["alerts"].get("area", {}).get("airports", []))
    return stations


def semantic_problems(
    doc: dict,
    *,
    channel: Callable[[int], ChannelInfo | None],
    role_exists: Callable[[int], bool],
    unknown_stations: Iterable[str],
    known_regions: Iterable[str],
    known_passes: Callable[[str], set[str]],
) -> tuple[list[str], list[str]]:
    """(blocking problems, warnings) beyond what the schema can express.

    Lookups are injected so this is testable without a Discord connection.
    """
    problems: list[str] = []
    warnings: list[str] = []
    eff = effective(doc)

    for section, label in (("brief", "Morning brief"), ("alerts", "Alerts")):
        block = eff[section]
        cid = block.get("channel_id")
        if block.get("enabled") and not cid:
            problems.append(f"{label} is on but has no channel.")
            continue
        if not cid:
            continue
        info = channel(int(cid))
        if info is None:
            problems.append(f"{label}: channel `{cid}` isn't in this server, or I can't see it.")
            continue
        if info.kind == "other":
            problems.append(f"{label}: #{info.name} is neither a text channel nor a forum.")
            continue
        if not info.can_post:
            what = "create posts in" if info.kind == "forum" else "send messages in"
            problems.append(f"{label}: I don't have permission to {what} #{info.name}.")
        if not info.can_attach:
            problems.append(f"{label}: I can't attach files in #{info.name}, and the brief is an image.")
        if info.kind == "forum" and not info.can_post_in_threads:
            warnings.append(
                f"{label}: I can create posts in #{info.name} but not reply in them, "
                "so alert updates can't be added to the thread."
            )
        if block.get("forum_tag_id") and info.kind != "forum":
            warnings.append(f"{label}: a forum tag is set but #{info.name} isn't a forum; it will be ignored.")

    role = eff["alerts"].get("mention_role_id")
    if role and not role_exists(int(role)):
        problems.append(f"Alerts: the mention role `{role}` doesn't exist in this server.")
    for rid in eff.get("admin_role_ids", []):
        if not role_exists(int(rid)):
            warnings.append(f"Admin role `{rid}` doesn't exist in this server; it will have no effect.")

    unknown = sorted(set(unknown_stations))
    if unknown:
        problems.append(f"Unknown station(s): {', '.join(unknown)}.")

    mountain = eff["mountain"]
    regions = set(known_regions)
    if mountain.get("enabled") or eff["brief"]["include"].get("mountain"):
        region = mountain.get("region")
        if region not in regions:
            problems.append(
                f"Mountain region `{region}` isn't one I know. Known: {', '.join(sorted(regions)) or 'none'}."
            )
        else:
            bad = sorted(set(mountain.get("passes", [])) - known_passes(region))
            if bad:
                problems.append(f"Unknown pass(es) for {region}: {', '.join(bad)}.")

    chat = eff["chat"]
    if chat.get("enabled") and not chat.get("channel_ids"):
        problems.append("Chat is on but no channels are chosen.")
    for cid in chat.get("channel_ids", []):
        info = channel(int(cid))
        if info is None:
            problems.append(f"Chat: channel `{cid}` isn't in this server, or I can't see it.")
        elif info.kind == "other":
            problems.append(f"Chat: #{info.name} is neither a text channel nor a forum.")
        elif not info.can_post_in_threads or not info.can_read_history:
            problems.append(
                f"Chat: conversations happen in threads, so I need Send Messages in Threads "
                f"and Read Message History in #{info.name}."
            )
        elif info.kind == "text" and not info.can_create_threads:
            problems.append(f"Chat: I need Create Public Threads in #{info.name} to start a conversation there.")

    if eff["brief"].get("enabled") and not (eff["brief"].get("airports") or eff.get("home_airports")):
        problems.append("Morning brief is on but no airports are set (brief.airports or home_airports).")
    if eff["alerts"].get("enabled") and not (
        eff["alerts"]["area"].get("airports") or eff.get("home_airports")
    ):
        problems.append("Alerts are on but no area airports are set (alerts.area.airports or home_airports).")

    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(eff["timezone"])
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, …
        problems.append(f"`{eff['timezone']}` isn't a time zone I recognise. Use an IANA name like America/Denver.")

    return problems, warnings
