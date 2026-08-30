from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_DIR = ROOT / "journal"
RUNS = JOURNAL_DIR / "runs.jsonl"
OPENS = JOURNAL_DIR / "open.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append(event: dict[str, Any]) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    event = {"logged_at": _now(), **event}
    with RUNS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_open() -> dict[str, Any]:
    if not OPENS.exists():
        return {}
    return json.loads(OPENS.read_text(encoding="utf-8"))


def save_open(state: dict[str, Any]) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    OPENS.write_text(json.dumps(state, indent=2), encoding="utf-8")


def consecutive_losses(inst_id: str) -> int:
    if not RUNS.exists():
        return 0
    streak = 0
    lines = RUNS.read_text(encoding="utf-8").strip().splitlines()
    for line in reversed(lines):
        event = json.loads(line)
        if event.get("inst_id") != inst_id:
            continue
        if event.get("type") != "paper_close":
            continue
        if event.get("result") == "loss":
            streak += 1
            continue
        if event.get("result") == "win":
            break
    return streak


def stats() -> dict[str, Any]:
    if not RUNS.exists():
        return {"fills": 0, "wins": 0, "losses": 0, "stand_downs": 0, "win_rate": None, "avg_r": None}
    fills = wins = losses = stand = 0
    r_sum = 0.0
    r_n = 0
    for line in RUNS.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        kind = event.get("type")
        if kind == "stand_down":
            stand += 1
        elif kind == "paper_fill":
            fills += 1
        elif kind == "paper_close":
            if event.get("result") == "win":
                wins += 1
            elif event.get("result") == "loss":
                losses += 1
            if event.get("r") is not None:
                r_sum += float(event["r"])
                r_n += 1
    closed = wins + losses
    return {
        "fills": fills,
        "wins": wins,
        "losses": losses,
        "stand_downs": stand,
        "win_rate": (wins / closed) if closed else None,
        "avg_r": (r_sum / r_n) if r_n else None,
        "open": load_open(),
    }
