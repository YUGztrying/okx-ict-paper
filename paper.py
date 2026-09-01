#!/usr/bin/env python
"""Paper desk. ICT + Fabio AAA. Never places OKX orders."""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from fabio.model import analyze as analyze_fabio
from ict.journal import (
    absorb_legacy,
    append,
    consecutive_losses,
    load_open,
    save_open,
    stats,
    write_desk,
)
from ict.model import Fiche, analyze as analyze_ict
from ict.cloud import dispatch_next, persist_enabled, persist_journal, publish_dashboard
from ict.exits import exit_result, realized_r
from ict.fees import Fees, fee_in_r, net_rr, round_trip
from ict.okx_data import closed_bars, closed_candle, fetch_candles, fetch_last, near_bar_boundary
from ict.okx_ws import PublicFeed, drain_events, is_decision_bar
from ict.instruments import round_price
from ict.sizing import position_size

ROOT = Path(__file__).resolve().parent


def load_config() -> dict:
    with (ROOT / "config.toml").open("rb") as fh:
        return tomllib.load(fh)


def underlying(inst_id: str) -> str:
    """BTC-USDT-SWAP and BTC-USDT are the same coin. One book means one bet on
    it: a perp long and a spot short are not two independent trades, they are
    one confused one."""
    return inst_id.split("-", 1)[0].upper()


def update_open(cfg: dict, marks: dict[str, float] | None = None) -> bool:
    fees = Fees.from_config(cfg)
    open_state = load_open()
    changed = False
    for inst_id, pos in list(open_state.items()):
        strategy = pos.get("strategy", "ict")
        try:
            last = float(marks[inst_id]) if marks and inst_id in marks else fetch_last(inst_id)
        except Exception as exc:
            print(f"[{strategy}] {inst_id}: mark error {exc}", file=sys.stderr)
            continue
        hit = exit_result(pos["bias"], last, float(pos["stop"]), float(pos["target"]))
        if not hit:
            continue
        r = realized_r(float(pos["entry"]), float(pos["stop"]), float(pos["target"]), hit)
        # Gross R is the strategy's result; net R is the one that reaches the
        # account. Both are recorded — the gap is the cost of trading.
        risk = float(pos.get("risk_usd_actual") or pos.get("risk_usd") or 0.0)
        qty = float(pos.get("qty") or 0.0)
        rate = float(pos.get("fee_rate") or fees.taker)
        fee = round_trip(float(pos["entry"]), last, qty, rate) if qty else 0.0
        pnl_gross = r * risk
        append(
            {
                "type": "paper_close",
                "inst_id": inst_id,
                "result": hit,
                "last": last,
                "r": r,
                "fee": round(fee, 4),
                "r_net": round((pnl_gross - fee) / risk, 4) if risk else None,
                "pnl_gross": round(pnl_gross, 4),
                "pnl_net": round(pnl_gross - fee, 4),
                "position": pos,
            },
            strategy=strategy,
        )
        del open_state[inst_id]
        changed = True
        print(f"[{strategy}] CLOSED {inst_id} {hit} @ {last}  R={r:.2f}")
    if changed:
        save_open(open_state)
        write_desk()
        persist_journal("paper close")
        publish_dashboard()
    return changed


def blocked_reason(fiche: Fiche, cfg: dict, open_state: dict, strategy: str) -> tuple[str, list[str]] | None:
    """Why this setup cannot be taken, or None. Checked BEFORE arbitration so a
    candidate that could never have filled does not crowd out one that could."""
    if cfg.get("one_position_per_asset", True):
        coin = underlying(fiche.inst_id)
        held = [i for i in open_state if underlying(i) == coin]
        if held:
            return "already_open", [f"open: {', '.join(held)}"]
    elif fiche.inst_id in open_state:
        return "already_open", []

    losses = consecutive_losses(fiche.inst_id, strategy)
    if losses >= int(cfg["max_consecutive_losses"]):
        return "loss_streak", [f"streak={losses}"]
    if not fiche.passed:
        return "vetoed", []
    return None


def build_position(fiche: Fiche, cfg: dict, strategy: str) -> dict | None:
    """The order the desk would send, priced on the exchange's grid. None when
    the risk budget rounds down to zero contracts."""
    fees = Fees.from_config(cfg)
    spec = instrument_spec(fiche.inst_id)
    entry, stop, target = float(fiche.entry), float(fiche.stop), float(fiche.target)
    if spec is not None:
        # A price off the tick is rejected by the exchange, so the desk must not
        # record one. Stop and target round AWAY from entry: never claim a
        # tighter stop or a nearer target than the grid allows.
        long = (fiche.bias or "").lower() == "long"
        entry = round_price(entry, spec, "near")
        stop = round_price(stop, spec, "down" if long else "up")
        target = round_price(target, spec, "down" if long else "up")

    pos = {
        "bias": fiche.bias,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr": fiche.rr,
        "rr_net": round(net_rr(entry, stop, target, fees.taker), 4),
        "fee_rate": fees.taker,
        "fee_r_est": round(fee_in_r(entry, stop, fees.taker), 4),
        "sized_with_spec": spec is not None,
        "risk_pct": cfg["risk_pct"],
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "reasons": fiche.reasons,
        "strategy": strategy,
        **position_size(
            entry,
            stop,
            equity=float(cfg.get("default_equity_usdt", 10000)),
            risk_pct=float(cfg["risk_pct"]),
            spec=spec,
        ),
    }
    return pos if pos.get("qty") else None


def stand_down(fiche: Fiche, strategy: str, missing: list[str], extra: list[str] | None = None) -> None:
    append(
        {
            "type": "stand_down",
            "inst_id": fiche.inst_id,
            "missing": missing,
            "last": fiche.last,
            "bias": fiche.bias,
            "reasons": fiche.reasons + (extra or []),
        },
        strategy=strategy,
    )


def commit(fiche: Fiche, pos: dict, strategy: str) -> None:
    open_state = load_open()
    open_state[fiche.inst_id] = pos
    save_open(open_state)
    append(
        {"type": "paper_fill", "inst_id": fiche.inst_id, "last": fiche.last, "position": pos},
        strategy=strategy,
    )
    print(f"[{strategy}] {fiche.inst_id}: PAPER FILL  {fiche.bias} @ {pos['entry']}")
    print(f"  stop {pos['stop']}  target {pos['target']}  R:R {pos['rr']:.2f} brut / {pos['rr_net']:.2f} net")


def arbitrate(candidates: list[tuple[str, Fiche]], cfg: dict) -> None:
    """One book, one position per asset — so when both strategies fire on the
    same coin, only one of them can be taken.

    The winner is the best reward-to-risk AFTER fees, because that is the number
    the account actually collects: a 2.4 gross setup on a stop so tight the fees
    eat a third of it is worth less than a 2.1 with room to breathe.

    The loser is written down as `crowded_out`, naming who took the slot and at
    what R:R. Without that line the cost of sharing a book is invisible — the
    rarer strategy just looks like it stopped finding setups.
    """
    open_state = load_open()
    ready: list[tuple[str, Fiche, dict]] = []
    for strategy, fiche in candidates:
        blocked = blocked_reason(fiche, cfg, open_state, strategy)
        if blocked:
            reason, extra = blocked
            if reason == "vetoed":
                stand_down(fiche, strategy, fiche.missing)
                print(f"[{strategy}] {fiche.inst_id}: STAND DOWN  last={fiche.last}  "
                      f"missing={', '.join(fiche.missing)}")
                for c in fiche.checks:
                    print(f"  [{ 'OK' if c.ok else 'NO' }] {c.name}: {c.detail}")
            else:
                stand_down(fiche, strategy, [reason], extra)
                print(f"[{strategy}] {fiche.inst_id}: stand down — {reason}")
            continue
        if not (fiche.entry and fiche.stop and fiche.target):
            stand_down(fiche, strategy, ["incomplete_levels"])
            continue
        pos = build_position(fiche, cfg, strategy)
        if pos is None:
            stand_down(fiche, strategy, ["below_min_size"])
            print(f"[{strategy}] {fiche.inst_id}: stand down — size rounds to zero contracts")
            continue
        ready.append((strategy, fiche, pos))

    if not ready:
        return
    ready.sort(key=lambda item: item[2]["rr_net"], reverse=True)
    strategy, fiche, pos = ready[0]
    for other, loser_fiche, loser_pos in ready[1:]:
        stand_down(
            loser_fiche,
            other,
            ["crowded_out"],
            [f"crowded out by {strategy} on {fiche.inst_id}: "
             f"R:R net {loser_pos['rr_net']:.2f} vs {pos['rr_net']:.2f}"],
        )
        print(f"[{other}] {loser_fiche.inst_id}: crowded out by {strategy} "
              f"({loser_pos['rr_net']:.2f} net vs {pos['rr_net']:.2f})")
    commit(fiche, pos, strategy)


def maybe_fill(fiche: Fiche, cfg: dict, strategy: str) -> None:
    """A single candidate, no competition. Kept because a lone signal is still
    the common case — arbitrate() is what handles the collision."""
    arbitrate([(strategy, fiche)], cfg)


def instrument_spec(inst_id: str):
    """Contract specs, or None. A paper desk places no orders, so it keeps
    running on fractional sizes rather than dying — but the fill records that
    it was sized without them."""
    try:
        from ict.instruments import spec as lookup

        return lookup(inst_id)
    except Exception as exc:
        print(f"{inst_id}: no contract spec ({exc})", file=sys.stderr)
        return None


def print_status() -> None:
    total = stats()
    wr = f"{total['win_rate']*100:.1f}%" if total["win_rate"] is not None else "n/a"
    print(f"desk: fills={total['fills']} vetos={total['stand_downs']} "
          f"(dont {total['crowded_out']} crowded out) wr={wr}")
    for strategy in ("ict", "fabio"):
        s = stats(strategy)
        wr = f"{s['win_rate']*100:.1f}%" if s["win_rate"] is not None else "n/a"
        held = [i for i, pos in load_open().items() if pos.get("strategy") == strategy]
        print(f"  {strategy}: fills={s['fills']} vetos={s['stand_downs']} "
              f"crowded_out={s['crowded_out']} wr={wr} open={held}")


def rest_marks(cfg: dict) -> dict[str, float]:
    marks: dict[str, float] = {}
    needed = set(load_open())
    if not needed:
        needed.update(cfg["instruments"])
    for inst in needed:
        try:
            marks[inst] = fetch_last(inst)
        except Exception as exc:
            print(f"{inst}: ticker error {exc}", file=sys.stderr)
    return marks


def any_open() -> bool:
    return bool(load_open())


def still_pending(pending: dict[str, int], seen_close: dict[str, int]) -> dict[str, int]:
    """Closes still waiting for a scan. A scan records the bar it analyzed, so
    anything left here is a bar REST had not published yet — retry, never drop."""
    return {inst: ts for inst, ts in pending.items() if seen_close.get(inst) != ts}


def hand_off_watch() -> bool:
    """Push journal first. The next runner must not checkout a stale blotter."""
    write_desk()
    if persist_enabled() and not persist_journal("paper watch handoff"):
        print("handoff persist failed — YAML will retry before chaining", file=sys.stderr)
        return False
    return dispatch_next()


def tick(cfg: dict, marks: dict[str, float] | None = None, seen_close: dict[str, int] | None = None) -> bool:
    """Scan every instrument on its newest CLOSED bar.

    One close event means one desk scan, not one per instrument: `tick` already
    walks the whole instrument list, so it records the bar it decided on for
    each of them in `seen_close`. A second trigger for the same bar — the other
    instrument's confirm, or the REST stall guard — then finds nothing to do.

    Returns True when at least one instrument was analyzed.
    """
    print(f"\n=== {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")
    update_open(cfg, marks)
    bar = cfg["entry_bar"]
    scanned = False
    for inst in cfg["instruments"]:
        try:
            hourly = closed_bars(fetch_candles(inst, cfg["htf_bar"], int(cfg["htf_limit"])), cfg["htf_bar"])
            entry = closed_bars(fetch_candles(inst, bar, int(cfg["entry_limit"])), bar)
            last = float(marks[inst]) if marks and inst in marks else fetch_last(inst)
        except Exception as exc:
            print(f"{inst}: error {exc}", file=sys.stderr)
            append({"type": "error", "inst_id": inst, "error": str(exc)}, strategy="desk")
            continue
        if not entry or not hourly:
            print(f"{inst}: no closed {bar} bar yet", file=sys.stderr)
            continue
        if seen_close is not None:
            if seen_close.get(inst) == entry[-1].ts:
                continue
            seen_close[inst] = entry[-1].ts
        scanned = True
        # Both strategies read the same bar before either one is allowed to take
        # the slot — otherwise whichever runs first wins by running first.
        candidates: list[tuple[str, Fiche]] = []
        try:
            candidates.append((
                "ict",
                analyze_ict(
                    inst,
                    last,
                    hourly,
                    entry,
                    min_rr=float(cfg["min_rr"]),
                    session_min=int(cfg["session_min_score"]),
                ),
            ))
        except Exception as exc:
            append({"type": "error", "inst_id": inst, "error": str(exc)}, strategy="ict")
            print(f"[ict] {inst}: error {exc}", file=sys.stderr)
        try:
            candidates.append(("fabio", analyze_fabio(inst, last, entry, min_rr=1.5)))
        except Exception as exc:
            append({"type": "error", "inst_id": inst, "error": str(exc)}, strategy="fabio")
            print(f"[fabio] {inst}: error {exc}", file=sys.stderr)
        try:
            arbitrate(candidates, cfg)
        except Exception as exc:
            append({"type": "error", "inst_id": inst, "error": str(exc)}, strategy="desk")
            print(f"{inst}: arbitration error {exc}", file=sys.stderr)
    if not scanned:
        return False
    write_desk()
    persist_journal("paper scan")
    # Redraw the phone blotter now. Pages would otherwise only rebuild when
    # this 5h20 job ends, showing positions that closed hours ago as live.
    publish_dashboard()
    return True


def watch(cfg: dict, minutes: float, chain_after: float = 0) -> None:
    """Desk loop: WS last for SL/TP, confirmed 15m close for entries."""
    deadline = time.time() + minutes * 60 if minutes > 0 else None
    chain_at = time.time() + chain_after * 60 if chain_after > 0 else None
    marks: dict[str, float] = {}
    chained = False
    last_watchdog = 0.0
    last_bar_rest = 0.0
    last_handoff = 0.0
    bar = cfg["entry_bar"]
    feed = PublicFeed(list(cfg["instruments"]), bar)
    feed.start()
    if not feed.connected.wait(15):
        print("WS not up yet — REST marks until it is", file=sys.stderr)
    # The start tick seeds itself: it records the exact bar it decided on for
    # each instrument. Seeding separately afterwards could swallow a bar that
    # closed while the tick was still fetching and pushing.
    seen_close: dict[str, int] = {}
    tick(cfg, seen_close=seen_close)
    persist_journal("paper watch start")
    print(f"watch WS tickers + business {bar} confirm=1 — no live orders")
    # Bars we know closed but have not scanned yet. A scan reads REST, which can
    # trail the socket by a moment; the decision waits rather than being dropped.
    pending: dict[str, int] = {}
    last_scan_try = 0.0
    try:
        while deadline is None or time.time() < deadline:
            ticks, closes = drain_events(feed.events, timeout=1.0)
            for ev in closes:
                if is_decision_bar(ev.inst_id, ev.ts, seen_close):
                    print(f"{ev.inst_id}: {bar} closed @ {ev.close}")
                    marks[ev.inst_id] = ev.close
                    pending[ev.inst_id] = ev.ts
            if pending and time.time() - last_scan_try >= 2.0:
                last_scan_try = time.time()
                tick(cfg, marks, seen_close)
                pending = still_pending(pending, seen_close)
            if ticks:
                for inst, ev in ticks.items():
                    marks[inst] = ev.last
                update_open(cfg, marks)
            now = time.time()
            if any_open() and feed.stale(8.0) and now - last_watchdog >= 2.0:
                last_watchdog = now
                marks.update(rest_marks(cfg))
                update_open(cfg, marks)
            if near_bar_boundary(bar) and now - last_bar_rest >= 2.0:
                last_bar_rest = now
                for inst in cfg["instruments"]:
                    try:
                        closed = closed_candle(fetch_candles(inst, bar, 3), bar)
                    except Exception as exc:
                        print(f"{inst}: bar-rest error {exc}", file=sys.stderr)
                        continue
                    if closed and is_decision_bar(inst, closed.ts, seen_close):
                        print(f"{inst}: {bar} closed @ {closed.close} (REST)")
                        marks[inst] = closed.close
                        pending[inst] = closed.ts
                if pending and time.time() - last_scan_try >= 2.0:
                    last_scan_try = time.time()
                    tick(cfg, marks, seen_close)
                    pending = still_pending(pending, seen_close)
            if chain_at and not chained and now >= chain_at and now - last_handoff >= 30:
                last_handoff = now
                if hand_off_watch():
                    chained = True
                    break
                print("handoff not ready — retry in 30s", file=sys.stderr)
    finally:
        feed.stop()
        write_desk()
        persist_journal("paper watch end")


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper desk — ICT + Fabio AAA, no live orders")
    parser.add_argument("--loop", type=int, metavar="MIN", help="repeat a full scan every N minutes")
    parser.add_argument("--watch", action="store_true", help="WS last for SL/TP, confirmed 15m close for entries")
    parser.add_argument("--minutes", type=float, default=0, help="with --watch, stop after N minutes (0 = forever)")
    parser.add_argument("--chain-after", type=float, default=0, help="dispatch the next GitHub watch after N minutes")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    cfg = load_config()
    # An older runner may still be writing the second journal. Fold whatever it
    # left behind into the book before reading a single line of it.
    moved = absorb_legacy()
    if moved:
        print(f"absorbed {moved} events from the old per-strategy journal")

    if args.status:
        print_status()
        return 0

    if args.watch:
        watch(cfg, args.minutes, args.chain_after)
        return 0

    tick(cfg)
    if args.loop:
        minutes = args.loop
        print(f"looping every {minutes} min — Ctrl+C to stop. No live orders.")
        while True:
            time.sleep(minutes * 60)
            tick(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
