"""Human-readable strings for weather values, shared by images and alt text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import clock
from .density_alt import c_to_f


@dataclass(frozen=True, slots=True)
class Units:
    altimeter: str = "inHg"
    temperature: str = "C"

    @classmethod
    def from_config(cls, cfg: dict) -> "Units":
        u = cfg.get("units") or {}
        return cls(altimeter=u.get("altimeter", "inHg"), temperature=u.get("temperature", "C"))


def wind(direction: int | None, speed: int | None, gust: int | None = None, *, variable=False) -> str:
    if speed is None:
        return "—"
    if speed == 0 and not gust:
        return "Calm"
    head = "VRB" if variable or direction is None else f"{direction:03d}°"
    text = f"{head} {speed} kt"
    if gust:
        text += f" G{gust}"
    return text


def visibility(sm: float | None, raw: str = "") -> str:
    if sm is None:
        return "—"
    plus = "+" if raw.endswith("+") or sm >= 10 else ""
    if sm >= 10:
        return f"10{plus} SM"
    if sm == int(sm):
        return f"{int(sm)}{plus} SM"
    whole, frac = divmod(sm, 1)
    fracs = {0.25: "1/4", 0.5: "1/2", 0.75: "3/4", 0.125: "1/8", 0.375: "3/8", 0.625: "5/8", 0.875: "7/8"}
    label = fracs.get(round(frac, 3))
    if label:
        return f"{int(whole)} {label} SM" if whole else f"{label} SM"
    return f"{sm:.1f} SM"


def ceiling(feet: int | None) -> str:
    return "None" if feet is None else f"{feet:,} ft"


def clouds(layers: list[dict]) -> str:
    if not layers:
        return "Clear"
    parts = []
    for layer in layers:
        cover = layer.get("cover", "")
        base = layer.get("base")
        if cover in ("CLR", "SKC", "NCD", "NSC", "CAVOK"):
            parts.append(cover)
        elif base is not None:
            kind = layer.get("type") or ""
            parts.append(f"{cover}{base // 100:03d}{kind}")
        else:
            parts.append(cover)
    return " ".join(parts)


def temperature(c: float | None, units: Units) -> str:
    if c is None:
        return "—"
    if units.temperature == "F":
        return f"{round(c_to_f(c))}°F"
    return f"{round(c)}°C"


def altimeter(inhg: float | None, hpa: float | None, units: Units) -> str:
    if units.altimeter == "hPa":
        return f"{round(hpa)} hPa" if hpa is not None else "—"
    return f"{inhg:.2f} inHg" if inhg is not None else "—"


def feet(value: float | None) -> str:
    return "—" if value is None else f"{round(value / 10) * 10:,.0f} ft"


def zulu(when: datetime | None) -> str:
    return "—" if when is None else when.astimezone(timezone.utc).strftime("%d %H%MZ")


def local(when: datetime | None, tz: ZoneInfo, fmt: str = "%a %H:%M") -> str:
    return "—" if when is None else when.astimezone(tz).strftime(fmt)


def ago(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return ""
    now = now or clock.now()
    minutes = int((now - when).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes} min ago"
    return f"{minutes // 60} h {minutes % 60:02d} min ago"


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001 - bad names fall back rather than break a render
        return ZoneInfo("UTC")
