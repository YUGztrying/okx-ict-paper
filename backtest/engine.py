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
from random import Random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from fabio.model import analyze as analyze_fabio
from ict.model import Check, Fiche, analyze as analyze_ict
from ict.fees import Fees, net_rr, round_trip
from ict.okx_data import Candle, bar_ms
from ict.sizing import leverage, position_size


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
    fee: float = 0.0
    r_net: float | None = None
    stop_pct: float = 0.0   # stop distance as a fraction of entry

    @property
    def pnl(self) -> float:
        return (self.r or 0.0) * self.risk_usd

    @property
    def pnl_net(self) -> float:
        return self.pnl - self.fee


@dataclass
class Result:
    book: str
    inst_id: str
    trades: list[Trade] = field(default_factory=list)
    vetoes: Counter = field(default_factory=Counter)
    decisions: int = 0
    skipped: Counter = field(default_factory=Counter)
    crowded_out: Counter = field(default_factory=Counter)
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


def _random(inst_id: str, last: float, hourly: list[Candle], entry: list[Candle],
            cfg: dict, now: datetime) -> Fiche:
    """The null hypothesis: a coin flip with the desk's geometry.

    Every other book here answers "how much does this strategy make". This one
    answers the question underneath it — does the entry logic do anything at
    all. It takes the same stop distance, the same reward-to-risk, the same
    sizing and the same guards, and picks the direction at random.

    The arithmetic says a random entry with a target R times the stop should
    win about 1/(1+R) of the time, so a strategy landing on that number is
    indistinguishable from this. But that argument assumes a driftless walk and
    ignores the session filter, the stop-first intrabar rule and the fee model.
    Running the control measures what the argument only asserts.

    Seeded from the bar, so a re-run gives the same answer and two books can be
    compared rather than re-rolled.
    """
    bar = entry[-1]
    rng = Random(f"{inst_id}:{bar.ts}")
    px = float(bar.close)
    stop_pct = float(cfg.get("random_stop_pct", 0.005))
    rr = float(cfg.get("random_rr", 2.0))
    long = rng.random() < 0.5
    risk = px * stop_pct
    fiche = Fiche(inst_id=inst_id, last=px, bias="long" if long else "short")
    fiche.checks.append(Check("coin_flip", True, f"{'long' if long else 'short'} · stop {stop_pct:.2%}"))
    fiche.reasons.append(f"coin flip · stop {stop_pct:.2%} · R:R {rr:.2f}")
    fiche.entry = px
    fiche.stop = px - risk if long else px + risk
    fiche.target = px + rr * risk if long else px - rr * risk
    fiche.rr = rr
    return fiche


BOOKS: dict[str, Callable[..., Fiche]] = {"ict": _ict, "fabio": _fabio, "random": _random}
# The desk trades one book: both models read the bar, one of them gets the slot.
DESK = ("ict", "fabio")


def run(
    book: str,
    inst_id: str,
    entry_bars: list[Candle],
    hourly_bars: list[Candle],
    cfg: dict,
    *,
    entry_bar: str = "15m",
    htf_bar: str = "1H",
    fees: Fees | None = None,
) -> Result:
    """Replay `entry_bars` in order. Both lists must be sorted oldest first."""
    if book == "desk":
        models = [(name, BOOKS[name]) for name in DESK]
    elif book in BOOKS:
        models = [(book, BOOKS[book])]
    else:
        raise ValueError(f"unknown book {book}")
    entry_limit = int(cfg.get("entry_limit", 96))
    htf_limit = int(cfg.get("htf_limit", 240))
    max_losses = int(cfg.get("max_consecutive_losses", 5))
    cooldown_ms = float(cfg.get("loss_cooldown_hours", 24)) * 3_600_000
    max_lev = float(cfg.get("max_leverage", 0) or 0)
    on_net = bool(cfg.get("min_rr_on_net"))
    floors = {"ict": float(cfg.get("min_rr", 2.0)), "fabio": float(cfg.get("fabio_min_rr", 1.5))}
    equity = float(cfg.get("default_equity_usdt", 10000))
    risk_pct = float(cfg.get("risk_pct", 0.5))
    width = bar_ms(entry_bar)
    htf_width = bar_ms(htf_bar)
    fees = fees or Fees.from_config(cfg)

    htf_ts = [c.ts for c in hourly_bars]
    result = Result(book=book, inst_id=inst_id)
    position: Trade | None = None
    streak = 0
    halted_at: int | None = None   # when the loss breaker last tripped

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
                # Exit at the level that was hit — the same price the R is
                # measured against, so fee and result describe one trade.
                exit_px = position.target if hit == "win" else position.stop
                position.fee = round_trip(position.entry, exit_px, position.qty, fees.taker)
                if position.risk_usd:
                    position.r_net = (position.pnl - position.fee) / position.risk_usd
                streak = streak + 1 if hit == "loss" else 0
                halted_at = bar.ts if streak >= max_losses else None
                position = None

        if position is not None:
            result.skipped["already_open"] += 1
            continue
        # A cooldown, not a ban: the old check only cleared on a win, and a
        # halted book can never win. It blocked Fabio on BTC for 33,721 bars.
        if streak >= max_losses and halted_at is not None and bar.ts - halted_at < cooldown_ms:
            result.skipped["loss_streak"] += 1
            continue

        # Only bars closed at this instant reach the models.
        window = entry_bars[max(0, i - entry_limit + 1) : i + 1]
        cut = bisect_right(htf_ts, closed_at - htf_width)
        htf = hourly_bars[max(0, cut - htf_limit) : cut]

        # Every model reads the bar before any of them takes the slot. Running
        # them in sequence and filling the first passer would hand the book to
        # whichever one happens to be listed first.
        ready: list[tuple[str, Fiche]] = []
        for name, analyze in models:
            try:
                fiche = analyze(inst_id, bar.close, htf, window, cfg, now)
            except Exception as exc:  # a model that raises is a finding, not a crash
                result.skipped[f"error:{name}:{type(exc).__name__}"] += 1
                continue
            result.decisions += 1
            if not fiche.passed:
                for missing in fiche.missing:
                    result.vetoes[f"{name}:{missing}" if len(models) > 1 else missing] += 1
                continue
            if not (fiche.entry and fiche.stop and fiche.target):
                result.skipped["incomplete_levels"] += 1
                continue
            lev = leverage(float(fiche.entry), float(fiche.stop), risk_pct)
            if max_lev and lev > max_lev:
                result.skipped["stop_too_tight"] += 1
                continue
            if on_net and net_rr(float(fiche.entry), float(fiche.stop),
                                 float(fiche.target), fees.taker) < floors.get(name, 0.0):
                result.skipped["rr_net_below_min"] += 1
                continue
            ready.append((name, fiche))

        if not ready:
            continue
        # Best reward-to-risk AFTER fees wins, the same rule paper.arbitrate
        # applies live: a wide gross R:R on a stop so tight the fees eat a third
        # of it is worth less than a narrower one with room to breathe.
        ready.sort(
            key=lambda item: net_rr(
                float(item[1].entry), float(item[1].stop), float(item[1].target), fees.taker
            ),
            reverse=True,
        )
        name, fiche = ready[0]
        for other, _ in ready[1:]:
            result.crowded_out[other] += 1

        size = position_size(float(fiche.entry), float(fiche.stop), equity=equity, risk_pct=risk_pct)
        position = Trade(
            inst_id=inst_id,
            book=name,
            bias=fiche.bias,
            entry=float(fiche.entry),
            stop=float(fiche.stop),
            target=float(fiche.target),
            rr=float(fiche.rr or 0.0),
            opened_ts=bar.ts,
            qty=size["qty"],
            risk_usd=size.get("risk_usd_actual") or size["risk_usd"],
            stop_pct=abs(float(fiche.entry) - float(fiche.stop)) / float(fiche.entry),
        )
        result.trades.append(position)

    return result
