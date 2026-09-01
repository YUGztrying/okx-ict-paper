"""Backtest guardrails.

A backtest that lies is worse than none: it manufactures confidence. The two
lies that matter are look-ahead (the model sees a bar that had not closed) and
optimistic exits (a bar that covers stop and target counted as a win). Both are
pinned here.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backtest import data
from backtest.engine import Result, Trade, bar_exit, run
from backtest.report import metrics, render
from ict.model import Check, Fiche
from ict.okx_data import Candle

BAR_MS = 900_000
HOUR_MS = 3_600_000
CFG = {
    "min_rr": 2.0,
    "session_min_score": 3,
    "max_consecutive_losses": 5,
    "entry_limit": 4,
    "htf_limit": 2,
    "default_equity_usdt": 10000.0,
    "risk_pct": 0.5,
}


def series(n: int, width: int, start_ts: int = 0, price: float = 100.0) -> list[Candle]:
    return [
        Candle(ts=start_ts + i * width, open=price, high=price + 1, low=price - 1, close=price, volume=10.0)
        for i in range(n)
    ]


class BarExit(unittest.TestCase):
    def test_long_stop_wins_over_target_in_the_same_bar(self) -> None:
        # The bar covers both. We cannot know the order, so it is a loss.
        bar = Candle(ts=0, open=100, high=130, low=90, close=120, volume=1)
        self.assertEqual(bar_exit("long", bar, stop=95, target=125), "loss")

    def test_short_stop_wins_over_target_in_the_same_bar(self) -> None:
        bar = Candle(ts=0, open=100, high=130, low=70, close=80, volume=1)
        self.assertEqual(bar_exit("short", bar, stop=125, target=75), "loss")

    def test_clean_target(self) -> None:
        bar = Candle(ts=0, open=100, high=126, low=99, close=125, volume=1)
        self.assertEqual(bar_exit("long", bar, stop=95, target=125), "win")

    def test_untouched(self) -> None:
        bar = Candle(ts=0, open=100, high=110, low=98, close=105, volume=1)
        self.assertIsNone(bar_exit("long", bar, stop=95, target=125))


class NoLookAhead(unittest.TestCase):
    def test_models_only_see_bars_closed_at_decision_time(self) -> None:
        entry = series(40, BAR_MS)
        hourly = series(10, HOUR_MS)
        seen = []

        def spy(inst_id, last, htf, window, cfg, now):
            seen.append((now, window, htf, last))
            f = Fiche(inst_id=inst_id, last=last, bias="unclear")
            f.checks.append(Check("htf_bias", False, "stub"))
            return f

        with patch.dict("backtest.engine.BOOKS", {"ict": spy}):
            run("ict", "BTC-USDT", entry, hourly, CFG)

        self.assertTrue(seen)
        for now, window, htf, last in seen:
            now_ms = now.timestamp() * 1000
            # every entry bar handed over had fully closed
            for c in window:
                self.assertLessEqual(c.ts + BAR_MS, now_ms)
            # so had every HTF bar
            for c in htf:
                self.assertLessEqual(c.ts + HOUR_MS, now_ms)
            # the decision bar's close is the fill price and the timestamp
            self.assertEqual(window[-1].ts + BAR_MS, now_ms)
            self.assertEqual(last, window[-1].close)

    def test_window_is_capped_at_entry_limit(self) -> None:
        entry = series(40, BAR_MS)
        hourly = series(10, HOUR_MS)
        widths = []

        def spy(inst_id, last, htf, window, cfg, now):
            widths.append(len(window))
            f = Fiche(inst_id=inst_id, last=last, bias="unclear")
            f.checks.append(Check("x", False, ""))
            return f

        with patch.dict("backtest.engine.BOOKS", {"ict": spy}):
            run("ict", "BTC-USDT", entry, hourly, CFG)
        self.assertTrue(widths)
        self.assertTrue(all(w <= CFG["entry_limit"] for w in widths), widths)


def _long_signal(entry_px: float, stop: float, target: float):
    def analyze(inst_id, last, htf, window, cfg, now):
        f = Fiche(inst_id=inst_id, last=last, bias="long")
        f.checks.append(Check("all", True, "stub"))
        f.entry, f.stop, f.target, f.rr = entry_px, stop, target, 2.0
        return f

    return analyze


class Replay(unittest.TestCase):
    def test_a_winning_trade_is_recorded_with_its_r(self) -> None:
        entry = series(10, BAR_MS)
        # bar 8 spikes to the target
        entry[8] = Candle(ts=entry[8].ts, open=100, high=130, low=99, close=128, volume=1)
        hourly = series(10, HOUR_MS)
        with patch.dict("backtest.engine.BOOKS", {"ict": _long_signal(100.0, 90.0, 120.0)}):
            res = run("ict", "BTC-USDT", entry, hourly, CFG)
        self.assertEqual(len(res.closed), 1)
        trade = res.closed[0]
        self.assertEqual(trade.result, "win")
        self.assertAlmostEqual(trade.r, 2.0)
        self.assertAlmostEqual(trade.risk_usd, 50.0)
        self.assertAlmostEqual(trade.pnl, 100.0)

    def test_a_position_cannot_close_on_the_bar_that_opened_it(self) -> None:
        # The opening bar itself already spans stop and target; entering at its
        # close must not let it exit on the same bar.
        entry = series(8, BAR_MS)
        entry[5] = Candle(ts=entry[5].ts, open=100, high=130, low=80, close=100, volume=1)
        hourly = series(10, HOUR_MS)
        with patch.dict("backtest.engine.BOOKS", {"ict": _long_signal(100.0, 90.0, 120.0)}):
            res = run("ict", "BTC-USDT", entry, hourly, CFG)
        opened = [t for t in res.trades if t.opened_ts == entry[5].ts]
        for t in opened:
            self.assertNotEqual(t.closed_ts, entry[5].ts)

    def test_vetoes_are_counted_per_filter(self) -> None:
        entry = series(12, BAR_MS)
        hourly = series(10, HOUR_MS)

        def analyze(inst_id, last, htf, window, cfg, now):
            f = Fiche(inst_id=inst_id, last=last, bias="unclear")
            f.checks.append(Check("amd", False, ""))
            f.checks.append(Check("risk", False, ""))
            f.checks.append(Check("session", True, ""))
            return f

        with patch.dict("backtest.engine.BOOKS", {"ict": analyze}):
            res = run("ict", "BTC-USDT", entry, hourly, CFG)
        self.assertEqual(res.vetoes["amd"], res.decisions)
        self.assertEqual(res.vetoes["risk"], res.decisions)
        self.assertNotIn("session", res.vetoes)
        self.assertEqual(len(res.trades), 0)

    def test_loss_streak_halts_the_book(self) -> None:
        # Every bar stops out immediately; after max_consecutive_losses the
        # engine must stop taking trades, like the live desk.
        cfg = dict(CFG, max_consecutive_losses=2)
        entry = [
            Candle(ts=i * BAR_MS, open=100, high=101, low=80, close=100, volume=1)
            for i in range(20)
        ]
        hourly = series(10, HOUR_MS)
        with patch.dict("backtest.engine.BOOKS", {"ict": _long_signal(100.0, 90.0, 120.0)}):
            res = run("ict", "BTC-USDT", entry, hourly, cfg)
        self.assertEqual(len([t for t in res.closed if t.result == "loss"]), 2)
        self.assertGreater(res.skipped["loss_streak"], 0)

    def test_a_raising_model_is_recorded_not_fatal(self) -> None:
        entry = series(12, BAR_MS)
        hourly = series(10, HOUR_MS)

        def boom(*a, **k):
            raise ValueError("bad candle")

        with patch.dict("backtest.engine.BOOKS", {"ict": boom}):
            res = run("ict", "BTC-USDT", entry, hourly, CFG)
        self.assertGreater(res.skipped["error:ValueError"], 0)
        self.assertEqual(res.decisions, 0)


class Metrics(unittest.TestCase):
    def _res(self, rs: list[float]) -> Result:
        res = Result(book="ict", inst_id="BTC-USDT")
        for i, r in enumerate(rs):
            res.trades.append(
                Trade(inst_id="BTC-USDT", book="ict", bias="long", entry=100, stop=90,
                      target=120, rr=2.0, opened_ts=i * BAR_MS, closed_ts=(i + 1) * BAR_MS,
                      result="win" if r > 0 else "loss", r=r, risk_usd=50.0)
            )
        res.decisions = len(rs)
        return res

    def test_expectancy_and_profit_factor(self) -> None:
        m = metrics(self._res([2.0, -1.0, 2.0, -1.0]))
        self.assertAlmostEqual(m["total_r"], 2.0)
        self.assertAlmostEqual(m["expectancy_r"], 0.5)
        self.assertAlmostEqual(m["profit_factor"], 2.0)
        self.assertAlmostEqual(m["win_rate"], 0.5)
        self.assertAlmostEqual(m["pnl_usd"], 100.0)

    def test_drawdown_is_measured_from_the_peak(self) -> None:
        m = metrics(self._res([2.0, -1.0, -1.0]))
        self.assertAlmostEqual(m["max_drawdown_r"], -2.0)

    def test_no_trades_renders_without_crashing(self) -> None:
        res = Result(book="ict", inst_id="BTC-USDT")
        res.decisions = 10
        res.vetoes["amd"] = 10
        out = render(res)
        self.assertIn("no closed trade", out)
        self.assertIn("never passed", out)


class Cache(unittest.TestCase):
    def test_round_trip_and_dedupe(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTC-USDT-15m.jsonl"
            rows = series(5, BAR_MS) + series(2, BAR_MS)  # overlapping pages
            data.save(rows, "BTC-USDT", "15m", path)
            back = data.load("BTC-USDT", "15m", path)
            self.assertEqual(len(back), 5)
            self.assertEqual([c.ts for c in back], sorted(c.ts for c in back))

    def test_coverage_reports_gaps(self) -> None:
        rows = [Candle(ts=t, open=1, high=1, low=1, close=1, volume=1)
                for t in (0, BAR_MS, 4 * BAR_MS)]
        cov = data.coverage(rows, "15m")
        self.assertEqual(cov["bars"], 3)
        self.assertEqual(cov["gaps"], 1)
        self.assertEqual(cov["missing_bars"], 2)

    def test_fetch_stops_when_history_runs_out(self) -> None:
        pages = [series(3, BAR_MS, start_ts=2 * BAR_MS), series(2, BAR_MS, start_ts=0), []]
        calls = []

        def fake(inst, bar, limit, after):
            calls.append(after)
            return pages[len(calls) - 1] if len(calls) <= len(pages) else []

        rows = data.fetch("BTC-USDT", "15m", days=365, now_ms=10 * BAR_MS, fetcher=fake, pause=0)
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.ts for c in rows], [0, BAR_MS, 2 * BAR_MS, 3 * BAR_MS, 4 * BAR_MS])

    def test_fetch_does_not_loop_on_a_stuck_cursor(self) -> None:
        calls = []

        def stuck(inst, bar, limit, after):
            calls.append(after)
            return series(3, BAR_MS, start_ts=0)  # same page forever

        data.fetch("BTC-USDT", "15m", days=365, now_ms=10**12, fetcher=stuck, pause=0)
        self.assertLessEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
