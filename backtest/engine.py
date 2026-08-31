"""Replay history through the live models.

The point of this engine is that it does not reimplement a strategy. `analyze`
in ict/model.py and fabio/model.py are pure functions of candle lists with an
injectable `now`, so a backtest can feed them historical windows and get the
exact decision the desk would have made. Exits reuse the desk's own rule. If
the models change, the backtest changes with them — there is no second copy to
drift.

Two things a backtest must not do, and how this one avoids them:

Look-ahead. At every decision the models see only bars that had fully closed at
that instant — the entry window ends at the decision bar, and the HTF window is
cut at the same timestamp. `now` is the moment the bar closed, never later.

Optimistic exits. A 15m bar says where price went, not in what order. When a
bar's range covers both stop and target, this counts the stop, matching the
desk's live rule: a print through both is a loss, not a lucky win.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from fabio.model import analyze as analyze_fabio
from ict.model import Fiche, analyze as analyze_ict
from ict.okx_data import Candle, bar_ms
from ict.sizing import position_size


@dataclass
class Trade:
    inst_id: str
    book: str
    bias: str
    entry: float
    stop: float
    target: float
    rr: float
    opened_ts: int
    closed_ts: int | None = None
    result: str | None = None
    r: float | None = None
    bars_held: int = 0
    qty: float = 0.0
    risk_usd: float = 0.0

    @property
    def pnl(self) -> float:
        return (self.r or 0.0) * self.risk_usd


@dataclass
class Result:
    book: str
    inst_id: str
    trades: list[Trade] = field(default_factory=list)
    vetoes: Counter = field(default_factory=Counter)
    decisions: int = 0
    skipped: Counter = field(default_factory=Counter)
    first_ts: int | None = None
    last_ts: int | None = None

    @property
    def closed(self) -> list[Trade]:
        return [t for t in self.trades if t.result]


def bar_exit(bias: str, bar: Candle, stop: float, target: float) -> str | None:
    """Did this bar take the position out, and how?

    The stop is tested first. Within one 15m bar we cannot know whether price
    reached the stop or the target first, so the adverse one wins — the same
    conservative rule ict/exits.py applies to a print through both levels.
    """
    if (bias or "").lower() == "short":
        if bar.high >= stop:
            return "loss"
        if bar.low <= target:
            return "win"
        return None
    if bar.low <= stop:
        return "loss"
    if bar.high >= target:
        return "win"
    return None


def _ict(inst_id: str, last: float, hourly: list[Candle], entry: list[Candle], cfg: dict, now: datetime) -> Fiche:
    return analyze_ict(
        inst_id,
        last,
        hourly,
        entry,
        min_rr=float(cfg["min_rr"]),
        session_min=int(cfg["session_min_score"]),
        now=now,
    )


def _fabio(inst_id: str, last: float, hourly: list[Candle], entry: list[Candle], cfg: dict, now: datetime) -> Fiche:
    return analyze_fabio(inst_id, last, entry, min_rr=float(cfg.get("fabio_min_rr", 1.5)), now=now)


BOOKS: dict[str, Callable[..., Fiche]] = {"ict": _ict, "fabio": _fabio}


def run(
    book: str,
    inst_id: str,
    entry_bars: list[Candle],
    hourly_bars: list[Candle],
    cfg: dict,
    *,
    entry_bar: str = "15m",
    htf_bar: str = "1H",
) -> Result:
    """Replay `entry_bars` in order. Both lists must be sorted oldest first."""
    if book not in BOOKS:
        raise ValueError(f"unknown book {book}")
    analyze = BOOKS[book]
    entry_limit = int(cfg.get("entry_limit", 96))
    htf_limit = int(cfg.get("htf_limit", 240))
    max_losses = int(cfg.get("max_consecutive_losses", 5))
    equity = float(cfg.get("default_equity_usdt", 10000))
    risk_pct = float(cfg.get("risk_pct", 0.5))
    width = bar_ms(entry_bar)
    htf_width = bar_ms(htf_bar)

    htf_ts = [c.ts for c in hourly_bars]
    result = Result(book=book, inst_id=inst_id)
    position: Trade | None = None
    streak = 0

    # Enough closed bars must exist on both timeframes before the first decision.
    start = entry_limit
    while start < len(entry_bars):
        closed_at = entry_bars[start].ts + width
        if bisect_right(htf_ts, closed_at - htf_width) >= htf_limit:
            break
        start += 1

    for i in range(start, len(entry_bars)):
        bar = entry_bars[i]
        closed_at = bar.ts + width
        now = datetime.fromtimestamp(closed_at / 1000, tz=timezone.utc)
        if result.first_ts is None:
            result.first_ts = bar.ts
        result.last_ts = bar.ts

        # A position opened at an earlier close can be taken out by this bar.
        if position is not None:
            hit = bar_exit(position.bias, bar, position.stop, position.target)
            position.bars_held += 1
            if hit:
                risk = abs(position.entry - position.stop)
                reward = abs(position.target - position.entry)
                position.result = hit
                position.r = (reward / risk) if hit == "win" and risk else (-1.0 if risk else 0.0)
                position.closed_ts = bar.ts
                streak = streak + 1 if hit == "loss" else 0
                position = None

        if position is not None:
            result.skipped["already_open"] += 1
            continue
        if streak >= max_losses:
            result.skipped["loss_streak"] += 1
            continue

        # Only bars closed at this instant reach the models.
        window = entry_bars[max(0, i - entry_limit + 1) : i + 1]
        cut = bisect_right(htf_ts, closed_at - htf_width)
        htf = hourly_bars[max(0, cut - htf_limit) : cut]
        try:
            fiche = analyze(inst_id, bar.close, htf, window, cfg, now)
        except Exception as exc:  # a model that raises is a finding, not a crash
            result.skipped[f"error:{type(exc).__name__}"] += 1
            continue

        result.decisions += 1
        if not fiche.passed:
            for name in fiche.missing:
                result.vetoes[name] += 1
            continue
        if not (fiche.entry and fiche.stop and fiche.target):
            result.skipped["incomplete_levels"] += 1
            continue

        size = position_size(float(fiche.entry), float(fiche.stop), equity=equity, risk_pct=risk_pct)
        position = Trade(
            inst_id=inst_id,
            book=book,
            bias=fiche.bias,
            entry=float(fiche.entry),
            stop=float(fiche.stop),
            target=float(fiche.target),
            rr=float(fiche.rr or 0.0),
            opened_ts=bar.ts,
            qty=size["qty"],
            risk_usd=size["risk_usd"],
        )
        result.trades.append(position)

    return result
