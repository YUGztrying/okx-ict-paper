"""What a round trip costs, at your OKX rates.

Fees are charged on the notional of each execution, and a trade has two: the
entry and the exit, at different prices. The desk enters at the close of a
confirmed bar and exits when a level is hit, so both are market orders — taker
on both sides, by construction.

The result worth remembering is what happens when you express it in R:

    fee_R = (fee_in + fee_out) / risk_usd  ~=  2 x rate / (stop distance in %)

The size cancels out. The cost in R depends on the fee rate and on how tight
the stop is as a fraction of price — not on the account, not on the risk
percent. A stop 0.15% away costs ten times more, in R, than one 1.5% away.
"""

from __future__ import annotations

from dataclasses import dataclass


def product_of(inst_id: str | None) -> str:
    """OKX charges spot and perpetuals at different rates — at Lv1 Regular,
    twice as much on spot. The instrument id is what says which."""
    return "swap" if (inst_id or "").upper().endswith("-SWAP") else "spot"


@dataclass(frozen=True)
class Fees:
    """Rates as fractions: 0.0005 is OKX perp taker at Lv1."""

    taker: float = 0.0005
    maker: float = 0.0002

    @classmethod
    def from_config(cls, cfg: dict, inst_id: str | None = None) -> "Fees":
        """Rates for one instrument, or the desk's default when none is named.

        The desk trades perpetuals, so `[fees]` holds the perp rates and that
        is the default. But a position can outlive the config that opened it —
        two spot positions carried across the switch to perps and closed under
        it, and were charged the perp rate: half of what OKX would have taken.
        Naming the instrument makes the right table apply on its own.
        """
        raw = cfg.get("fees") or {}
        if inst_id is not None and product_of(inst_id) == "spot":
            raw = raw.get("spot") or raw
        return cls(
            taker=float(raw.get("taker_pct", 0.05)) / 100.0,
            maker=float(raw.get("maker_pct", 0.02)) / 100.0,
        )


def execution_fee(notional: float, rate: float) -> float:
    return abs(float(notional)) * float(rate)


def round_trip(entry: float, exit_px: float, qty: float, rate: float) -> float:
    """Both executions, each on its own notional."""
    return execution_fee(qty * entry, rate) + execution_fee(qty * exit_px, rate)


def fee_in_r(entry: float, stop: float, rate: float) -> float:
    """Round-trip cost in R, approximating the exit notional by the entry's.

    Exact enough to compare setups before knowing where the trade exits: the
    two notionals differ by the move itself, a few percent at most.
    """
    dist = abs(float(entry) - float(stop))
    if not dist:
        return 0.0
    return 2.0 * float(rate) * float(entry) / dist


def net_rr(entry: float, stop: float, target: float, rate: float) -> float:
    """Reward-to-risk after fees — what the trade actually offers.

    A setup at 2.0 gross with a tight stop can be under 1.5 net, so a min_rr
    gate applied to the gross number lets through trades that do not meet it.
    """
    dist = abs(float(entry) - float(stop))
    if not dist:
        return 0.0
    reward = abs(float(target) - float(entry))
    return (reward - 2.0 * float(rate) * float(entry)) / dist
