#!/usr/bin/env python
"""Paper desk. ICT + Fabio AAA. Never places OKX orders."""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty

from fabio.model import analyze as analyze_fabio
from ict.journal import append, consecutive_losses, load_open, save_open, stats, write_desk
from ict.model import Fiche, analyze as analyze_ict
from ict.cloud import dispatch_next, persist_enabled, persist_journal
from ict.exits import exit_result, realized_r
from ict.okx_data import closed_candle, fetch_candles, fetch_last
from ict.okx_ws import BarClose, PublicFeed, Tick, is_decision_bar
from ict.sizing import position_size

ROOT = Path(__file__).resolve().parent


def load_config() -> dict:
    with (ROOT / "config.toml").open("rb") as fh:
        return tomllib.load(fh)


def update_open(cfg: dict, book: str, marks: dict[str, float] | None = None) -> bool:
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
        append(
            {
                "type": "paper_close",
                "inst_id": inst_id,
                "result": hit,
                "last": last,
                "r": r,
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

    pos = {
        "bias": fiche.bias,
        "entry": fiche.entry,
        "stop": fiche.stop,
        "target": fiche.target,
        "rr": fiche.rr,
        "risk_pct": cfg["risk_pct"],
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "reasons": fiche.reasons,
        "strategy": book,
        **position_size(
            float(fiche.entry),
            float(fiche.stop),
            equity=float(cfg.get("default_equity_usdt", 10000)),
            risk_pct=float(cfg["risk_pct"]),
        ),
    }
    open_state[fiche.inst_id] = pos
    save_open(open_state, book)
    append({"type": "paper_fill", "inst_id": fiche.inst_id, "last": fiche.last, "position": pos}, book=book)
    print(f"{tag}: PAPER FILL  {fiche.bias} @ {fiche.entry}")
    print(f"  stop {fiche.stop}  target {fiche.target}  R:R {fiche.rr:.2f}")


def print_status() -> None:
    for book in ("ict", "fabio"):
        s = stats(book)
        wr = f"{s['win_rate']*100:.1f}%" if s["win_rate"] is not None else "n/a"
        print(f"{book}: fills={s['fills']} vetos={s['stand_downs']} wr={wr} open={list(s['open'])}")


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


def seed_seen_closes(instruments: list[str], bar: str) -> dict[str, int]:
    """Remember the bar the start tick already decided on. 0 = seed failed."""
    seen: dict[str, int] = {}
    for inst in instruments:
        try:
            closed = closed_candle(fetch_candles(inst, bar, 4), bar)
            seen[inst] = closed.ts if closed else 0
        except Exception as exc:
            print(f"{inst}: close-seed error {exc}", file=sys.stderr)
            seen[inst] = 0
    return seen


def hand_off_watch() -> bool:
    """Push journal first. The next runner must not checkout a stale blotter."""
    write_desk()
    if persist_enabled() and not persist_journal("paper watch handoff"):
        print("handoff persist failed — YAML will retry before chaining", file=sys.stderr)
        return False
    return dispatch_next()


def tick(cfg: dict, marks: dict[str, float] | None = None) -> None:
    print(f"\n=== {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")
    mark_all(cfg, marks)
    for inst in cfg["instruments"]:
        try:
            hourly = fetch_candles(inst, cfg["htf_bar"], int(cfg["htf_limit"]))
            entry = fetch_candles(inst, cfg["entry_bar"], int(cfg["entry_limit"]))
            last = float(marks[inst]) if marks and inst in marks else fetch_last(inst)
        except Exception as exc:
            print(f"{inst}: error {exc}", file=sys.stderr)
            append({"type": "error", "inst_id": inst, "error": str(exc)}, book="ict")
            continue
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
    write_desk()
    persist_journal("paper scan")


def watch(cfg: dict, minutes: float, chain_after: float = 0) -> None:
    """Desk loop: WS last for SL/TP, confirmed 15m close for entries."""
    deadline = time.time() + minutes * 60 if minutes > 0 else None
    chain_at = time.time() + chain_after * 60 if chain_after > 0 else None
    marks: dict[str, float] = {}
    chained = False
    last_watchdog = 0.0
    last_desk = 0.0
    last_handoff = 0.0
    bar = cfg["entry_bar"]
    feed = PublicFeed(list(cfg["instruments"]), bar)
    feed.start()
    if not feed.connected.wait(15):
        print("WS not up yet — REST marks until it is", file=sys.stderr)
    tick(cfg)
    seen_close = seed_seen_closes(list(cfg["instruments"]), bar)
    persist_journal("paper watch start")
    print(f"watch WS tickers + {bar} confirm=1 — no live orders")
    try:
        while deadline is None or time.time() < deadline:
            try:
                ev = feed.events.get(timeout=1.0)
            except Empty:
                ev = None
            if isinstance(ev, Tick):
                marks[ev.inst_id] = ev.last
                mark_all(cfg, marks)
            elif isinstance(ev, BarClose):
                if is_decision_bar(ev.inst_id, ev.ts, seen_close):
                    print(f"{ev.inst_id}: {bar} closed @ {ev.close}")
                    marks[ev.inst_id] = ev.close
                    tick(cfg, marks)
            now = time.time()
            if any_open() and feed.stale(8.0) and now - last_watchdog >= 2.0:
                last_watchdog = now
                marks.update(rest_marks(cfg))
                mark_all(cfg, marks)
            if any_open() and now - last_desk >= 120:
                write_desk()
                persist_journal("paper desk")
                last_desk = now
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
