"""The blotter redraw.

Pages only rebuilt when the 5h20 watch job ended, so the phone could show a
position that closed hours ago — and the page live-marks open positions against
OKX, so a stale row looked like a live one. The desk now dispatches a redraw
after every scan and every close.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from ict.cloud import publish_dashboard

ROOT = Path(__file__).resolve().parents[1]
PAPER = (ROOT / ".github/workflows/paper.yml").read_text(encoding="utf-8")
PUBLISH = (ROOT / ".github/workflows/publish-dashboard.yml").read_text(encoding="utf-8")


class Dispatch(unittest.TestCase):
    def test_no_token_is_a_quiet_no_op(self) -> None:
        with patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}, clear=False):
            self.assertFalse(publish_dashboard())

    def test_missing_gh_never_raises_into_the_desk_loop(self) -> None:
        # tick() calls this straight after persisting; an exception here would
        # unwind the watch loop and stop the desk.
        with patch.dict(os.environ, {"GH_TOKEN": "x"}, clear=False), \
             patch("ict.cloud.subprocess.run", side_effect=OSError("no gh")):
            self.assertFalse(publish_dashboard())

    def test_dispatch_failure_is_reported_not_raised(self) -> None:
        class Proc:
            returncode = 1
            stderr = "boom"

        with patch.dict(os.environ, {"GH_TOKEN": "x"}, clear=False), \
             patch("ict.cloud.subprocess.run", return_value=Proc()):
            self.assertFalse(publish_dashboard())

    def test_dispatches_the_publisher_on_the_running_branch(self) -> None:
        seen = {}

        class Proc:
            returncode = 0
            stderr = ""

        def fake(args, **kw):
            seen["args"] = args
            return Proc()

        with patch.dict(os.environ, {"GH_TOKEN": "x", "GITHUB_REF_NAME": "main"}, clear=False), \
             patch("ict.cloud.subprocess.run", fake):
            self.assertTrue(publish_dashboard())
        self.assertEqual(seen["args"][:3], ["gh", "workflow", "run"])
        self.assertIn("publish-dashboard.yml", seen["args"])
        self.assertIn("main", seen["args"])


class Workflows(unittest.TestCase):
    def test_one_publisher_owns_pages(self) -> None:
        # Two workflows deploying Pages would race each other.
        self.assertIn("deploy-pages", PUBLISH)
        self.assertNotIn("deploy-pages", PAPER)
        self.assertNotIn("upload-pages-artifact", PAPER)

    def test_publisher_rebuilds_desk_json_from_the_journal(self) -> None:
        # desk.json is untracked, so the artifact needs it built at publish time.
        self.assertIn("write_desk", PUBLISH)
        self.assertIn("path: dashboard", PUBLISH)

    def test_publisher_only_reads_the_repo(self) -> None:
        # It must never commit: the desk is the single writer of the journal.
        self.assertIn("contents: read", PUBLISH)
        self.assertNotIn("contents: write", PUBLISH)

    def test_publisher_is_dispatchable_with_a_cron_backup(self) -> None:
        # A GITHUB_TOKEN push does not retrigger `on: push`, and GitHub skips
        # cron hours on public repos, so dispatch is the primary path.
        self.assertIn("workflow_dispatch:", PUBLISH)
        self.assertIn("schedule:", PUBLISH)

    def test_pages_deployments_are_serialised(self) -> None:
        self.assertIn("group: pages", PUBLISH)
        self.assertIn("cancel-in-progress: false", PUBLISH)

    def test_the_desk_still_owns_the_journal_and_the_chain(self) -> None:
        self.assertIn("persist_journal", PAPER)
        self.assertIn("dispatch_next", PAPER)
        self.assertIn("publish_dashboard", PAPER)


if __name__ == "__main__":
    unittest.main()
