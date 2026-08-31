"""Guardrails for the static blotter. The header used class `bar`, same as
the 8px profit-bar, which crushed the title row to 8px on every screen."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")


class BlotterMarkup(unittest.TestCase):
    def test_header_is_not_the_8px_bar_class(self) -> None:
        self.assertIn('<header class="topbar">', HTML)
        self.assertNotIn('<header class="bar">', HTML)
        self.assertRegex(HTML, r"\.bar\s*\{[^}]*height:\s*8px")

    def test_phone_frame_is_centered(self) -> None:
        self.assertIn("max-width: 430px", HTML)
        self.assertIn("margin: 0 auto", HTML)
        self.assertIn('<div class="phone">', HTML)
        self.assertGreaterEqual(HTML.count("</div>"), 1)

    def test_vetos_are_not_sold_as_buys_or_recent_fills(self) -> None:
        self.assertIn('stand_down" ? "NO TRADE"', HTML)
        self.assertIn(".side.none", HTML)
        self.assertIn('e.type === "paper_close" || e.type === "paper_fill"', HTML)
        self.assertNotIn("const fallback", HTML)

    def test_books_live_on_their_own_row(self) -> None:
        header, _, rest = HTML.partition('<header class="topbar">')
        self.assertTrue(rest)
        block = rest.split("</header>", 1)[0]
        self.assertNotIn('data-book="ict"', block)
        self.assertIn('id="books"', rest.split("</header>", 1)[1][:400])

    def test_open_size_and_live_mark_hooks(self) -> None:
        self.assertIn('id="opens"', HTML)
        self.assertIn("wss://ws.okx.com:8443/ws/v5/public", HTML)
        self.assertIn("/api/tickers?instIds=", HTML)
        self.assertIn("data-live-pnl", HTML)
        self.assertIn("Notional", HTML)


if __name__ == "__main__":
    unittest.main()
