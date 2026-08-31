#!/usr/bin/env python
"""Collapse repeated decisions on the same bar.

Before the one-scan-per-bar fix, a 15m close triggered one full desk scan per
instrument, so every instrument was journaled twice per bar. Earlier still, the
`--loop` era rescanned the same bar every few minutes — up to five times.

The desk decides once per bar per instrument per book, so that is what the
journal should hold. This keeps the FIRST entry per (bar, instrument, type)
and drops the repeats.

Trades are never touched. paper_fill and paper_close are the record this desk
exists to produce; they were verified unique, and a dedupe heuristic has no
business near them. Only stand_down and error collapse.

The bar is the 15m wall-clock bucket the entry was logged in — entries land
within seconds of the close they decided on, so the bucket identifies the bar.

    python tools/dedupe_journal.py            # report only
    python tools/dedupe_journal.py --apply    # rewrite the journals
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOKS = {"ict": ROOT / "journal/runs.jsonl", "fabio": ROOT / "journal/fabio/runs.jsonl"}
BAR_SECONDS = 900
COLLAPSIBLE = ("stand_down", "error")


def bar_bucket(event: dict) -> int:
    return int(datetime.fromisoformat(event["logged_at"]).timestamp() // BAR_SECONDS)


def dedupe(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (kept, dropped), input order preserved."""
    seen: set[tuple] = set()
    kept: list[dict] = []
    dropped: list[dict] = []
    for event in events:
        if event.get("type") not in COLLAPSIBLE:
            kept.append(event)
            continue
        key = (bar_bucket(event), event.get("inst_id"), event.get("type"))
        if key in seen:
            dropped.append(event)
            continue
        seen.add(key)
        kept.append(event)
    return kept, dropped


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="rewrite the journals (default: report only)")
    args = ap.parse_args()

    for book, path in BOOKS.items():
        events = read(path)
        kept, dropped = dedupe(events)
        by_type = Counter(e["type"] for e in dropped)
        per_bar = defaultdict(int)
        for e in dropped:
            per_bar[bar_bucket(e)] += 1
        print(f"{book}: {len(events)} -> {len(kept)} lines  (-{len(dropped)})")
        print(f"  dropped: {dict(by_type) or 'none'} across {len(per_bar)} bars")
        trades = sum(1 for e in kept if e["type"] in ("paper_fill", "paper_close"))
        assert trades == sum(1 for e in events if e["type"] in ("paper_fill", "paper_close")), "a trade was dropped"
        print(f"  trades preserved: {trades}")
        if args.apply and dropped:
            path.write_text(
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept),
                encoding="utf-8",
            )
            print("  rewritten")
    if not args.apply:
        print("\nreport only — pass --apply to rewrite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
