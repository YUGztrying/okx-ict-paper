"""Journal payload guardrails.

The blotter computed P&L from a 40-entry feed while its fill counters came from
the whole journal, so stand-downs pushed real trades off the books within hours
and the page reported $0.00 next to "2 fills".
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ict import journal


class JournalPayload(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(journal, "JOURNAL_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        journal.invalidate_cache()

    def _close(self, result: str, r: float, inst: str = "BTC-USDT") -> None:
        journal.append({"type": "paper_close", "inst_id": inst, "result": result, "r": r}, book="ict")

    def test_trades_survive_a_flood_of_stand_downs(self) -> None:
        journal.append({"type": "paper_fill", "inst_id": "BTC-USDT"}, book="ict")
        self._close("win", 2.0)
        for _ in range(80):
            journal.append({"type": "stand_down", "inst_id": "BTC-USDT", "missing": ["amd"]}, book="ict")

        snap = journal.snapshot(book="ict")
        self.assertEqual(len(snap["feed"]), 40)
        self.assertEqual([e["type"] for e in snap["feed"]], ["stand_down"] * 40)
        # The trade history is not display-capped, so P&L stays computable.
        self.assertEqual([e["type"] for e in snap["trades"]], ["paper_fill", "paper_close"])
        self.assertEqual(snap["stats"]["fills"], 1)
        self.assertEqual(snap["stats"]["wins"], 1)

    def test_trades_are_oldest_first_for_the_equity_curve(self) -> None:
        self._close("win", 1.0)
        self._close("loss", -1.0)
        results = [e["result"] for e in journal.snapshot(book="ict")["trades"]]
        self.assertEqual(results, ["win", "loss"])

    def test_errors_are_counted_and_surfaced(self) -> None:
        journal.append({"type": "error", "inst_id": "ETH-USDT", "error": "boom"}, book="ict")
        snap = journal.snapshot(book="ict")
        self.assertEqual(snap["stats"]["errors"], 1)
        self.assertEqual(snap["last_error"]["error"], "boom")

    def test_stats_do_not_carry_a_second_copy_of_open(self) -> None:
        journal.save_open({"BTC-USDT": {"entry": 1, "stop": 0.5}}, book="ict")
        snap = journal.snapshot(book="ict")
        self.assertNotIn("open", snap["stats"])
        self.assertIn("BTC-USDT", snap["open"])

    def test_desk_payload_has_no_duplicated_top_level_book(self) -> None:
        journal.append({"type": "paper_fill", "inst_id": "BTC-USDT"}, book="ict")
        payload = journal.desk_payload()
        self.assertEqual(set(payload), {"mode", "generated_at", "books"})
        self.assertEqual(set(payload["books"]), {"ict", "fabio"})
        for dead in ("feed", "stats", "latest"):
            self.assertNotIn(dead, payload)
        self.assertNotIn("latest", payload["books"]["ict"])

    def test_consecutive_losses_stops_at_the_last_win(self) -> None:
        self._close("loss", -1.0)
        self._close("win", 2.0)
        self._close("loss", -1.0)
        self._close("loss", -1.0)
        self.assertEqual(journal.consecutive_losses("BTC-USDT", "ict"), 2)
        self.assertEqual(journal.consecutive_losses("ETH-USDT", "ict"), 0)


class IncrementalParse(unittest.TestCase):
    """runs.jsonl is append-only and grows forever; a scan reads the new tail."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(journal, "JOURNAL_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        journal.invalidate_cache()

    def test_appends_are_picked_up(self) -> None:
        journal.append({"type": "stand_down", "inst_id": "BTC-USDT"}, book="ict")
        self.assertEqual(len(journal._parse_lines("ict")), 1)
        journal.append({"type": "stand_down", "inst_id": "ETH-USDT"}, book="ict")
        events = journal._parse_lines("ict")
        self.assertEqual(len(events), 2)
        self.assertEqual([e["inst_id"] for e in events], ["BTC-USDT", "ETH-USDT"])

    def test_only_the_tail_is_read_after_an_append(self) -> None:
        for i in range(5):
            journal.append({"type": "stand_down", "inst_id": f"X{i}"}, book="ict")
        journal._parse_lines("ict")
        with patch.object(journal, "_decode", wraps=journal._decode) as decode:
            journal.append({"type": "stand_down", "inst_id": "TAIL"}, book="ict")
            events = journal._parse_lines("ict")
            blob = decode.call_args[0][0]
        self.assertEqual(len(events), 6)
        self.assertIn("TAIL", blob.decode())
        self.assertNotIn("X0", blob.decode())

    def test_rewritten_file_is_fully_reparsed(self) -> None:
        # A git rebase can rewrite the middle of the journal.
        journal.append({"type": "stand_down", "inst_id": "BTC-USDT"}, book="ict")
        journal._parse_lines("ict")
        runs, _ = journal._paths("ict")
        runs.write_text("", encoding="utf-8")
        journal.invalidate_cache()
        self.assertEqual(journal._parse_lines("ict"), [])


if __name__ == "__main__":
    unittest.main()
