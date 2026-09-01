"""Perpetual contracts, fees, and what they do to R.

A desk that sizes in fractional contracts and prices off the tick is describing
trades the exchange would reject, and a record that ignores fees overstates
every result it publishes. Both are pinned here.
"""

from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ict import instruments
from ict.fees import Fees, execution_fee, fee_in_r, net_rr, round_trip
from ict.instruments import Spec, base_qty, contracts_for, round_price
from ict.sizing import position_size

ROOT = Path(__file__).resolve().parents[1]
BTC = Spec("BTC-USDT-SWAP", ct_val=0.01, ct_val_ccy="BTC", lot_sz=0.1, min_sz=0.1, tick_sz=0.1)
ETH = Spec("ETH-USDT-SWAP", ct_val=0.1, ct_val_ccy="ETH", lot_sz=0.01, min_sz=0.01, tick_sz=0.01)


class Config(unittest.TestCase):
    def test_every_key_the_desk_reads_is_top_level(self) -> None:
        """A TOML table swallows every bare key after it — putting [fees]
        anywhere but last silently moved entry_bar inside it."""
        cfg = tomllib.load((ROOT / "config.toml").open("rb"))
        for key in ("instruments", "entry_bar", "htf_bar", "htf_limit", "entry_limit",
                    "min_rr", "session_min_score", "max_consecutive_losses",
                    "risk_pct", "default_equity_usdt", "loop_minutes", "min_rr_on_net"):
            self.assertIn(key, cfg, f"{key} fell into a table")

    def test_the_desk_trades_perpetuals(self) -> None:
        cfg = tomllib.load((ROOT / "config.toml").open("rb"))
        for inst in cfg["instruments"]:
            self.assertTrue(inst.endswith("-SWAP"), inst)

    def test_fee_rates_come_from_config(self) -> None:
        cfg = tomllib.load((ROOT / "config.toml").open("rb"))
        f = Fees.from_config(cfg)
        self.assertAlmostEqual(f.taker, 0.0005)
        self.assertAlmostEqual(f.maker, 0.0002)

    def test_missing_config_falls_back_to_okx_lv1(self) -> None:
        f = Fees.from_config({})
        self.assertAlmostEqual(f.taker, 0.0005)


class Rounding(unittest.TestCase):
    def test_contracts_round_down_never_up(self) -> None:
        # Rounding up would risk more than the desk decided to risk.
        self.assertAlmostEqual(contracts_for(0.10091, BTC), 10.0)
        self.assertAlmostEqual(contracts_for(0.1844, BTC), 18.4)
        self.assertAlmostEqual(base_qty(10.0, BTC), 0.1)

    def test_below_minimum_is_no_position(self) -> None:
        tiny = Spec("X-SWAP", ct_val=1.0, ct_val_ccy="X", lot_sz=1.0, min_sz=1.0, tick_sz=0.1)
        self.assertEqual(contracts_for(0.4, tiny), 0.0)

    def test_prices_land_on_the_tick(self) -> None:
        self.assertAlmostEqual(round_price(79414.512, BTC), 79414.5)
        self.assertAlmostEqual(round_price(79414.58, BTC, "down"), 79414.5)
        self.assertAlmostEqual(round_price(79414.51, BTC, "up"), 79414.6)
        self.assertAlmostEqual(round_price(2445.517, ETH), 2445.52)

    def test_sizing_reports_intended_and_actual_risk(self) -> None:
        s = position_size(78919.0, 79414.51, equity=10000, risk_pct=0.5, spec=BTC)
        self.assertEqual(s["risk_usd"], 50.0)
        self.assertAlmostEqual(s["contracts"], 10.0)
        self.assertAlmostEqual(s["qty"], 0.1)
        # rounding down means slightly less at risk than intended, never more
        self.assertLess(s["risk_usd_actual"], s["risk_usd"])
        self.assertAlmostEqual(s["risk_usd_actual"], 0.1 * abs(78919.0 - 79414.51))

    def test_sizing_without_a_spec_is_unchanged(self) -> None:
        s = position_size(100.0, 90.0, equity=10000, risk_pct=0.5)
        self.assertAlmostEqual(s["qty"], 5.0)
        self.assertAlmostEqual(s["risk_usd_actual"], s["risk_usd"])
        self.assertNotIn("contracts", s)


class FeeMath(unittest.TestCase):
    def test_round_trip_charges_each_execution_on_its_own_notional(self) -> None:
        fee = round_trip(entry=78919.0, exit_px=77729.1, qty=0.10091, rate=0.0005)
        expected = execution_fee(0.10091 * 78919.0, 0.0005) + execution_fee(0.10091 * 77729.1, 0.0005)
        self.assertAlmostEqual(fee, expected)
        self.assertAlmostEqual(fee, 7.90, places=1)

    def test_cost_in_r_depends_only_on_stop_tightness(self) -> None:
        # Same stop percentage, wildly different prices -> same cost in R.
        a = fee_in_r(entry=80000.0, stop=80000.0 * 0.995, rate=0.0005)
        b = fee_in_r(entry=2500.0, stop=2500.0 * 0.995, rate=0.0005)
        self.assertAlmostEqual(a, b, places=9)
        self.assertAlmostEqual(a, 0.2, places=6)

    def test_a_tight_stop_costs_ten_times_more(self) -> None:
        tight = fee_in_r(78919.0, 78919.0 * 0.9985, 0.0005)   # 0.15%
        wide = fee_in_r(78919.0, 78919.0 * 0.985, 0.0005)     # 1.5%
        self.assertAlmostEqual(tight / wide, 10.0, places=6)
        self.assertAlmostEqual(tight, 0.667, places=3)

    def test_net_rr_is_below_gross(self) -> None:
        entry, stop = 78919.0, 78919.0 * 0.9985
        target = entry + 2 * (entry - stop)          # exactly 2.0 gross
        self.assertAlmostEqual(net_rr(entry, stop, target, 0.0005), 1.333, places=3)
        self.assertAlmostEqual(net_rr(entry, stop, target, 0.0), 2.0, places=6)


class SpecCache(unittest.TestCase):
    def test_round_trip_through_the_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "instruments.json"
            instruments.save({"BTC-USDT-SWAP": BTC}, path)
            back = instruments.load(path)
            self.assertEqual(back["BTC-USDT-SWAP"], BTC)

    def test_specs_are_parsed_from_the_okx_payload(self) -> None:
        payload = {"data": [{"instId": "BTC-USDT-SWAP", "ctVal": "0.01", "ctValCcy": "BTC",
                             "lotSz": "0.1", "minSz": "0.1", "tickSz": "0.1"},
                            {"instId": "BROKEN", "ctVal": "nope"}]}
        with patch("ict.instruments._get", return_value=payload):
            specs = instruments.fetch_specs("SWAP")
        self.assertEqual(specs["BTC-USDT-SWAP"].ct_val, 0.01)
        self.assertNotIn("BROKEN", specs)   # a malformed row is skipped, not guessed

    def test_a_missing_spec_raises_instead_of_guessing(self) -> None:
        """A wrong lot size silently changes every size in the record."""
        instruments.clear_memo()
        with patch.object(instruments, "load", return_value={}), \
             patch.object(instruments, "fetch_specs", return_value={}):
            with self.assertRaises(RuntimeError) as ctx:
                instruments.spec("BTC-USDT-SWAP")
        self.assertIn("instruments", str(ctx.exception))


class DeskIntegration(unittest.TestCase):
    def test_a_fill_records_fees_and_net_rr(self) -> None:
        import paper
        from ict import journal
        from ict.model import Check, Fiche

        with TemporaryDirectory() as tmp:
            with patch.object(journal, "JOURNAL_DIR", Path(tmp)):
                journal.invalidate_cache()
                fiche = Fiche(inst_id="BTC-USDT-SWAP", last=78919.0, bias="long")
                fiche.checks.append(Check("all", True, "stub"))
                fiche.entry, fiche.stop, fiche.target = 78919.0, 78423.49, 79910.02
                fiche.rr = 2.0
                cfg = {"risk_pct": 0.5, "default_equity_usdt": 10000,
                       "max_consecutive_losses": 5, "fees": {"taker_pct": 0.05}}
                with patch.object(paper, "instrument_spec", return_value=BTC):
                    paper.maybe_fill(fiche, cfg, "ict")
                pos = journal.load_open()["BTC-USDT-SWAP"]

        self.assertTrue(pos["sized_with_spec"])
        self.assertIn("contracts", pos)
        self.assertEqual(pos["fee_rate"], 0.0005)
        self.assertGreater(pos["fee_r_est"], 0)
        self.assertLess(pos["rr_net"], pos["rr"])          # fees cost R:R
        self.assertEqual(pos["stop"], round_price(pos["stop"], BTC, "down"))


if __name__ == "__main__":
    unittest.main()
