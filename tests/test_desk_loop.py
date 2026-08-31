from __future__ import annotations

import json
import unittest

from ict.exits import exit_result, realized_r
from ict.okx_ws import BarClose, Tick, is_decision_bar, parse_message, subscribe_payload
from ict.cloud import persist_enabled, persist_journal, should_dispatch_next


class Exits(unittest.TestCase):
    def test_long_stop_before_target(self) -> None:
        self.assertEqual(exit_result("long", 99, stop=100, target=120), "loss")
        self.assertEqual(exit_result("long", 120, stop=100, target=120), "win")
        self.assertIsNone(exit_result("long", 110, stop=100, target=120))

    def test_short_stop_before_target(self) -> None:
        self.assertEqual(exit_result("short", 121, stop=120, target=80), "loss")
        self.assertEqual(exit_result("short", 80, stop=120, target=80), "win")
        self.assertIsNone(exit_result("short", 100, stop=120, target=80))

    def test_gap_through_stop_is_a_loss_not_a_win(self) -> None:
        # last jumped past both; stop is the conservative fill.
        self.assertEqual(exit_result("long", 50, stop=100, target=120), "loss")

    def test_realized_r(self) -> None:
        self.assertAlmostEqual(realized_r(100, 90, 130, "win"), 3.0)
        self.assertAlmostEqual(realized_r(100, 90, 130, "loss"), -1.0)


class WsParse(unittest.TestCase):
    def test_ticker(self) -> None:
        raw = json.dumps({
            "arg": {"channel": "tickers", "instId": "ETH-USDT"},
            "data": [{"instId": "ETH-USDT", "last": "2467.21"}],
        })
        ev = parse_message(raw)
        self.assertEqual(ev, [Tick("ETH-USDT", 2467.21)])

    def test_forming_candle_is_ignored(self) -> None:
        raw = json.dumps({
            "arg": {"channel": "candle15m", "instId": "ETH-USDT"},
            "data": [["1", "1", "2", "0", "1.5", "1", "1", "1", "0"]],
        })
        self.assertEqual(parse_message(raw), [])

    def test_confirmed_candle_is_a_bar_close(self) -> None:
        raw = json.dumps({
            "arg": {"channel": "candle15m", "instId": "ETH-USDT"},
            "data": [["1700000000000", "2440", "2460", "2430", "2458.9", "1", "1", "1", "1"]],
        })
        self.assertEqual(parse_message(raw), [BarClose("ETH-USDT", 1700000000000, 2458.9)])

    def test_ping_and_ack_are_empty(self) -> None:
        self.assertEqual(parse_message("ping"), [])
        self.assertEqual(parse_message(json.dumps({"event": "subscribe", "arg": {"channel": "tickers"}})), [])

    def test_subscribe_covers_tickers_and_15m(self) -> None:
        payload = subscribe_payload(["BTC-USDT", "ETH-USDT"], "15m")
        channels = {(a["channel"], a["instId"]) for a in payload["args"]}
        self.assertIn(("tickers", "ETH-USDT"), channels)
        self.assertIn(("candle15m", "BTC-USDT"), channels)

    def test_seed_bar_is_not_a_decision(self) -> None:
        seen: dict[str, int] = {}
        self.assertFalse(is_decision_bar("ETH-USDT", 100, seen))
        self.assertEqual(seen["ETH-USDT"], 100)
        self.assertFalse(is_decision_bar("ETH-USDT", 100, seen))

    def test_next_confirm_is_a_decision(self) -> None:
        seen = {"ETH-USDT": 100}
        self.assertTrue(is_decision_bar("ETH-USDT", 200, seen))
        self.assertEqual(seen["ETH-USDT"], 200)
        self.assertFalse(is_decision_bar("ETH-USDT", 200, seen))

    def test_failed_seed_zero_still_fires_first_real_close(self) -> None:
        seen = {"ETH-USDT": 0}
        self.assertTrue(is_decision_bar("ETH-USDT", 1700000000000, seen))


class Chain(unittest.TestCase):
    def test_dispatch_when_we_are_the_only_run(self) -> None:
        self.assertTrue(should_dispatch_next(1, 0))

    def test_skip_when_already_queued_or_doubled(self) -> None:
        self.assertFalse(should_dispatch_next(1, 1))
        self.assertFalse(should_dispatch_next(2, 0))

    def test_persist_is_noop_without_flag(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"PAPER_GIT_PUSH": ""}, clear=False):
            self.assertFalse(persist_enabled())
            self.assertFalse(persist_journal("should not git"))


if __name__ == "__main__":
    unittest.main()
