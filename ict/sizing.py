"""Paper position size from stop distance. Risk is a percent of start equity."""

from __future__ import annotations

from typing import Any

DEFAULT_EQUITY = 10000.0
DEFAULT_RISK_PCT = 0.5  # 0.5% → $50 on $10,000


def risk_usd(equity: float = DEFAULT_EQUITY, risk_pct: float = DEFAULT_RISK_PCT) -> float:
    return float(equity) * (float(risk_pct) / 100.0)


def position_size(
    entry: float,
    stop: float,
    *,
    equity: float = DEFAULT_EQUITY,
    risk_pct: float = DEFAULT_RISK_PCT,
) -> dict[str, float]:
    risk = risk_usd(equity, risk_pct)
    dist = abs(float(entry) - float(stop))
    qty = (risk / dist) if dist else 0.0
    return {
        "risk_usd": risk,
        "qty": qty,
        "notional": qty * float(entry),
        "stop_dist": dist,
    }


def unrealized(bias: str, entry: float, last: float, qty: float) -> float:
    if (bias or "").lower() == "short":
        return float(qty) * (float(entry) - float(last))
    return float(qty) * (float(last) - float(entry))


def attach_size(pos: dict[str, Any], equity: float = DEFAULT_EQUITY) -> dict[str, Any]:
    """Fill qty / notional / risk_usd when a stored position is missing them."""
    if pos.get("qty") and pos.get("notional") is not None and pos.get("risk_usd") is not None:
        return pos
    entry, stop = pos.get("entry"), pos.get("stop")
    if entry is None or stop is None:
        return pos
    pos.update(
        position_size(
            float(entry),
            float(stop),
            equity=equity,
            risk_pct=float(pos.get("risk_pct") or DEFAULT_RISK_PCT),
        )
    )
    return pos
