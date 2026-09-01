"""What a stop distance decides, and the two ways it used to decide it silently.

A 365-day replay on perps surfaced three things no unit test was watching:
the loss breaker never released, a tight stop bought an enormous position, and
the R:R gate was applied to a number the account never sees. All three are
arithmetic, not opinion, so all three are pinned here.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paper
from backtest.engine import run as replay
from ict import journal
from ict.instruments import Spec
from ict.model import Check, Fiche
from ict.okx_data import Candle
from ict.sizing import leverage, position_size

BTC = Spec("BTC-USDT-SWAP", ct_val=0.01, ct_val_ccy="BTC", lot_sz=0.1, min_sz=0.1, tick_sz=0.1)
CFG = {
    "risk_pct": 0.5,
    "default_equity_usdt": 10000,
    "max_consecutive_losses": 5,
    "loss_cooldown_hours": 24,
    "one_position_per_asset": True,
    "min_rr": 2.0,
    "fabio_min_rr": 1.5,
    "max_leverage": 5.0,
    "fees": {"taker_pct": 0.05},
}


def setup(inst: str, rr: float, stop_pct: float, entry: float = 79000.0) -> Fiche:
    risk = entry * stop_pct
    f = Fiche(inst_id=inst, last=entry, bias="long")
    f.checks.append(Check("all", True, "stub"))
    f.entry, f.stop, f.target = entry, entry - risk, entry + rr * risk
    f.rr = rr
    return f


class Leverage(unittest.TestCase):
    """leverage = risk_pct / stop_pct. Equity cancels out — the stop decides."""

    def test_the_stop_sets_the_leverage(self) -> None:
        self.assertAlmostEqual(leverage(100.0, 99.0, 0.5), 0.5)      # 1.00% stop
        self.assertAlmostEqual(leverage(100.0, 99.8, 0.5), 2.5)      # 0.20% stop
        self.assertAlmostEqual(leverage(100.0, 99.96, 0.5), 12.5)    # 0.04% stop

    def test_equity_does_not_change_it(self) -> None:
        small = position_size(100.0, 99.96, equity=1_000, risk_pct=0.5)
        large = position_size(100.0, 99.96, equity=1_000_000, risk_pct=0.5)
        self.assertAlmostEqual(small["leverage"], large["leverage"])
        self.assertAlmostEqual(small["leverage"], 12.5)

    def test_the_backtest_finding_reproduces(self) -> None:
        """ICT took $131,090 of ETH on a $10,000 account off a 0.04% stop."""
        size = position_size(2500.0, 2500.0 * 0.9996, equity=10_000, risk_pct=0.5)
        self.assertGreater(size["notional"], 100_000)
        self.assertAlmostEqual(size["leverage"], 12.5, places=6)


class Gates(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        patcher = patch.object(journal, "JOURNAL_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        journal.invalidate_cache()
        spec = patch.object(paper, "instrument_spec", return_value=BTC)
        spec.start()
        self.addCleanup(spec.stop)

    def veto(self, cfg, rr=2.0, stop_pct=0.01):
        pos = paper.build_position(setup("BTC-USDT-SWAP", rr, stop_pct), cfg, "ict")
        return paper.position_veto(pos, cfg, "ict")

    def test_a_stop_too_tight_to_afford_is_refused(self) -> None:
        reason, why = self.veto(CFG, stop_pct=0.0004)   # 12.5x
        self.assertEqual(reason, "stop_too_tight")
        self.assertIn("12.5x", " ".join(why))

    def test_a_normal_stop_passes(self) -> None:
        self.assertIsNone(self.veto(CFG, stop_pct=0.01))   # 0.5x

    def test_no_cap_configured_means_no_gate(self) -> None:
        self.assertIsNone(self.veto({**CFG, "max_leverage": 0}, stop_pct=0.0004))

    def test_the_rr_gate_can_be_moved_onto_the_net(self) -> None:
        """A 2.0 gross setup on a 0.25% stop is 1.14 net. The account gets 1.14."""
        off = self.veto(CFG, rr=2.0, stop_pct=0.0025)
        self.assertIsNone(off)
        reason, why = self.veto({**CFG, "min_rr_on_net": True}, rr=2.0, stop_pct=0.0025)
        self.assertEqual(reason, "rr_net_below_min")
        self.assertIn("brut 2.00", " ".join(why))

    def test_a_wide_stop_survives_the_net_gate(self) -> None:
        # Fees cost 2*rate/stop_pct in R: at a 5% stop that is 0.02 R.
        self.assertIsNone(self.veto({**CFG, "min_rr_on_net": True}, rr=2.5, stop_pct=0.05))


class Breaker(Gates):
    """The loss breaker used to latch. It has to release."""

    def _lose(self, n: int, ago_hours: float) -> None:
        when = datetime.now(timezone.utc) - timedelta(hours=ago_hours)
        for i in range(n):
            journal.append(
                {"logged_at": (when + timedelta(seconds=i)).isoformat(),
                 "type": "paper_close", "inst_id": "BTC-USDT-SWAP", "result": "loss", "r": -1.0},
                strategy="ict",
            )

    def test_five_losses_halt_the_strategy(self) -> None:
        self._lose(5, ago_hours=1)
        blocked = paper.blocked_reason(setup("BTC-USDT-SWAP", 2.0, 0.01), CFG, {}, "ict")
        self.assertEqual(blocked[0], "loss_streak")
        self.assertIn("reprise dans", " ".join(blocked[1]))

    def test_the_halt_releases_after_the_cooldown(self) -> None:
        self._lose(5, ago_hours=30)   # cooldown is 24h
        self.assertIsNone(paper.blocked_reason(setup("BTC-USDT-SWAP", 2.0, 0.01), CFG, {}, "ict"))

    def test_a_win_clears_it_outright(self) -> None:
        self._lose(5, ago_hours=1)
        journal.append({"type": "paper_close", "inst_id": "BTC-USDT-SWAP", "result": "win", "r": 2.0},
                       strategy="ict")
        self.assertIsNone(paper.blocked_reason(setup("BTC-USDT-SWAP", 2.0, 0.01), CFG, {}, "ict"))


BAR_MS = 900_000


def falling(n: int, ms: int) -> list[Candle]:
    """Every bar takes out a long's stop, so every trade loses."""
    return [Candle(ts=i * ms, open=100.0, high=100.4, low=90.0, close=100.0, volume=10)
            for i in range(n)]


class BacktestBreaker(unittest.TestCase):
    def test_the_replay_breaker_releases_too(self) -> None:
        """Fabio took 5 losses on BTC and was blocked for 33,721 more bars —
        the rest of the year. A halted book can never win, so it never cleared."""
        entry, hourly = falling(1200, BAR_MS), falling(400, 3_600_000)
        model = lambda inst, *a, **k: setup(inst, 2.0, 0.01, entry=100.0)
        cfg = {**CFG, "entry_limit": 96, "htf_limit": 240}
        with patch.dict("backtest.engine.BOOKS", {"ict": model}):
            latched = replay("ict", "BTC-USDT-SWAP", entry, hourly, {**cfg, "loss_cooldown_hours": 1e9})
            releases = replay("ict", "BTC-USDT-SWAP", entry, hourly, cfg)
        self.assertEqual(len(latched.trades), 5)             # halts and never resumes
        self.assertGreater(len(releases.trades), 5)          # resumes after the cooldown
        # Every remaining bar of the replay is blocked, and the cooldown run
        # spends far fewer of them halted.
        self.assertGreater(latched.skipped["loss_streak"], 200)
        self.assertLess(releases.skipped["loss_streak"], latched.skipped["loss_streak"])


if __name__ == "__main__":
    unittest.main()
