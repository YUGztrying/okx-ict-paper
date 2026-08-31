from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_DIR = ROOT / "journal"
RUNS = JOURNAL_DIR / "runs.jsonl"
OPENS = JOURNAL_DIR / "open.json"


def _paths(book: str) -> tuple[Path, Path]:
    if book == "ict":
        return JOURNAL_DIR / "runs.jsonl", JOURNAL_DIR / "open.json"
    folder = JOURNAL_DIR / book
    return folder / "runs.jsonl", folder / "open.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append(event: dict[str, Any], book: str = "ict") -> None:
    runs, _ = _paths(book)
    runs.parent.mkdir(parents=True, exist_ok=True)
    event = {"logged_at": _now(), "strategy": book, **event}
    with runs.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_open(book: str = "ict") -> dict[str, Any]:
    _, opens = _paths(book)
    if not opens.exists():
        return {}
    return json.loads(opens.read_text(encoding="utf-8"))


def save_open(state: dict[str, Any], book: str = "ict") -> None:
    runs, opens = _paths(book)
    opens.parent.mkdir(parents=True, exist_ok=True)
    opens.write_text(json.dumps(state, indent=2), encoding="utf-8")


def consecutive_losses(inst_id: str, book: str = "ict") -> int:
    runs, _ = _paths(book)
    if not runs.exists():
        return 0
    streak = 0
    lines = runs.read_text(encoding="utf-8").strip().splitlines()
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


def stats(book: str = "ict") -> dict[str, Any]:
    runs, _ = _paths(book)
    if not runs.exists():
        return {"fills": 0, "wins": 0, "losses": 0, "stand_downs": 0, "win_rate": None, "avg_r": None, "open": {}}
    fills = wins = losses = stand = 0
    r_sum = 0.0
    r_n = 0
    for line in runs.read_text(encoding="utf-8").splitlines():
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
        "open": load_open(book),
    }


def _parse_lines(book: str = "ict") -> list[dict[str, Any]]:
    runs, _ = _paths(book)
    if not runs.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in runs.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def snapshot(limit: int = 40, book: str = "ict") -> dict[str, Any]:
    events = _parse_lines(book)
    latest: dict[str, Any] = {}
    for event in reversed(events):
        inst = event.get("inst_id")
        if inst and inst not in latest and event.get("type") != "error":
            latest[inst] = event
        if len(latest) >= 8:
            break
    feed = list(reversed(events[-limit:]))
    last_scan = events[-1]["logged_at"] if events else None
    return {
        "mode": "paper",
        "book": book,
        "generated_at": _now(),
        "last_scan": last_scan,
        "stats": stats(book),
        "open": load_open(book),
        "latest": latest,
        "feed": feed,
    }


def write_desk(path: Path | None = None) -> Path:
    dest = path or (ROOT / "dashboard" / "desk.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    ict = snapshot(book="ict")
    fabio = snapshot(book="fabio")
    payload = {
        **ict,
        "books": {"ict": ict, "fabio": fabio},
    }
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest
