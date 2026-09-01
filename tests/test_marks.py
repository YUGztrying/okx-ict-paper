"""Marking a position the socket does not carry.

update_open runs on every ticker batch — several times a second. Any open
position whose instrument is not in the socket's marks fell through to a
blocking REST ticker call, every single time. That never happened while the
config and the open positions named the same instruments; switching the desk to
perps while two spot positions were still open made it happen on every tick.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paper
from ict import journal

CFG = {"risk_pct": 0.5, "default_equity_usdt": 10000, "max_consecutive_losses": 5,
       "fees": {"taker_pct": 0.05}}
# A long that neither stop nor target can reach at the mark below, so update_open
# marks it and leaves it open.
SPOT = {"bias": "long", "entry": 100.0, "stop": 50.0, "target": 500.0,
        "strategy": "fabio", "qty": 1.0, "risk_usd": 50.0}


class RestMarks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        patcher = patch.object(journal, "JOURNAL_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        journal.invalidate_cache()
        paper._rest_mark.clear()
        self.addCleanup(paper._rest_mark.clear)

    def test_a_socket_mark_never_reaches_rest(self) -> None:
        journal.save_open({"BTC-USDT-SWAP": SPOT})
        with patch.object(paper, "fetch_last", side_effect=AssertionError("REST")) as rest:
            for _ in range(50):
                paper.update_open(CFG, {"BTC-USDT-SWAP": 100.0})
        rest.assert_not_called()

    def test_an_unsubscribed_mark_is_fetched_once_not_per_tick(self) -> None:
        journal.save_open({"BTC-USDT": SPOT})
        with patch.object(paper, "fetch_last", return_value=100.0) as rest:
            for _ in range(50):
                # marks carries the perps the socket is subscribed to; the
                # leftover spot position is not in it.
                paper.update_open(CFG, {"BTC-USDT-SWAP": 79000.0})
        self.assertEqual(rest.call_count, 1)

    def test_the_cached_mark_expires(self) -> None:
        journal.save_open({"BTC-USDT": SPOT})
        with patch.object(paper, "fetch_last", return_value=100.0) as rest:
            paper.update_open(CFG, None)
            paper._rest_mark["BTC-USDT"] = (0.0, 100.0)   # older than the TTL
            paper.update_open(CFG, None)
        self.assertEqual(rest.call_count, 2)

    def test_a_stale_mark_still_closes_the_position(self) -> None:
        """The throttle must not swallow an exit — only slow how often it looks."""
        journal.save_open({"BTC-USDT": SPOT})
        with patch.object(paper, "fetch_last", return_value=40.0):   # through the stop
            paper.update_open(CFG, None)
        self.assertEqual(journal.load_open(), {})
        closes = [e for e in journal._parse_lines() if e["type"] == "paper_close"]
        self.assertEqual(closes[0]["result"], "loss")
        self.assertEqual(closes[0]["strategy"], "fabio")


if __name__ == "__main__":
    unittest.main()
