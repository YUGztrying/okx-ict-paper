"""One book, tagged by strategy — and one slot per coin.

Two journals meant two blotters and no answer to "what did the desk do today".
Merging them raises a question the split never had to answer: when ICT and Fabio
both fire on BTC in the same 15m bar, only one of them can have the position.
The rule pinned here is best reward-to-risk AFTER fees, and the loser is written
down as `crowded_out` — otherwise the cost of sharing a book is invisible.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paper
from ict import journal
from ict.instruments import Spec
from ict.model import Check, Fiche

BTC = Spec("BTC-USDT-SWAP", ct_val=0.01, ct_val_ccy="BTC", lot_sz=0.1, min_sz=0.1, tick_sz=0.1)
CFG = {
    "risk_pct": 0.5,
    "default_equity_usdt": 10000,
    "max_consecutive_losses": 5,
    "one_position_per_asset": True,
    "fees": {"taker_pct": 0.05},
}


def setup(inst: str, rr: float, *, entry: float = 79000.0, stop_pct: float = 0.01) -> Fiche:
    """A passing long. `stop_pct` is what decides the fee cost in R, so it is
    what makes gross and net R:R disagree."""
    fiche = Fiche(inst_id=inst, last=entry, bias="long")
    fiche.checks.append(Check("all", True, "stub"))
    risk = entry * stop_pct
    fiche.entry, fiche.stop, fiche.target = entry, entry - risk, entry + rr * risk
    fiche.rr = rr
    return fiche


class Book(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patcher = patch.object(journal, "JOURNAL_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        journal.invalidate_cache()
        spec = patch.object(paper, "instrument_spec", return_value=BTC)
        spec.start()
        self.addCleanup(spec.stop)

    def events(self, kind: str) -> list[dict]:
        return [e for e in journal._parse_lines() if e.get("type") == kind]


class OneLedger(unittest.TestCase):
    def test_both_strategies_write_the_same_file(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(journal, "JOURNAL_DIR", Path(tmp)):
                journal.invalidate_cache()
                journal.append({"type": "paper_fill", "inst_id": "BTC-USDT-SWAP"}, strategy="ict")
                journal.append({"type": "paper_fill", "inst_id": "ETH-USDT-SWAP"}, strategy="fabio")
                self.assertEqual(len(list(Path(tmp).glob("**/runs.jsonl"))), 1)
                self.assertEqual(journal.stats()["fills"], 2)
                self.assertEqual(journal.stats("ict")["fills"], 1)
                self.assertEqual(journal.stats("fabio")["fills"], 1)

    def test_a_line_older_than_the_strategy_tag_still_belongs_to_ict(self) -> None:
        """22 lines in the live journal predate the tag. They came from the
        ICT-only file, which is what append() has always defaulted to."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(journal, "JOURNAL_DIR", root):
                journal.invalidate_cache()
                root.mkdir(parents=True, exist_ok=True)
                (root / "runs.jsonl").write_text(
                    json.dumps({"logged_at": "2026-01-01T10:00:00",
                                "type": "stand_down", "inst_id": "BTC-USDT",
                                "missing": ["amd"]}) + "\n", encoding="utf-8")
                self.assertEqual(journal.stats("ict")["stand_downs"], 1)
                self.assertEqual(journal.stats("fabio")["stand_downs"], 0)
                self.assertEqual(journal.stats()["stand_downs"], 1)

    def test_payload_carries_the_book_and_its_two_views(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(journal, "JOURNAL_DIR", Path(tmp)):
                journal.invalidate_cache()
                journal.append({"type": "paper_fill", "inst_id": "BTC-USDT-SWAP"}, strategy="fabio")
                journal.save_open({"BTC-USDT-SWAP": {"entry": 1, "stop": 0.5, "strategy": "fabio"}})
                books = journal.desk_payload()["books"]
        self.assertEqual(books["desk"]["stats"]["fills"], 1)
        self.assertEqual(books["ict"]["stats"]["fills"], 0)
        self.assertEqual(books["fabio"]["stats"]["fills"], 1)
        # A position belongs to the strategy that opened it, and to the desk.
        self.assertIn("BTC-USDT-SWAP", books["desk"]["open"])
        self.assertIn("BTC-USDT-SWAP", books["fabio"]["open"])
        self.assertEqual(books["ict"]["open"], {})


class Underlying(unittest.TestCase):
    def test_perp_and_spot_are_the_same_coin(self) -> None:
        self.assertEqual(paper.underlying("BTC-USDT-SWAP"), "BTC")
        self.assertEqual(paper.underlying("BTC-USDT"), "BTC")
        self.assertNotEqual(paper.underlying("ETH-USDT-SWAP"), "BTC")


class Collision(Book):
    def test_best_net_rr_takes_the_slot(self) -> None:
        # Fabio is wider gross but its stop is tight enough that fees eat more
        # of it than they eat of ICT's. Net is what the account collects.
        ict = setup("BTC-USDT-SWAP", 2.0, stop_pct=0.010)
        fabio = setup("BTC-USDT-SWAP", 2.2, stop_pct=0.002)
        paper.arbitrate([("ict", ict), ("fabio", fabio)], CFG)

        fills = self.events("paper_fill")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["strategy"], "ict")
        self.assertEqual(list(journal.load_open()), ["BTC-USDT-SWAP"])
        self.assertEqual(journal.load_open()["BTC-USDT-SWAP"]["strategy"], "ict")

    def test_the_loser_is_written_down_not_dropped(self) -> None:
        paper.arbitrate(
            [("ict", setup("BTC-USDT-SWAP", 2.0, stop_pct=0.010)),
             ("fabio", setup("BTC-USDT-SWAP", 2.2, stop_pct=0.002))],
            CFG,
        )
        crowded = [e for e in self.events("stand_down") if "crowded_out" in e["missing"]]
        self.assertEqual(len(crowded), 1)
        self.assertEqual(crowded[0]["strategy"], "fabio")
        self.assertIn("crowded out by ict", " ".join(crowded[0]["reasons"]))
        self.assertEqual(journal.stats("fabio")["crowded_out"], 1)

    def test_a_gross_rr_win_can_lose_on_net(self) -> None:
        """The whole point of ranking on net: gross would have picked fabio."""
        ict = setup("BTC-USDT-SWAP", 2.0, stop_pct=0.010)
        fabio = setup("BTC-USDT-SWAP", 2.2, stop_pct=0.002)
        self.assertGreater(fabio.rr, ict.rr)
        a = paper.build_position(ict, CFG, "ict")
        b = paper.build_position(fabio, CFG, "fabio")
        self.assertGreater(a["rr_net"], b["rr_net"])

    def test_one_signal_is_not_crowded_out_by_itself(self) -> None:
        paper.arbitrate([("ict", setup("BTC-USDT-SWAP", 2.0))], CFG)
        self.assertEqual(len(self.events("paper_fill")), 1)
        self.assertEqual([e for e in self.events("stand_down") if "crowded_out" in e["missing"]], [])

    def test_a_vetoed_signal_never_crowds_anyone_out(self) -> None:
        vetoed = Fiche(inst_id="BTC-USDT-SWAP", last=79000.0, bias="unclear")
        vetoed.checks.append(Check("htf_bias", False, "stub"))
        paper.arbitrate([("ict", vetoed), ("fabio", setup("BTC-USDT-SWAP", 2.2))], CFG)
        fills = self.events("paper_fill")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["strategy"], "fabio")
        self.assertEqual([e for e in self.events("stand_down") if "crowded_out" in e["missing"]], [])

    def test_a_held_coin_blocks_both_strategies(self) -> None:
        journal.save_open({"BTC-USDT-SWAP": {"entry": 1, "stop": 0.5, "strategy": "ict"}})
        paper.arbitrate(
            [("ict", setup("BTC-USDT-SWAP", 2.0)), ("fabio", setup("BTC-USDT-SWAP", 2.2))], CFG
        )
        self.assertEqual(self.events("paper_fill"), [])
        self.assertEqual(
            {e["strategy"] for e in self.events("stand_down") if "already_open" in e["missing"]},
            {"ict", "fabio"},
        )

    def test_the_same_coin_on_another_instrument_still_blocks(self) -> None:
        """A leftover spot BTC position and a new BTC perp are one bet, twice."""
        journal.save_open({"BTC-USDT": {"entry": 1, "stop": 0.5, "strategy": "fabio"}})
        paper.arbitrate([("ict", setup("BTC-USDT-SWAP", 2.0))], CFG)
        self.assertEqual(self.events("paper_fill"), [])

    def test_a_different_coin_is_untouched(self) -> None:
        journal.save_open({"BTC-USDT-SWAP": {"entry": 1, "stop": 0.5, "strategy": "ict"}})
        paper.arbitrate([("fabio", setup("ETH-USDT-SWAP", 2.0, entry=2500.0))], CFG)
        self.assertEqual(len(self.events("paper_fill")), 1)


class Migration(unittest.TestCase):
    def test_the_old_second_journal_is_folded_in_by_time(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(journal, "JOURNAL_DIR", root):
                journal.invalidate_cache()
                (root).mkdir(parents=True, exist_ok=True)
                (root / "runs.jsonl").write_text(
                    json.dumps({"logged_at": "2026-01-01T10:00:00", "type": "paper_fill",
                                "inst_id": "BTC", "strategy": "ict"}) + "\n", encoding="utf-8")
                fabio = root / "fabio"
                fabio.mkdir()
                (fabio / "runs.jsonl").write_text(
                    json.dumps({"logged_at": "2026-01-01T09:00:00", "type": "paper_fill",
                                "inst_id": "ETH"}) + "\n", encoding="utf-8")
                (fabio / "open.json").write_text(
                    json.dumps({"ETH-USDT": {"entry": 1, "stop": 0.5}}), encoding="utf-8")

                moved = journal.absorb_legacy()
                events = journal._parse_lines()
                opened = journal.load_open()

                self.assertEqual(moved, 1)
                self.assertFalse(fabio.exists())
                # oldest first, and the folded rows carry their strategy tag
                self.assertEqual([e["inst_id"] for e in events], ["ETH", "BTC"])
                self.assertEqual(events[0]["strategy"], "fabio")
                # a position still being carried is not dropped on the floor
                self.assertEqual(opened["ETH-USDT"]["strategy"], "fabio")
                # second run is a no-op, not a duplicate
                self.assertEqual(journal.absorb_legacy(), 0)
                self.assertEqual(len(journal._parse_lines()), 2)


if __name__ == "__main__":
    unittest.main()
