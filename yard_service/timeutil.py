"""Time and number helpers shared by the yard service."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def parse_dt(value: datetime | str | None) -> datetime | None:
    """Parse the project's UTC ISO-8601 strings (trailing ``Z``)."""
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        raise ValueError("时间必须是非空 ISO 8601 字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def as_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def money_fen_from_milli(milli_fen: int) -> Decimal:
    return Decimal(milli_fen) / Decimal(1000)


def money_json(milli_fen: int) -> int | float:
    """Return a JSON-friendly yuan? No: unit is fen.

    Internal accounting uses milli-fen so Wh * fen/kWh stays exact.  When the
    result is a whole fen, emit an int; otherwise emit a float for convenience.
    ``amount_fen_text`` is always available for exact display.
    """
    value = money_fen_from_milli(milli_fen)
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def decimal_to_milli_fen(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    if isinstance(value, int):
        decimal = Decimal(value)
    elif isinstance(value, float):
        decimal = Decimal(str(value))
    elif isinstance(value, str):
        decimal = Decimal(value)
    elif isinstance(value, Decimal):
        decimal = value
    else:
        raise ValueError("金额必须是数字或字符串")
    return int((decimal * Decimal(1000)).to_integral_value(rounding="ROUND_HALF_UP"))
