"""One closed bar is one desk scan.

tick() walks every instrument, so triggering it once per instrument scanned the
whole desk N times per 15m boundary — every instrument journaled N stand-downs,
every boundary N commits. The journal showed it: two full passes 2s apart, one
of them reading a 15m candle that was 2 seconds old.
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paper
from ict import journal
from ict.model import Check, Fiche
from ict.okx_data import Candle

BAR_MS = 900_000
CFG = {
    "instruments": ["BTC-USDT", "ETH-USDT"],
    "entry_bar": "15m",
    "htf_bar": "1H",
    "htf_limit": 240,
    "entry_limit": 96,
    "min_rr": 2.0,
    "session_min_score": 3,
    "max_consecutive_losses": 5,
    "risk_pct": 0.5,
}


def _veto(inst_id: str, *_a, **_k) -> Fiche:
    fiche = Fiche(inst_id=inst_id, last=1.0, bias="unclear")
    fiche.checks.append(Check("htf_bias", False, "stubbed"))
    return fiche


def _aligned_now() -> int:
    """Start of the bar that is currently printing."""
    return (int(time.time() * 1000) // BAR_MS) * BAR_MS


def _series(last_closed_ts: int, *, forming: bool) -> list[Candle]:
    rows = [
        Candle(ts=last_closed_ts - i * BAR_MS, open=1, high=2, low=0.5, close=1.5, volume=10)
        for i in range(6)
    ]
    rows.reverse()
    if forming:
        rows.append(Candle(ts=_aligned_now(), open=1, high=2, low=0.5, close=1.5, volume=1))
    return rows


class ScanOncePerBar(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(journal, "JOURNAL_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        journal.invalidate_cache()
        # Start a bar back so the next bar in this test is still a closed one.
        self.closed_ts = _aligned_now() - 2 * BAR_MS
        self.commits: list[str] = []
        for target, repl in (
            ("fetch_candles", lambda inst, bar, limit: _series(self.closed_ts, forming=True)),
            ("fetch_last", lambda inst: 1.0),
            ("write_desk", lambda *a, **k: None),
            ("persist_journal", lambda reason="paper scan": self.commits.append(reason) or True),
            ("analyze_ict", _veto),
            ("analyze_fabio", _veto),
        ):
            p = patch.object(paper, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _stand_downs(self) -> int:
        return sum(len([e for e in journal._parse_lines(b) if e["type"] == "stand_down"]) for b in ("ict", "fabio"))

    def test_second_trigger_for_the_same_bar_does_nothing(self) -> None:
        seen: dict[str, int] = {}
        self.assertTrue(paper.tick(CFG, seen_close=seen))
        # 2 instruments x 2 books, once.
        self.assertEqual(self._stand_downs(), 4)
        self.assertEqual(self.commits, ["paper scan"])
        self.assertEqual(seen, {"BTC-USDT": self.closed_ts, "ETH-USDT": self.closed_ts})

        # The other instrument's confirm=1 for the same bar arrives ~2s later.
        self.assertFalse(paper.tick(CFG, seen_close=seen))
        self.assertEqual(self._stand_downs(), 4)
        self.assertEqual(self.commits, ["paper scan"])

    def test_next_bar_scans_again(self) -> None:
        seen: dict[str, int] = {}
        paper.tick(CFG, seen_close=seen)
        self.closed_ts += BAR_MS
        self.assertTrue(paper.tick(CFG, seen_close=seen))
        self.assertEqual(self._stand_downs(), 8)

    def test_one_shot_scan_has_no_dedup(self) -> None:
        self.assertTrue(paper.tick(CFG))
        self.assertTrue(paper.tick(CFG))
        self.assertEqual(self._stand_downs(), 8)


class PendingCloses(unittest.TestCase):
    def test_scanned_bars_clear_and_unscanned_ones_stay(self) -> None:
        pending = {"BTC-USDT": 200, "ETH-USDT": 200}
        # The scan covered BTC; REST had not published ETH's bar yet.
        seen = {"BTC-USDT": 200, "ETH-USDT": 100}
        self.assertEqual(paper.still_pending(pending, seen), {"ETH-USDT": 200})

    def test_nothing_left_once_every_bar_is_scanned(self) -> None:
        self.assertEqual(paper.still_pending({"BTC-USDT": 200}, {"BTC-USDT": 200}), {})

    def test_a_close_is_never_dropped_when_the_scan_missed_it(self) -> None:
        self.assertEqual(paper.still_pending({"BTC-USDT": 200}, {}), {"BTC-USDT": 200})


class ClosedBarsOnly(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(journal, "JOURNAL_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        journal.invalidate_cache()
        self.closed_ts = _aligned_now() - BAR_MS
        self.seen_entry: list[list[Candle]] = []

    def test_models_never_see_the_forming_candle(self) -> None:
        def capture(inst_id, last, hourly, entry, **kw):
            self.seen_entry.append(entry)
            return _veto(inst_id)

        with patch.object(paper, "fetch_candles", lambda inst, bar, limit: _series(self.closed_ts, forming=True)), \
             patch.object(paper, "fetch_last", lambda inst: 1.0), \
             patch.object(paper, "write_desk", lambda *a, **k: None), \
             patch.object(paper, "persist_journal", lambda reason="paper scan": True), \
             patch.object(paper, "analyze_fabio", _veto), \
             patch.object(paper, "analyze_ict", capture):
            paper.tick(CFG)

        self.assertTrue(self.seen_entry)
        for entry in self.seen_entry:
            self.assertEqual(entry[-1].ts, self.closed_ts)
            self.assertTrue(all(c.ts + BAR_MS <= int(time.time() * 1000) for c in entry))


if __name__ == "__main__":
    unittest.main()
