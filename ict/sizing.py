"""Paper position size from stop distance. Risk is a percent of start equity."""

from __future__ import annotations

from typing import Any

DEFAULT_EQUITY = 10000.0
DEFAULT_RISK_PCT = 0.5  # 0.5% → $50 on $10,000


def risk_usd(equity: float = DEFAULT_EQUITY, risk_pct: float = DEFAULT_RISK_PCT) -> float:
    return float(equity) * (float(risk_pct) / 100.0)


def leverage(entry: float, stop: float, risk_pct: float = DEFAULT_RISK_PCT) -> float:
    """Notional as a multiple of equity.

    Equity cancels out of this: the stop decides the leverage, not the account
    size. `leverage = risk_pct / stop_pct`, so at 0.5% risk a 1% stop is 0.5x
    and a 0.04% stop is 12.5x. Nothing in the strategies bounds the stop, so
    nothing bounds this either unless the desk says so.
    """
    entry, dist = float(entry), abs(float(entry) - float(stop))
    if not entry or not dist:
        return 0.0
    return (float(risk_pct) / 100.0) * entry / dist


def position_size(
    entry: float,
    stop: float,
    *,
    equity: float = DEFAULT_EQUITY,
    risk_pct: float = DEFAULT_RISK_PCT,
    spec: Any = None,
) -> dict[str, float]:
    """Size from stop distance, then snap to what the exchange will accept.

    Without a spec this returns the fractional size, which is fine for a
    what-if. With one, the size is rounded DOWN to whole contracts, and
    `risk_usd_actual` reports what that rounding really puts at risk —
    `risk_usd` stays the amount that was intended. The two differ, and a record
    that only keeps the intended one quietly mis-states every R it computes.
    """
    risk = risk_usd(equity, risk_pct)
    dist = abs(float(entry) - float(stop))
    qty = (risk / dist) if dist else 0.0
    out = {
        "risk_usd": risk,
        "qty": qty,
        "notional": qty * float(entry),
        "stop_dist": dist,
        "risk_usd_actual": risk,
        "leverage": (qty * float(entry) / float(equity)) if equity else 0.0,
    }
    if spec is None:
        return out

    from ict.instruments import base_qty, contracts_for

    lots = contracts_for(qty, spec)
    filled = base_qty(lots, spec)
    out.update(
        {
            "contracts": lots,
            "qty": filled,
            "notional": filled * float(entry),
            "risk_usd_actual": filled * dist,
            "leverage": (filled * float(entry) / float(equity)) if equity else 0.0,
            "ct_val": spec.ct_val,
        }
    )
    return out


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
