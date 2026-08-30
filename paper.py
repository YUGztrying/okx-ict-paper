#!/usr/bin/env python
"""ICT paper desk. Never places OKX orders."""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from ict.journal import append, consecutive_losses, load_open, save_open, stats, write_desk
from ict.model import Fiche, analyze
from ict.okx_data import fetch_candles, fetch_last

ROOT = Path(__file__).resolve().parent


def load_config() -> dict:
    with (ROOT / "config.toml").open("rb") as fh:
        return tomllib.load(fh)


def update_open(cfg: dict) -> None:
    open_state = load_open()
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
            }
        )
        del open_state[inst_id]
        changed = True
        print(f"CLOSED {inst_id} {hit} @ {last}  R={r:.2f}")
    if changed:
        save_open(open_state)


def run_one(inst_id: str, cfg: dict) -> Fiche:
    hourly = fetch_candles(inst_id, cfg["htf_bar"], int(cfg["htf_limit"]))
    entry = fetch_candles(inst_id, cfg["entry_bar"], int(cfg["entry_limit"]))
    last = fetch_last(inst_id)
    fiche = analyze(
        inst_id,
        last,
        hourly,
        entry,
        min_rr=float(cfg["min_rr"]),
        session_min=int(cfg["session_min_score"]),
    )
    return fiche


def maybe_fill(fiche: Fiche, cfg: dict) -> None:
    open_state = load_open()
    if fiche.inst_id in open_state:
        append(
            {
                "type": "stand_down",
                "inst_id": fiche.inst_id,
                "missing": ["already_open"],
                "last": fiche.last,
                "reasons": fiche.reasons,
            }
        )
        print(f"{fiche.inst_id}: stand down — already in a paper position")
        return

    losses = consecutive_losses(fiche.inst_id)
    if losses >= int(cfg["max_consecutive_losses"]):
        append(
            {
                "type": "stand_down",
                "inst_id": fiche.inst_id,
                "missing": ["loss_streak"],
                "last": fiche.last,
                "reasons": fiche.reasons + [f"streak={losses}"],
            }
        )
        print(f"{fiche.inst_id}: stand down — {losses} losses in a row")
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
            }
        )
        print(f"{fiche.inst_id}: STAND DOWN  last={fiche.last}  missing={', '.join(fiche.missing)}")
        for c in fiche.checks:
            mark = "OK" if c.ok else "NO"
            print(f"  [{mark}] {c.name}: {c.detail}")
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
    }
    open_state[fiche.inst_id] = pos
    save_open(open_state)
    append({"type": "paper_fill", "inst_id": fiche.inst_id, "last": fiche.last, "position": pos})
    print(f"{fiche.inst_id}: PAPER FILL  {fiche.bias} @ {fiche.entry}")
    print(f"  stop {fiche.stop}  target {fiche.target}  R:R {fiche.rr:.2f}")
    for c in fiche.checks:
        print(f"  [OK] {c.name}: {c.detail}")


def print_status() -> None:
    s = stats()
    wr = f"{s['win_rate']*100:.1f}%" if s["win_rate"] is not None else "n/a"
    avg = f"{s['avg_r']:.2f}" if s["avg_r"] is not None else "n/a"
    print(f"fills={s['fills']}  wins={s['wins']}  losses={s['losses']}  stand_downs={s['stand_downs']}")
    print(f"win_rate={wr}  avg_R={avg}")
    if s["open"]:
        print("open:")
        for inst, pos in s["open"].items():
            print(f"  {inst} {pos['bias']} entry={pos['entry']} stop={pos['stop']} tp={pos['target']}")
    else:
        print("open: none")


def main() -> int:
    parser = argparse.ArgumentParser(description="ICT paper desk — no live orders")
    parser.add_argument("--loop", type=int, metavar="MIN", help="repeat every N minutes")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    cfg = load_config()

    if args.status:
        print_status()
        return 0

    def tick() -> None:
        print(f"\n=== {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")
        update_open(cfg)
        for inst in cfg["instruments"]:
            try:
                fiche = run_one(inst, cfg)
                maybe_fill(fiche, cfg)
            except Exception as exc:
                print(f"{inst}: error {exc}", file=sys.stderr)
                append({"type": "error", "inst_id": inst, "error": str(exc)})
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
