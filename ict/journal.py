"""One book. Every line in it says which strategy wrote it.

There used to be two journals — journal/runs.jsonl for ICT and
journal/fabio/runs.jsonl for Fabio — which meant two blotters, two equity
curves, and no way to answer "what did the desk do today". The desk trades one
account, so it keeps one ledger; `strategy` on each event and each position is
what tells ICT from Fabio. Per-strategy views are a filter over that ledger, not
a second copy of it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ict.sizing import attach_size

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_DIR = ROOT / "journal"
STRATEGIES = ("ict", "fabio")
# Where the second book used to live. Absorbed on startup, then removed.
LEGACY_DIRS = ("fabio",)

TRADE_TYPES = ("paper_fill", "paper_close")
# The feed is what the blotter lists; trades are what its P&L is computed from.
# Stand-downs would otherwise push real fills out of a capped feed within hours.
TRADE_LIMIT = 500

# {"size": int, "mtime": int, "events": list}. runs.jsonl is append-only and this
# process is its only writer, so a scan reads the new tail, not the whole file.
# Without this every tick reparses a journal that grows forever.
_CACHE: dict[str, Any] = {}


def runs_path() -> Path:
    return JOURNAL_DIR / "runs.jsonl"


def opens_path() -> Path:
    return JOURNAL_DIR / "open.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def invalidate_cache(*_ignored) -> None:
    """Drop parsed events. Call when the file changed under us — a git rebase
    can rewrite the middle of the journal, which the tail read cannot see."""
    _CACHE.clear()


def append(event: dict[str, Any], strategy: str = "ict") -> None:
    runs = runs_path()
    runs.parent.mkdir(parents=True, exist_ok=True)
    event = {"logged_at": _now(), "strategy": strategy, **event}
    with runs.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_open() -> dict[str, Any]:
    opens = opens_path()
    if not opens.exists():
        return {}
    return json.loads(opens.read_text(encoding="utf-8"))


def save_open(state: dict[str, Any]) -> None:
    opens = opens_path()
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


def _parse_lines() -> list[dict[str, Any]]:
    """Every journal event, oldest first. The returned list is shared — read it,
    do not mutate it."""
    runs = runs_path()
    if not runs.exists():
        invalidate_cache()
        return []
    stat = runs.stat()
    cached = _CACHE.get("book")
    if cached and cached["size"] == stat.st_size and cached["mtime"] == stat.st_mtime_ns:
        return cached["events"]
    grew = bool(cached) and stat.st_size > cached["size"]
    offset = cached["size"] if grew else 0
    with runs.open("rb") as fh:
        fh.seek(offset)
        blob = fh.read()
    events = (list(cached["events"]) if grew else []) + _decode(blob)
    _CACHE["book"] = {"size": stat.st_size, "mtime": stat.st_mtime_ns, "events": events}
    return events


def events_for(strategy: str | None = None) -> list[dict[str, Any]]:
    events = _parse_lines()
    if strategy is None:
        return events
    return [e for e in events if e.get("strategy") == strategy]


def consecutive_losses(inst_id: str, strategy: str | None = None,
                       events: list[dict[str, Any]] | None = None) -> int:
    """Losing streak on one instrument. Scoped to a strategy when given: ICT
    failing on BTC says nothing about Fabio's next BTC setup."""
    streak = 0
    source = _parse_lines() if events is None else events
    for event in reversed(source):
        if event.get("inst_id") != inst_id or event.get("type") != "paper_close":
            continue
        if strategy is not None and event.get("strategy") != strategy:
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
    fills = wins = losses = stand = errors = crowded = 0
    r_sum = 0.0
    r_n = 0
    for event in events:
        kind = event.get("type")
        if kind == "stand_down":
            stand += 1
            if "crowded_out" in (event.get("missing") or []):
                crowded += 1
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
        "crowded_out": crowded,
        "errors": errors,
        "win_rate": (wins / closed) if closed else None,
        "avg_r": (r_sum / r_n) if r_n else None,
    }


def stats(strategy: str | None = None) -> dict[str, Any]:
    return stats_from(events_for(strategy))


def snapshot(limit: int = 40, strategy: str | None = None) -> dict[str, Any]:
    events = events_for(strategy)
    trades = [e for e in events if e.get("type") in TRADE_TYPES][-TRADE_LIMIT:]
    errors = [e for e in events if e.get("type") == "error"]
    feed = list(reversed(events[-limit:]))
    last_scan = events[-1]["logged_at"] if events else None
    opened = {
        inst: attach_size(dict(pos))
        for inst, pos in load_open().items()
        if strategy is None or pos.get("strategy") == strategy
    }
    return {
        "mode": "paper",
        "book": strategy or "desk",
        "generated_at": _now(),
        "last_scan": last_scan,
        "stats": stats_from(events),
        "open": opened,
        "trades": trades,
        "last_error": errors[-1] if errors else None,
        "feed": feed,
    }


def desk_payload() -> dict[str, Any]:
    """One book, plus the per-strategy views the blotter filters on. The views
    are slices of the same ledger — nothing is counted twice."""
    books = {"desk": snapshot()}
    for name in STRATEGIES:
        books[name] = snapshot(strategy=name)
    return {"mode": "paper", "generated_at": _now(), "books": books}


def absorb_legacy() -> int:
    """Fold an old per-strategy journal into the single book, once.

    The live runner can still be executing the two-book code when this lands, so
    this is not a one-shot migration: whatever that runner appended to
    journal/fabio/ is absorbed the next time the new code starts, in timestamp
    order, and the directory is removed. Returns the number of events moved.
    """
    moved = 0
    for name in LEGACY_DIRS:
        folder = JOURNAL_DIR / name
        legacy_runs, legacy_open = folder / "runs.jsonl", folder / "open.json"
        if not folder.exists():
            continue
        if legacy_runs.exists():
            incoming = [
                {**e, "strategy": e.get("strategy") or name}
                for e in _decode(legacy_runs.read_bytes())
            ]
            if incoming:
                merged = sorted(_parse_lines() + incoming, key=lambda e: e.get("logged_at") or "")
                runs_path().parent.mkdir(parents=True, exist_ok=True)
                runs_path().write_text(
                    "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in merged),
                    encoding="utf-8",
                )
                invalidate_cache()
                moved += len(incoming)
            legacy_runs.unlink()
        if legacy_open.exists():
            state = load_open()
            for inst, pos in json.loads(legacy_open.read_text(encoding="utf-8")).items():
                # A position the desk is still carrying must not be dropped on
                # the floor: it needs marking until it hits stop or target.
                if inst not in state:
                    state[inst] = {**pos, "strategy": pos.get("strategy") or name}
            save_open(state)
            legacy_open.unlink()
        for leftover in folder.iterdir():
            leftover.unlink()
        folder.rmdir()
    return moved


def write_desk(path: Path | None = None) -> Path:
    dest = path or (ROOT / "dashboard" / "desk.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(desk_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
    return dest
