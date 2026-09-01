#!/usr/bin/env python
"""Backtest CLI.

    python -m backtest fetch --days 180
    python -m backtest run --book desk          # the shared book: one slot per bar
    python -m backtest run --book ict           # one model in isolation
    python -m backtest run --book random        # the coin-flip control
    python -m backtest run --book all           # every book, control included
    python -m backtest sweep                    # what each setting costs

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
from backtest.report import metrics, render

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
    if args.book == "all":
        books = ("desk", "ict", "fabio", "random")
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


# What to try, and what each one is really asking. Every variant is the base
# config with one thing changed, so the column that moves is the answer.
VARIANTS: list[tuple[str, dict]] = [
    ("actuel", {}),
    ("R:R applique au net", {"min_rr_on_net": True}),
    ("levier <= 3x", {"max_leverage": 3.0}),
    ("levier <= 2x", {"max_leverage": 2.0}),
    ("ordres limites (maker)", {"fees": {"taker_pct": 0.02, "maker_pct": 0.02}}),
    ("pas de disjoncteur", {"loss_cooldown_hours": 0}),
    # The other end of the intrabar bracket. The live desk sits a tick stream
    # and lands between this row and "actuel"; if both lose, the ambiguity was
    # never what decided the result.
    ("sorties optimistes", {"intrabar": "target"}),
    ("optimiste + maker", {"intrabar": "target",
                           "fees": {"taker_pct": 0.02, "maker_pct": 0.02}}),
    ("tout combine", {"min_rr_on_net": True, "max_leverage": 3.0,
                      "fees": {"taker_pct": 0.02, "maker_pct": 0.02}}),
]


def cmd_sweep(args: argparse.Namespace, cfg: dict) -> int:
    """Run the shared book under each variant and print what each one costs.

    One number decides most of this desk: the stop distance. It sets the
    leverage (risk_pct / stop_pct) and it sets what the fees cost in R
    (2 * rate / stop_pct). Guessing at the settings around it is how a year of
    forward-testing gets spent learning something a replay answers in a minute.
    """
    instruments = args.inst or cfg["instruments"]
    series = {}
    for inst in instruments:
        entry, hourly = data.load(inst, args.entry_bar), data.load(inst, args.htf_bar)
        if not entry or not hourly:
            print(f"{inst}: no cached data — run `python -m backtest fetch` first", file=sys.stderr)
            return 1
        series[inst] = (entry, hourly)

    header = f"{'variante':<24}{'trades':>7}{'R brut':>9}{'R net':>9}{'frais':>10}{'annules':>9}{'ecartes':>9}"
    print(f"\nlivre partage · {', '.join(instruments)} · {args.entry_bar}")
    print(header)
    print("-" * len(header))
    for label, override in VARIANTS:
        trial = {**cfg, **override}
        totals = dict(trades=0, r=0.0, r_net=0.0, fees=0.0, eaten=0, blocked=0)
        for inst, (entry, hourly) in series.items():
            res = replay("desk", inst, entry, hourly, trial,
                         entry_bar=args.entry_bar, htf_bar=args.htf_bar)
            m = metrics(res)
            totals["trades"] += m["trades"]
            totals["r"] += m["total_r"]
            totals["r_net"] += m["total_r_net"]
            totals["fees"] += m["fees_usd"]
            totals["eaten"] += m["eaten_by_fees"]
            # Signals the desk turned away for a reason it can act on.
            totals["blocked"] += sum(res.skipped[k] for k in
                                     ("stop_too_tight", "rr_net_below_min"))
        print(f"{label:<24}{totals['trades']:>7}{totals['r']:>+9.2f}{totals['r_net']:>+9.2f}"
              f"{totals['fees']:>10,.0f}{totals['eaten']:>9}{totals['blocked']:>9}")
    print("\n'annules' = trades gagnants bruts devenus perdants apres frais.")
    print("'ecartes' = setups refuses par le plafond de levier ou le R:R net.")
    return 0


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
    r.add_argument("--book", choices=("desk", "ict", "fabio", "random", "both", "all"),
                   default="both",
                   help="desk = the shared book · random = the coin-flip control · "
                        "all = every book including the control")
    r.set_defaults(func=cmd_run)

    w = sub.add_parser("sweep", parents=[common],
                       help="run the shared book under each setting variant and compare")
    w.set_defaults(func=cmd_sweep)

    args = ap.parse_args()
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
