#!/usr/bin/env python
"""Backtest CLI.

    python -m backtest fetch --days 180
    python -m backtest run --book desk          # the shared book: one slot per bar
    python -m backtest run --book ict           # one model in isolation
    python -m backtest run --book both --inst BTC-USDT-SWAP

`fetch` needs network access to OKX (public endpoints, no key). `run` works
entirely from the cache in data/.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

from backtest import data
from backtest.engine import run as replay
from backtest.report import render

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with (ROOT / "config.toml").open("rb") as fh:
        return tomllib.load(fh)


def cmd_fetch(args: argparse.Namespace, cfg: dict) -> int:
    for inst in args.inst or cfg["instruments"]:
        for bar in (args.entry_bar, args.htf_bar):
            print(f"fetching {inst} {bar} ({args.days}d)...", flush=True)
            try:
                candles = data.fetch(inst, bar, args.days)
            except Exception as exc:
                print(f"  failed: {exc}", file=sys.stderr)
                return 1
            merged = data.dedupe(data.load(inst, bar) + candles)
            path = data.save(merged, inst, bar)
            cov = data.coverage(merged, bar)
            print(f"  {cov['bars']} bars, {cov['gaps']} gaps ({cov['missing_bars']} missing) -> {path}")
    return 0


def cmd_run(args: argparse.Namespace, cfg: dict) -> int:
    # "desk" is the book the money actually trades: both models, one slot,
    # best net R:R wins. "both" replays them in isolation, which is the same
    # history counted twice — useful for comparing the models, not for sizing
    # an account.
    books = ("desk", "ict", "fabio") if args.book == "both" else (args.book,)
    instruments = args.inst or cfg["instruments"]
    missing = False
    for inst in instruments:
        entry = data.load(inst, args.entry_bar)
        hourly = data.load(inst, args.htf_bar)
        if not entry or not hourly:
            print(f"{inst}: no cached data — run `python -m backtest fetch` first", file=sys.stderr)
            missing = True
            continue
        cov = data.coverage(entry, args.entry_bar)
        print(f"\n{inst}: {cov['bars']} {args.entry_bar} bars, {cov['gaps']} gaps")
        for book in books:
            print(render(replay(book, inst, entry, hourly, cfg,
                                entry_bar=args.entry_bar, htf_bar=args.htf_bar)))
    return 1 if missing else 0


def main() -> int:
    cfg = load_config()
    # Shared options live on a parent so they work after the subcommand too —
    # `run --inst BTC-USDT` is what anyone actually types.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--inst", nargs="*", help="instruments (default: config.toml)")
    common.add_argument("--entry-bar", default=cfg.get("entry_bar", "15m"))
    common.add_argument("--htf-bar", default=cfg.get("htf_bar", "1H"))

    ap = argparse.ArgumentParser(prog="backtest", parents=[common],
                                 description="Replay the desk's models over history")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", parents=[common], help="download and cache history (needs OKX access)")
    f.add_argument("--days", type=float, default=180)
    f.set_defaults(func=cmd_fetch)

    r = sub.add_parser("run", parents=[common], help="replay from the cache")
    r.add_argument("--book", choices=("desk", "ict", "fabio", "both"), default="both",
                   help="desk = the shared book (default view includes it)")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
