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

    r_net = [t.r_net if t.r_net is not None else (t.r or 0.0) for t in closed]
    fees = [t.fee for t in closed]
    # Trades the strategy won and the exchange took back. This single count
    # decides whether a strategy is viable at these fee rates.
    eaten = sum(1 for t in closed if (t.r or 0) > 0 and (t.r_net or 0) <= 0)
    stops = sorted(t.stop_pct for t in closed if t.stop_pct)
    notionals = [t.qty * t.entry for t in result.trades if t.qty]

    def pct(seq, q):
        if not seq:
            return None
        return seq[min(len(seq) - 1, int(q * len(seq)))]

    return {
        "total_r_net": sum(r_net),
        "expectancy_r_net": (sum(r_net) / len(closed)) if closed else None,
        "fees_usd": sum(fees),
        "fee_drag_r": (sum(fees) / closed[0].risk_usd / len(closed)) if closed and closed[0].risk_usd else None,
        "eaten_by_fees": eaten,
        "stop_pct_median": pct(stops, 0.5),
        "stop_pct_p10": pct(stops, 0.1),
        "stop_pct_p90": pct(stops, 0.9),
        "max_notional": max(notionals) if notionals else None,
        "max_leverage": (max(notionals) / 10000.0) if notionals else None,
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
            f"  total brut     {_num(m['total_r'], '{:+.2f}')} R    ({_num(m['pnl_usd'], '${:+,.2f}')})",
            f"  total NET      {_num(m['total_r_net'], '{:+.2f}')} R    "
            f"(frais {_num(m['fees_usd'], '${:,.2f}')})",
            f"  expectancy     {_num(m['expectancy_r'], '{:+.3f}')} brut  ->  "
            f"{_num(m['expectancy_r_net'], '{:+.3f}')} NET  par trade",
            f"  friction       {_num(m['fee_drag_r'], '{:.3f}')} R par trade"
            f"   ·  {m['eaten_by_fees']} trade(s) gagnant(s) annule(s) par les frais",
            f"  profit factor  {_num(m['profit_factor'])}",
            f"  max drawdown   {_num(m['max_drawdown_r'], '{:.2f}')} R",
            f"  avg win/loss   {_num(m['avg_win_r'], '{:+.2f}')} R / {_num(m['avg_loss_r'], '{:+.2f}')} R",
            f"  worst streak   {m['max_loss_streak']} losses  (best {m['max_win_streak']} wins)",
            f"  avg hold       {_num(m['avg_bars_held'], '{:.1f}')} bars",
            f"  tension stop   p10 {_num(m['stop_pct_p10'] and m['stop_pct_p10']*100, '{:.2f}%')}"
            f"  median {_num(m['stop_pct_median'] and m['stop_pct_median']*100, '{:.2f}%')}"
            f"  p90 {_num(m['stop_pct_p90'] and m['stop_pct_p90']*100, '{:.2f}%')}",
            f"  notionnel max  {_num(m['max_notional'], '${:,.0f}')}"
            f"  ({_num(m['max_leverage'], '{:.1f}x')} le capital)",
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
