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
from ict.okx_data import fetch_candles, fetch_last

ROOT = Path(__file__).resolve().parent


def load_config() -> dict:
    with (ROOT / "config.toml").open("rb") as fh:
        return tomllib.load(fh)


def update_open(cfg: dict, book: str) -> None:
    open_state = load_open(book)
    changed = False
    for inst_id, pos in list(open_state.items()):
        last = fetch_last(inst_id)
        stop = float(pos["stop"])
        target = float(pos["target"])
        entry = float(pos["entry"])
        side = pos["bias"]
        hit = None
        if side == "long":
            if last <= stop:
                hit = "loss"
            elif last >= target:
                hit = "win"
        else:
            if last >= stop:
                hit = "loss"
            elif last <= target:
                hit = "win"
        if not hit:
            continue
        risk = abs(entry - stop)
        r = (abs(target - entry) / risk) if hit == "win" and risk else (-1.0 if hit == "loss" else 0.0)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper desk — ICT + Fabio AAA, no live orders")
    parser.add_argument("--loop", type=int, metavar="MIN", help="repeat every N minutes")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    cfg = load_config()

    if args.status:
        print_status()
        return 0

    def tick() -> None:
        print(f"\n=== {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")
        for book in ("ict", "fabio"):
            update_open(cfg, book)
        for inst in cfg["instruments"]:
            hourly = None
            entry = None
            last = None
            try:
                hourly = fetch_candles(inst, cfg["htf_bar"], int(cfg["htf_limit"]))
                entry = fetch_candles(inst, cfg["entry_bar"], int(cfg["entry_limit"]))
                last = fetch_last(inst)
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

    tick()
    if args.loop:
        minutes = args.loop
        print(f"looping every {minutes} min — Ctrl+C to stop. No live orders.")
        while True:
            time.sleep(minutes * 60)
            tick()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
