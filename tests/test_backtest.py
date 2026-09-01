"""Backtest guardrails.

A backtest that lies is worse than none: it manufactures confidence. The two
lies that matter are look-ahead (the model sees a bar that had not closed) and
optimistic exits (a bar that covers stop and target counted as a win). Both are
pinned here.
"""

from __future__ import annotations

import json
import random
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
        self.assertGreater(res.skipped["error:ict:ValueError"], 0)
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



class DeskBook(unittest.TestCase):
    """The desk trades one book, so the backtest has to replay one book.

    Summing an ICT run and a Fabio run counts the same history twice: in real
    life the second signal on a bar does not get a position, it gets a
    crowded_out line. A backtest that misses this overstates both strategies.
    """

    def _fiche(self, inst_id, rr, stop_pct):
        entry = 100.0
        risk = entry * stop_pct
        f = Fiche(inst_id=inst_id, last=entry, bias="long")
        f.checks.append(Check("all", True, "stub"))
        f.entry, f.stop, f.target = entry, entry - risk, entry + rr * risk
        f.rr = rr
        return f

    def test_only_one_model_gets_the_slot(self) -> None:
        entry = series(200, BAR_MS)
        hourly = series(260, HOUR_MS)
        # fabio looks better gross; its stop is 5x tighter, so fees eat it.
        ict = lambda inst, *a, **k: self._fiche(inst, 2.0, 0.010)
        fabio = lambda inst, *a, **k: self._fiche(inst, 2.2, 0.002)
        with patch.dict("backtest.engine.BOOKS", {"ict": ict, "fabio": fabio}):
            res = run("desk", "BTC-USDT-SWAP", entry, hourly, CFG)
        self.assertTrue(res.trades)
        # every fill is ICT's, and every one of them cost Fabio a signal
        self.assertEqual({t.book for t in res.trades}, {"ict"})
        self.assertEqual(res.crowded_out["fabio"], len(res.trades))
        self.assertEqual(res.crowded_out["ict"], 0)

    def test_a_vetoed_model_crowds_out_nobody(self) -> None:
        entry = series(200, BAR_MS)
        hourly = series(260, HOUR_MS)

        def vetoed(inst, *a, **k):
            f = Fiche(inst_id=inst, last=100.0, bias="unclear")
            f.checks.append(Check("htf_bias", False, "stub"))
            return f

        with patch.dict("backtest.engine.BOOKS",
                        {"ict": vetoed, "fabio": lambda inst, *a, **k: self._fiche(inst, 2.0, 0.01)}):
            res = run("desk", "BTC-USDT-SWAP", entry, hourly, CFG)
        self.assertEqual({t.book for t in res.trades}, {"fabio"})
        self.assertEqual(sum(res.crowded_out.values()), 0)
        # a desk run names which model each veto came from
        self.assertTrue(any(k.startswith("ict:") for k in res.vetoes))

    def test_the_shared_book_takes_fewer_trades_than_the_two_apart(self) -> None:
        entry = series(200, BAR_MS)
        hourly = series(260, HOUR_MS)
        models = {"ict": lambda inst, *a, **k: self._fiche(inst, 2.0, 0.010),
                  "fabio": lambda inst, *a, **k: self._fiche(inst, 2.2, 0.002)}
        with patch.dict("backtest.engine.BOOKS", models):
            desk = run("desk", "BTC-USDT-SWAP", entry, hourly, CFG)
            apart = sum(len(run(b, "BTC-USDT-SWAP", entry, hourly, CFG).trades) for b in ("ict", "fabio"))
        self.assertLess(len(desk.trades), apart)



class CoinFlipControl(unittest.TestCase):
    """The control has to be a control.

    Every other book answers "how much does this strategy make". This one
    answers the question underneath: does the entry logic do anything at all.
    That only means something if the control itself is a fair coin with the
    desk's geometry, so those properties are pinned here.
    """

    CFG = dict(CFG, random_stop_pct=0.005, random_rr=2.0, loss_cooldown_hours=24)

    def _walk(self, seed: int, n: int = 3000) -> list[Candle]:
        rng = random.Random(seed)
        px, rows = 79000.0, []
        for i in range(n):
            px *= 1 + rng.gauss(0, 0.002)
            rows.append(Candle(ts=i * BAR_MS, open=px, high=px * 1.003,
                               low=px * 0.997, close=px, volume=10))
        return rows

    def _run(self, cfg=None):
        entry = self._walk(4)
        hourly = series(400, HOUR_MS)
        return run("random", "BTC-USDT-SWAP", entry, hourly, cfg or self.CFG)

    def test_the_same_bars_give_the_same_trades(self) -> None:
        """Seeded from the bar, so two books can be compared, not re-rolled."""
        a, b = self._run(), self._run()
        self.assertTrue(a.trades)
        self.assertEqual([(t.opened_ts, t.bias, t.entry, t.stop) for t in a.trades],
                         [(t.opened_ts, t.bias, t.entry, t.stop) for t in b.trades])

    def test_it_actually_flips(self) -> None:
        res = self._run()
        longs = sum(1 for t in res.trades if t.bias == "long")
        self.assertGreater(longs, 0)
        self.assertLess(longs, len(res.trades))
        self.assertAlmostEqual(longs / len(res.trades), 0.5, delta=0.15)

    def test_it_borrows_the_desk_geometry(self) -> None:
        res = self._run()
        for t in res.trades:
            self.assertAlmostEqual(t.stop_pct, 0.005, places=6)
            self.assertAlmostEqual(abs(t.target - t.entry) / abs(t.entry - t.stop), 2.0, places=6)

    def test_a_wider_target_is_hit_less_often(self) -> None:
        """1/(1+R) falling as R grows is the whole reason R:R is not free."""
        near = self._run(dict(self.CFG, random_rr=1.0))
        far = self._run(dict(self.CFG, random_rr=4.0))
        rate = lambda r: sum(1 for t in r.closed if t.result == "win") / len(r.closed)
        self.assertGreater(rate(near), rate(far))

    def test_the_control_never_flatters_itself(self) -> None:
        """A bar covering stop and target counts as a loss, so the measured rate
        sits at or below 1/(1+R). The real books carry the same drag, which is
        why comparing them to this is fair."""
        res = self._run()
        won = sum(1 for t in res.closed if t.result == "win") / len(res.closed)
        self.assertLessEqual(won, 1 / (1 + 2.0) + 0.02)



class Significance(unittest.TestCase):
    """A backtest that reports an average without its error invites reading
    noise as edge. Four trades at +1.17 R carry an error of +/-1.25 R — the
    uncertainty is larger than the estimate."""

    def _book(self, n: int, wr: float, rr: float, seed: int = 1) -> Result:
        res = Result(book="x", inst_id="BTC-USDT-SWAP")
        rng = random.Random(seed)
        outcomes = ["win"] * round(n * wr) + ["loss"] * (n - round(n * wr))
        rng.shuffle(outcomes)
        for i, o in enumerate(outcomes):
            t = Trade(inst_id="BTC-USDT-SWAP", book="x", bias="long", entry=100.0,
                      stop=99.5, target=100 + rr * 0.5, rr=rr, opened_ts=i,
                      qty=100.0, risk_usd=50.0, stop_pct=0.005)
            t.result, t.r, t.closed_ts, t.r_net = o, (rr if o == "win" else -1.0), i, None
            res.trades.append(t)
        return res

    def test_four_trades_prove_nothing(self) -> None:
        m = metrics(self._book(4, 0.5, 3.34))
        self.assertAlmostEqual(m["expectancy_r"], 1.17, places=2)
        self.assertGreater(m["se_r"], abs(m["expectancy_r"]))   # error beats the estimate
        self.assertLess(abs(m["t_stat"]), 2)
        self.assertIn("INDISTINGUABLE", render(self._book(4, 0.5, 3.34)))

    def test_four_hundred_coin_flips_prove_nothing_either(self) -> None:
        m = metrics(self._book(400, 0.273, 2.56))
        self.assertLess(abs(m["t_stat"]), 2)
        # An effect this small needs a sample no desk will ever collect.
        self.assertGreater(m["n_for_significance"], 5000)

    def test_a_real_effect_is_called_significant(self) -> None:
        """The verdict is not rigged to always say no."""
        m = metrics(self._book(500, 0.60, 2.0))
        self.assertGreater(m["t_stat"], 2)
        self.assertIn("significatif", render(self._book(500, 0.60, 2.0)))

    def test_the_error_shrinks_with_the_square_root_of_n(self) -> None:
        small = metrics(self._book(100, 0.3, 2.0, seed=7))
        large = metrics(self._book(1600, 0.3, 2.0, seed=7))
        self.assertAlmostEqual(small["se_r"] / large["se_r"], 4.0, delta=0.6)

    def test_one_trade_has_no_error_to_report(self) -> None:
        m = metrics(self._book(1, 1.0, 2.0))
        self.assertEqual(m["se_r"], 0.0)
        self.assertIsNone(m["t_stat"])
        self.assertIn("pas assez de trades", render(self._book(1, 1.0, 2.0)))



class IntrabarBracket(unittest.TestCase):
    """A 15m bar says where price went, not in what order.

    When its range covers stop and target, the rule chosen decides the trade.
    The desk's live rule is stop-first, so that stays the default — a backtest
    must never flatter itself. But the live desk sits a tick stream and sees
    the real order, so it lands somewhere above that floor. Running both ends
    says how wide the unknown is.
    """

    def _spanning(self, bias: str) -> Candle:
        """One bar covering both 95 and 110."""
        return Candle(ts=0, open=100, high=110, low=95, close=100, volume=1)

    def test_a_spanning_bar_is_a_loss_by_default(self) -> None:
        bar = self._spanning("long")
        self.assertEqual(bar_exit("long", bar, stop=95, target=110), "loss")
        self.assertEqual(bar_exit("short", bar, stop=110, target=95), "loss")

    def test_the_other_end_of_the_bracket_calls_it_a_win(self) -> None:
        bar = self._spanning("long")
        self.assertEqual(bar_exit("long", bar, 95, 110, favour="target"), "win")
        self.assertEqual(bar_exit("short", bar, 110, 95, favour="target"), "win")

    def test_an_unambiguous_bar_reads_the_same_either_way(self) -> None:
        """Only a bar touching both levels is ambiguous. Nothing else moves."""
        only_stop = Candle(ts=0, open=100, high=101, low=90, close=95, volume=1)
        only_target = Candle(ts=0, open=100, high=120, low=99, close=115, volume=1)
        neither = Candle(ts=0, open=100, high=101, low=99, close=100, volume=1)
        for favour in ("stop", "target"):
            self.assertEqual(bar_exit("long", only_stop, 95, 110, favour=favour), "loss")
            self.assertEqual(bar_exit("long", only_target, 95, 110, favour=favour), "win")
            self.assertIsNone(bar_exit("long", neither, 95, 110, favour=favour))

    def test_the_optimistic_end_never_scores_worse(self) -> None:
        entry = series(400, BAR_MS)
        hourly = series(300, HOUR_MS)
        model = lambda inst, *a, **k: self._fiche(inst)
        with patch.dict("backtest.engine.BOOKS", {"ict": model}):
            floor = run("ict", "BTC-USDT-SWAP", entry, hourly, dict(CFG, intrabar="stop"))
            ceiling = run("ict", "BTC-USDT-SWAP", entry, hourly, dict(CFG, intrabar="target"))
        wins = lambda r: sum(1 for t in r.closed if t.result == "win")
        self.assertGreaterEqual(wins(ceiling), wins(floor))

    def _fiche(self, inst_id):
        f = Fiche(inst_id=inst_id, last=100.0, bias="long")
        f.checks.append(Check("all", True, "stub"))
        f.entry, f.stop, f.target = 100.0, 99.0, 102.0
        f.rr = 2.0
        return f


if __name__ == "__main__":
    unittest.main()
