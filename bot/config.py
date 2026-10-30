"""Process-level configuration.

Only a few things live here: the bot token, an optional dev guild for fast
command sync, the database path, the configurator's URL and the User-Agent sent
to the weather APIs. Everything a server admin might want to change — which
features are on, which airports, which channels — lives in the database per
guild and arrives through `/avwx config import`, so the bot can be added to a
server and run without anyone editing files on disk.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SITE_URL = "https://stellarhopper.github.io/avwx-bot/"
# api.weather.gov refuses requests without a User-Agent and asks for a way to
# reach the operator in it. The repo URL is that contact.
DEFAULT_USER_AGENT = "avwx-bot (+https://github.com/stellarhopper/avwx-bot)"


def load_env_file(path: Path | None = None) -> None:
    """Populate os.environ from a .env file.

    Real environment variables always win, so a systemd unit's Environment=
    lines override the file rather than the other way around.
    """
    path = path or ROOT / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None


def database_path() -> Path:
    """DB_PATH if set; else systemd's state directory; else data/ in the checkout.

    The middle case matters on the Pi. systemd sets STATE_DIRECTORY itself from
    the unit's StateDirectory=, and nothing in .env can blank it. DB_PATH can be
    blanked: a .env copied from .env.example carries an empty `DB_PATH=`, and
    systemd lets the env file override the unit, which would quietly move the
    database into the checkout that every deployment resets.
    """
    raw = os.environ.get("DB_PATH", "").strip()
    if raw:
        return Path(raw)
    state = os.environ.get("STATE_DIRECTORY", "").split(":")[0].strip()
    if state:
        return Path(state) / "avwx.db"
    return ROOT / "data" / "avwx.db"


def club_path() -> Path:
    """CLUB_DIR if set; else beside the database in systemd's state directory;
    else data/club in the checkout. Same reasoning as database_path: on the Pi
    the club's files must not live in the checkout that deployment resets."""
    raw = os.environ.get("CLUB_DIR", "").strip()
    if raw:
        return Path(raw)
    state = os.environ.get("STATE_DIRECTORY", "").split(":")[0].strip()
    if state:
        return Path(state) / "club"
    return ROOT / "data" / "club"


def _id_set(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "")
    out = set()
    for part in raw.replace(",", " ").split():
        try:
            out.add(int(part))
        except ValueError:
            raise SystemExit(f"{name} must be a list of server IDs, got {part!r}") from None
    return frozenset(out)


EFFORTS = ("low", "medium", "high", "xhigh", "max")


class Config:
    """Snapshot of the environment, read once at startup."""

    def __init__(self) -> None:
        self.token = os.environ.get("DISCORD_TOKEN", "").strip()
        self.dev_guild_id = _optional_int("DEV_GUILD_ID")
        self.db_path = database_path()
        self.site_url = os.environ.get("CONFIG_SITE_URL", "").strip() or DEFAULT_SITE_URL
        self.user_agent = (
            os.environ.get("HTTP_USER_AGENT", "").strip() or DEFAULT_USER_AGENT
        )
        # The club chat spends this host's Anthropic credit, so it only runs on
        # servers listed here, whatever a server's own config says.
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or None
        self.chat_guild_ids = _id_set("CHAT_GUILD_IDS")
        self.club_dir = club_path()
        self.chat_effort = os.environ.get("CHAT_EFFORT", "").strip() or "medium"
        if self.chat_effort not in EFFORTS:
            raise SystemExit(f"CHAT_EFFORT must be one of {', '.join(EFFORTS)}, got {self.chat_effort!r}")

    def require_token(self) -> str:
        if not self.token:
            raise SystemExit(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
            )
        return self.token
