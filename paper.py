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
from ict.journal import append, consecutive_losses, load_open, save_open, stats, write_desk
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


def update_open(cfg: dict, book: str, marks: dict[str, float] | None = None) -> bool:
    fees = Fees.from_config(cfg)
    open_state = load_open(book)
    changed = False
    for inst_id, pos in list(open_state.items()):
        try:
            last = float(marks[inst_id]) if marks and inst_id in marks else fetch_last(inst_id)
        except Exception as exc:
            print(f"[{book}] {inst_id}: mark error {exc}", file=sys.stderr)
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
            book=book,
        )
        del open_state[inst_id]
        changed = True
        print(f"[{book}] CLOSED {inst_id} {hit} @ {last}  R={r:.2f}")
    if changed:
        save_open(open_state, book)
        write_desk()
        persist_journal(f"paper {book} close")
        publish_dashboard()
    return changed


def maybe_fill(fiche: Fiche, cfg: dict, book: str) -> None:
    open_state = load_open(book)
    tag = f"[{book}] {fiche.inst_id}"
    if fiche.inst_id in open_state:
        append(
            {
                "type": "stand_down",
                "inst_id": fiche.inst_id,
                "missing": ["already_open"],
                "last": fiche.last,
                "reasons": fiche.reasons,
            },
            book=book,
        )
        print(f"{tag}: stand down — already in a paper position")
        return

    losses = consecutive_losses(fiche.inst_id, book)
    if losses >= int(cfg["max_consecutive_losses"]):
        append(
            {
                "type": "stand_down",
                "inst_id": fiche.inst_id,
                "missing": ["loss_streak"],
                "last": fiche.last,
                "reasons": fiche.reasons + [f"streak={losses}"],
            },
            book=book,
        )
        print(f"{tag}: stand down — {losses} losses in a row")
        return

    if not fiche.passed:
        append(
            {
                "type": "stand_down",
                "inst_id": fiche.inst_id,
                "missing": fiche.missing,
                "last": fiche.last,
                "bias": fiche.bias,
                "reasons": fiche.reasons,
            },
            book=book,
        )
        print(f"{tag}: STAND DOWN  last={fiche.last}  missing={', '.join(fiche.missing)}")
        for c in fiche.checks:
            print(f"  [{ 'OK' if c.ok else 'NO' }] {c.name}: {c.detail}")
        return

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
        "strategy": book,
        **position_size(
            entry,
            stop,
            equity=float(cfg.get("default_equity_usdt", 10000)),
            risk_pct=float(cfg["risk_pct"]),
            spec=spec,
        ),
    }
    if not pos.get("qty"):
        append(
            {
                "type": "stand_down",
                "inst_id": fiche.inst_id,
                "missing": ["below_min_size"],
                "last": fiche.last,
                "reasons": fiche.reasons,
            },
            book=book,
        )
        print(f"{tag}: stand down — size rounds to zero contracts")
        return
    open_state[fiche.inst_id] = pos
    save_open(open_state, book)
    append({"type": "paper_fill", "inst_id": fiche.inst_id, "last": fiche.last, "position": pos}, book=book)
    print(f"{tag}: PAPER FILL  {fiche.bias} @ {fiche.entry}")
    print(f"  stop {fiche.stop}  target {fiche.target}  R:R {fiche.rr:.2f}")


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
    for book in ("ict", "fabio"):
        s = stats(book)
        wr = f"{s['win_rate']*100:.1f}%" if s["win_rate"] is not None else "n/a"
        print(f"{book}: fills={s['fills']} vetos={s['stand_downs']} wr={wr} open={list(load_open(book))}")


def mark_all(cfg: dict, marks: dict[str, float] | None = None) -> None:
    for book in ("ict", "fabio"):
        update_open(cfg, book, marks)


def rest_marks(cfg: dict) -> dict[str, float]:
    marks: dict[str, float] = {}
    needed = set()
    for book in ("ict", "fabio"):
        needed.update(load_open(book).keys())
    if not needed:
        needed.update(cfg["instruments"])
    for inst in needed:
        try:
            marks[inst] = fetch_last(inst)
        except Exception as exc:
            print(f"{inst}: ticker error {exc}", file=sys.stderr)
    return marks


def any_open() -> bool:
    return bool(load_open("ict") or load_open("fabio"))


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
    mark_all(cfg, marks)
    bar = cfg["entry_bar"]
    scanned = False
    for inst in cfg["instruments"]:
        try:
            hourly = closed_bars(fetch_candles(inst, cfg["htf_bar"], int(cfg["htf_limit"])), cfg["htf_bar"])
            entry = closed_bars(fetch_candles(inst, bar, int(cfg["entry_limit"])), bar)
            last = float(marks[inst]) if marks and inst in marks else fetch_last(inst)
        except Exception as exc:
            print(f"{inst}: error {exc}", file=sys.stderr)
            append({"type": "error", "inst_id": inst, "error": str(exc)}, book="ict")
            continue
        if not entry or not hourly:
            print(f"{inst}: no closed {bar} bar yet", file=sys.stderr)
            continue
        if seen_close is not None:
            if seen_close.get(inst) == entry[-1].ts:
                continue
            seen_close[inst] = entry[-1].ts
        scanned = True
        try:
            maybe_fill(
                analyze_ict(
                    inst,
                    last,
                    hourly,
                    entry,
                    min_rr=float(cfg["min_rr"]),
                    session_min=int(cfg["session_min_score"]),
                ),
                cfg,
                "ict",
            )
        except Exception as exc:
            append({"type": "error", "inst_id": inst, "error": str(exc)}, book="ict")
            print(f"[ict] {inst}: error {exc}", file=sys.stderr)
        try:
            maybe_fill(analyze_fabio(inst, last, entry, min_rr=1.5), cfg, "fabio")
        except Exception as exc:
            append({"type": "error", "inst_id": inst, "error": str(exc)}, book="fabio")
            print(f"[fabio] {inst}: error {exc}", file=sys.stderr)
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
                mark_all(cfg, marks)
            now = time.time()
            if any_open() and feed.stale(8.0) and now - last_watchdog >= 2.0:
                last_watchdog = now
                marks.update(rest_marks(cfg))
                mark_all(cfg, marks)
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
