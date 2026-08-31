from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ict.sizing import attach_size

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_DIR = ROOT / "journal"
RUNS = JOURNAL_DIR / "runs.jsonl"
OPENS = JOURNAL_DIR / "open.json"

TRADE_TYPES = ("paper_fill", "paper_close")
# The feed is what the blotter lists; trades are what its P&L is computed from.
# Stand-downs would otherwise push real fills out of a capped feed within hours.
TRADE_LIMIT = 500

# book -> {"size": int, "mtime": int, "events": list}. runs.jsonl is append-only
# and this process is its only writer, so a scan reads the new tail, not the
# whole file. Without this every tick reparses a journal that grows forever.
_CACHE: dict[str, dict[str, Any]] = {}


def _paths(book: str) -> tuple[Path, Path]:
    if book == "ict":
        return JOURNAL_DIR / "runs.jsonl", JOURNAL_DIR / "open.json"
    folder = JOURNAL_DIR / book
    return folder / "runs.jsonl", folder / "open.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def invalidate_cache(book: str | None = None) -> None:
    """Drop parsed events. Call when the file changed under us — a git rebase
    can rewrite the middle of the journal, which the tail read cannot see."""
    if book is None:
        _CACHE.clear()
    else:
        _CACHE.pop(book, None)


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


def _decode(blob: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in blob.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _parse_lines(book: str = "ict") -> list[dict[str, Any]]:
    """Every journal event, oldest first. The returned list is shared — read it,
    do not mutate it."""
    runs, _ = _paths(book)
    if not runs.exists():
        invalidate_cache(book)
        return []
    stat = runs.stat()
    cached = _CACHE.get(book)
    if cached and cached["size"] == stat.st_size and cached["mtime"] == stat.st_mtime_ns:
        return cached["events"]
    grew = bool(cached) and stat.st_size > cached["size"]
    offset = cached["size"] if grew else 0
    with runs.open("rb") as fh:
        fh.seek(offset)
        blob = fh.read()
    events = (list(cached["events"]) if grew else []) + _decode(blob)
    _CACHE[book] = {"size": stat.st_size, "mtime": stat.st_mtime_ns, "events": events}
    return events


def consecutive_losses(inst_id: str, book: str = "ict", events: list[dict[str, Any]] | None = None) -> int:
    streak = 0
    for event in reversed(_parse_lines(book) if events is None else events):
        if event.get("inst_id") != inst_id or event.get("type") != "paper_close":
            continue
        if event.get("result") == "loss":
            streak += 1
            continue
        if event.get("result") == "win":
            break
    return streak


def stats_from(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Counters over the whole journal. Open positions live on the snapshot, not
    here — one copy per payload."""
    fills = wins = losses = stand = errors = 0
    r_sum = 0.0
    r_n = 0
    for event in events:
        kind = event.get("type")
        if kind == "stand_down":
            stand += 1
        elif kind == "error":
            errors += 1
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
        "errors": errors,
        "win_rate": (wins / closed) if closed else None,
        "avg_r": (r_sum / r_n) if r_n else None,
    }


def stats(book: str = "ict") -> dict[str, Any]:
    return stats_from(_parse_lines(book))


def snapshot(limit: int = 40, book: str = "ict") -> dict[str, Any]:
    events = _parse_lines(book)
    trades = [e for e in events if e.get("type") in TRADE_TYPES][-TRADE_LIMIT:]
    errors = [e for e in events if e.get("type") == "error"]
    feed = list(reversed(events[-limit:]))
    last_scan = events[-1]["logged_at"] if events else None
    opened = {inst: attach_size(dict(pos)) for inst, pos in load_open(book).items()}
    return {
        "mode": "paper",
        "book": book,
        "generated_at": _now(),
        "last_scan": last_scan,
        "stats": stats_from(events),
        "open": opened,
        "trades": trades,
        "last_error": errors[-1] if errors else None,
        "feed": feed,
    }


def desk_payload() -> dict[str, Any]:
    return {
        "mode": "paper",
        "generated_at": _now(),
        "books": {"ict": snapshot(book="ict"), "fabio": snapshot(book="fabio")},
    }


def write_desk(path: Path | None = None) -> Path:
    dest = path or (ROOT / "dashboard" / "desk.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(desk_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
    return dest
