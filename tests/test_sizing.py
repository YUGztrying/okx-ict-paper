from __future__ import annotations

import unittest

from ict.sizing import attach_size, position_size, unrealized


class PositionSize(unittest.TestCase):
    def test_half_percent_of_10k_is_50_risk(self) -> None:
        sz = position_size(2440.78, 2432.33558, equity=10000, risk_pct=0.5)
        self.assertAlmostEqual(sz["risk_usd"], 50.0)
        self.assertAlmostEqual(sz["stop_dist"], 8.44442, places=5)
        self.assertAlmostEqual(sz["qty"], 50.0 / 8.44442, places=6)
        self.assertAlmostEqual(sz["notional"], sz["qty"] * 2440.78, places=4)

    def test_long_unrealized_tracks_mark(self) -> None:
        qty = 50.0 / 8.44442
        self.assertAlmostEqual(unrealized("long", 2440.78, 2440.78, qty), 0.0, places=6)
        self.assertGreater(unrealized("long", 2440.78, 2445.0, qty), 0)
        self.assertLess(unrealized("long", 2440.78, 2435.0, qty), 0)

    def test_short_unrealized_is_inverse(self) -> None:
        self.assertAlmostEqual(unrealized("short", 100.0, 90.0, 1.0), 10.0)
        self.assertAlmostEqual(unrealized("short", 100.0, 110.0, 1.0), -10.0)

    def test_attach_size_fills_legacy_open_without_qty(self) -> None:
        pos = attach_size({"entry": 100.0, "stop": 99.0, "risk_pct": 0.5})
        self.assertAlmostEqual(pos["risk_usd"], 50.0)
        self.assertAlmostEqual(pos["qty"], 50.0)
        self.assertAlmostEqual(pos["notional"], 5000.0)


if __name__ == "__main__":
    unittest.main()
