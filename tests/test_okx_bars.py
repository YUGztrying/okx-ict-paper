from __future__ import annotations

import unittest

from ict.okx_data import Candle, closed_candle, seconds_until_bar_close


def _c(ts: int) -> Candle:
    return Candle(ts=ts, open=1, high=2, low=0, close=1.5, volume=10)


class ClosedBar(unittest.TestCase):
    def test_skips_forming_candle(self) -> None:
        # 15:00 bar still open at 15:07
        now = 15 * 3600 * 1000 + 7 * 60 * 1000
        candles = [_c(14 * 3600 * 1000 + 45 * 60 * 1000), _c(15 * 3600 * 1000)]
        closed = closed_candle(candles, "15m", now_ms=now)
        self.assertIsNotNone(closed)
        self.assertEqual(closed.ts, 14 * 3600 * 1000 + 45 * 60 * 1000)

    def test_takes_bar_once_its_window_elapsed(self) -> None:
        now = 15 * 3600 * 1000 + 15 * 60 * 1000
        candles = [_c(15 * 3600 * 1000)]
        closed = closed_candle(candles, "15m", now_ms=now)
        self.assertEqual(closed.ts, 15 * 3600 * 1000)

    def test_seconds_until_next_15m_close(self) -> None:
        # 15:07:00 UTC on a unix-aligned clock: 8 minutes left
        now = 15 * 3600 + 7 * 60
        self.assertAlmostEqual(seconds_until_bar_close("15m", now=now), 8 * 60)


if __name__ == "__main__":
    unittest.main()
