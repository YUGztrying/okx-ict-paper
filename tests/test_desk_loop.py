from __future__ import annotations

import json
import queue
import unittest

from ict.exits import exit_result, realized_r
from ict.okx_ws import (
    BUSINESS_URL,
    PUBLIC_URL,
    BarClose,
    Tick,
    candle_subscribe,
    drain_events,
    is_decision_bar,
    parse_message,
    subscribe_error,
    subscribe_payload,
    ticker_subscribe,
)
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

    def test_subscribe_error_is_surfaced(self) -> None:
        raw = json.dumps({
            "event": "error",
            "code": "60018",
            "msg": "Subscribe failed, wrong URL or channel:candle15m",
        })
        self.assertIn("candle15m", subscribe_error(raw) or "")
        self.assertEqual(parse_message(raw), [])

    def test_candles_are_not_on_the_public_url(self) -> None:
        self.assertTrue(PUBLIC_URL.endswith("/ws/v5/public"))
        self.assertTrue(BUSINESS_URL.endswith("/ws/v5/business"))
        tickers = {a["channel"] for a in ticker_subscribe(["ETH-USDT"])["args"]}
        candles = {a["channel"] for a in candle_subscribe(["ETH-USDT"], "15m")["args"]}
        self.assertEqual(tickers, {"tickers"})
        self.assertEqual(candles, {"candle15m"})
        self.assertNotIn("candle15m", tickers)

    def test_subscribe_covers_tickers_and_15m(self) -> None:
        payload = subscribe_payload(["BTC-USDT", "ETH-USDT"], "15m")
        channels = {(a["channel"], a["instId"]) for a in payload["args"]}
        self.assertIn(("tickers", "ETH-USDT"), channels)
        self.assertIn(("candle15m", "BTC-USDT"), channels)

    def test_drain_keeps_latest_tick_and_every_close(self) -> None:
        q: queue.Queue[Tick | BarClose] = queue.Queue()
        q.put(Tick("ETH-USDT", 1.0))
        q.put(Tick("ETH-USDT", 2.0))
        q.put(BarClose("ETH-USDT", 100, 2.0))
        q.put(Tick("BTC-USDT", 9.0))
        ticks, closes = drain_events(q, timeout=0.01)
        self.assertEqual(ticks["ETH-USDT"].last, 2.0)
        self.assertEqual(ticks["BTC-USDT"].last, 9.0)
        self.assertEqual(closes, [BarClose("ETH-USDT", 100, 2.0)])

    def test_bar_already_scanned_is_not_a_decision(self) -> None:
        seen = {"ETH-USDT": 100}
        self.assertFalse(is_decision_bar("ETH-USDT", 100, seen))

    def test_next_confirm_is_a_decision(self) -> None:
        seen = {"ETH-USDT": 100}
        self.assertTrue(is_decision_bar("ETH-USDT", 200, seen))

    def test_unseen_instrument_fires(self) -> None:
        # A start tick whose fetch failed records nothing for that instrument.
        # Missing must mean "not decided yet", never "already seeded".
        self.assertTrue(is_decision_bar("ETH-USDT", 1700000000000, {}))

    def test_predicate_does_not_record_the_bar(self) -> None:
        # tick() records what it analyzed. If this marked the bar seen, the
        # scan the caller is about to run would skip the triggering instrument.
        seen: dict[str, int] = {}
        is_decision_bar("ETH-USDT", 100, seen)
        self.assertEqual(seen, {})


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
