"""GitHub-side persist + chain. Only one paper-scan should write the journal."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()


def persist_enabled() -> bool:
    return os.environ.get("PAPER_GIT_PUSH") == "1"


def _branch() -> str:
    ref = os.environ.get("GITHUB_REF_NAME")
    if ref:
        return ref
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    name = (proc.stdout or "").strip()
    return name if name and name != "HEAD" else "main"


def _rebase_on_origin(branch: str) -> bool:
    """Replay our journal commits on top of origin. The runner checks out once
    and watches for hours; a human push to the branch must not strand it."""
    from ict.journal import invalidate_cache

    fetch = subprocess.run(["git", "fetch", "origin", branch], cwd=ROOT, check=False)
    if fetch.returncode != 0:
        return False
    rebase = subprocess.run(["git", "rebase", "FETCH_HEAD"], cwd=ROOT, check=False)
    if rebase.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], cwd=ROOT, check=False)
        print("journal rebase conflicted — left the local commits alone", file=sys.stderr)
        invalidate_cache()
        return False
    # The rebase may have rewritten the middle of runs.jsonl; the tail read
    # in _parse_lines cannot see that.
    invalidate_cache()
    return True


def persist_journal(reason: str = "paper scan", attempts: int = 3) -> bool:
    """Commit and push the journal. No-op unless PAPER_GIT_PUSH=1.

    desk.json is derived from these files and is rebuilt for the Pages
    artifact, so it stays out of git history and out of the merge path.

    Returns True when origin is current (nothing to commit, or push succeeded).
    False if the flag is off or the push failed — caller must not chain yet.
    """
    if not persist_enabled():
        return False
    with _LOCK:
        subprocess.run(["git", "add", "journal"], cwd=ROOT, check=False)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=ROOT)
        if diff.returncode == 0:
            return True
        subprocess.run(["git", "commit", "-m", reason], cwd=ROOT, check=False)
        branch = _branch()
        for attempt in range(1, attempts + 1):
            if subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], cwd=ROOT, check=False).returncode == 0:
                return True
            if attempt == attempts or not _rebase_on_origin(branch):
                break
            time.sleep(2 ** (attempt - 1))
        print(f"journal push failed after {attempts} attempts — fills are only on this runner", file=sys.stderr)
        return False


def publish_dashboard(ref: str | None = None) -> bool:
    """Redraw the Pages blotter now, instead of when this 5h20 job ends.

    The journal is pushed every scan, but paper.yml only reaches its Pages
    steps at the end of the watch. Between those, the phone shows a position
    that may have closed hours ago — and the page live-marks it against OKX,
    so a stale row looks like a live one. A GITHUB_TOKEN push does not
    retrigger `on: push`, so dispatch is the mechanism that works here.

    Best effort: a failed redraw must never disturb the desk.
    """
    if ref is None:
        ref = os.environ.get("GITHUB_REF_NAME") or "main"
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        return False
    try:
        proc = subprocess.run(
            ["gh", "workflow", "run", "publish-dashboard.yml", "--ref", ref],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:  # no gh on this box — a redraw is not worth a crash
        print(f"dashboard redraw unavailable: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"dashboard redraw dispatch failed: {(proc.stderr or '').strip()[:200]}", file=sys.stderr)
        return False
    return True


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
