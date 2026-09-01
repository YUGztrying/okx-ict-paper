"""What the replay found.

Everything is expressed in R first. R is the unit the desk actually risks —
dollars follow from position_size and the equity you chose, and only R survives
a change of account size.

The veto table is the part worth reading when a book never trades: it names the
condition that is blocking, which forward-testing takes weeks to reveal.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backtest.engine import Result


def metrics(result: Result) -> dict:
    closed = result.closed
    wins = [t for t in closed if t.result == "win"]
    losses = [t for t in closed if t.result == "loss"]
    r_values = [t.r or 0.0 for t in closed]
    gross_win = sum(r for r in r_values if r > 0)
    gross_loss = -sum(r for r in r_values if r < 0)

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in r_values:
        equity += r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    streak = best_win = best_loss = 0
    for t in closed:
        if t.result == "win":
            streak = streak + 1 if streak > 0 else 1
            best_win = max(best_win, streak)
        else:
            streak = streak - 1 if streak < 0 else -1
            best_loss = max(best_loss, -streak)

    return {
        "decisions": result.decisions,
        "trades": len(result.trades),
        "closed": len(closed),
        "open_at_end": len(result.trades) - len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "total_r": sum(r_values),
        "expectancy_r": (sum(r_values) / len(closed)) if closed else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss else (float("inf") if gross_win else None),
        "max_drawdown_r": max_dd,
        "avg_win_r": (sum(t.r or 0 for t in wins) / len(wins)) if wins else None,
        "avg_loss_r": (sum(t.r or 0 for t in losses) / len(losses)) if losses else None,
        "max_win_streak": best_win,
        "max_loss_streak": best_loss,
        "pnl_usd": sum(t.pnl for t in closed),
        "avg_bars_held": (sum(t.bars_held for t in closed) / len(closed)) if closed else None,
    }


def _day(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _num(value, fmt: str = "{:.2f}", dash: str = "—") -> str:
    if value is None:
        return dash
    if value == float("inf"):
        return "∞"
    return fmt.format(value)


def render(result: Result) -> str:
    m = metrics(result)
    lines = [
        f"{result.book} · {result.inst_id} · {_day(result.first_ts)} -> {_day(result.last_ts)}",
        f"  decisions      {m['decisions']}",
        f"  trades         {m['trades']}  (closed {m['closed']}, still open {m['open_at_end']})",
    ]
    if m["closed"]:
        lines += [
            f"  win rate       {_num(m['win_rate'] and m['win_rate'] * 100, '{:.1f}%')}"
            f"  ({m['wins']}W / {m['losses']}L)",
            f"  total          {_num(m['total_r'], '{:+.2f}')} R    ({_num(m['pnl_usd'], '${:+,.2f}')})",
            f"  expectancy     {_num(m['expectancy_r'], '{:+.3f}')} R per trade",
            f"  profit factor  {_num(m['profit_factor'])}",
            f"  max drawdown   {_num(m['max_drawdown_r'], '{:.2f}')} R",
            f"  avg win/loss   {_num(m['avg_win_r'], '{:+.2f}')} R / {_num(m['avg_loss_r'], '{:+.2f}')} R",
            f"  worst streak   {m['max_loss_streak']} losses  (best {m['max_win_streak']} wins)",
            f"  avg hold       {_num(m['avg_bars_held'], '{:.1f}')} bars",
        ]
    else:
        lines.append("  no closed trade")

    if result.vetoes:
        lines.append("  blocked by:")
        for name, count in result.vetoes.most_common():
            share = 100 * count / result.decisions if result.decisions else 0
            flag = "   <- never passed" if count == result.decisions and result.decisions else ""
            lines.append(f"    {name:<12} {count:>5}/{result.decisions}  ({share:.0f}%){flag}")
    if result.skipped:
        lines.append(f"  skipped: {dict(result.skipped)}")
    return "\n".join(lines)
