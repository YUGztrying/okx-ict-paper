"""GitHub-side persist + chain. Only one paper-scan should write the journal."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()


def persist_enabled() -> bool:
    return os.environ.get("PAPER_GIT_PUSH") == "1"


def persist_journal(reason: str = "paper scan") -> bool:
    """Commit journal + desk.json. No-op unless PAPER_GIT_PUSH=1.

    Returns True when origin is current (nothing to commit, or push succeeded).
    False if the flag is off or the push failed — caller must not chain yet.
    """
    if not persist_enabled():
        return False
    with _LOCK:
        subprocess.run(["git", "add", "journal", "dashboard/desk.json"], cwd=ROOT, check=False)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=ROOT)
        if diff.returncode == 0:
            return True
        subprocess.run(["git", "commit", "-m", reason], cwd=ROOT, check=False)
        push = subprocess.run(["git", "push"], cwd=ROOT, check=False)
        return push.returncode == 0


def should_dispatch_next(in_progress: int, queued: int) -> bool:
    """This run counts as one in_progress. Don't fork a second chain."""
    return queued == 0 and in_progress <= 1


def _gh_count(status: str) -> int:
    proc = subprocess.run(
        ["gh", "run", "list", "--workflow", "paper.yml", "--status", status, "--json", "databaseId"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return 0
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return 0
    return len(rows) if isinstance(rows, list) else 0


def dispatch_next(ref: str | None = None) -> bool:
    if ref is None:
        ref = os.environ.get("GITHUB_REF_NAME") or "main"
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        return False
    in_progress = _gh_count("in_progress")
    queued = _gh_count("queued")
    if not should_dispatch_next(in_progress, queued):
        print(f"skip chain (in_progress={in_progress} queued={queued})")
        return False
    proc = subprocess.run(
        ["gh", "workflow", "run", "paper.yml", "--ref", ref],
        cwd=ROOT,
        check=False,
    )
    ok = proc.returncode == 0
    print("chained next watch" if ok else "chain dispatch failed")
    return ok
