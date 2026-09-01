"""Historical candles on disk.

OKX's /market/candles only reaches back a few hundred bars, so history is
walked backwards through /market/history-candles and cached. Both endpoints
are public: no API key, no account, nothing secret to hand around.

Cache format is one compact JSON row per line, oldest first:
    [ts_ms, open, high, low, close, volume]
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ict.okx_data import Candle, bar_ms, fetch_history_candles

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PAGE = 100  # history-candles caps a page at 100


def cache_path(inst_id: str, bar: str) -> Path:
    return DATA_DIR / f"{inst_id}-{bar}.jsonl"


def load(inst_id: str, bar: str, path: Path | None = None) -> list[Candle]:
    path = path or cache_path(inst_id, bar)
    if not path.exists():
        return []
    out: list[Candle] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        ts, o, h, low, c, v = json.loads(line)
        out.append(Candle(ts=int(ts), open=o, high=h, low=low, close=c, volume=v))
    out.sort(key=lambda c: c.ts)
    return out


def save(candles: list[Candle], inst_id: str, bar: str, path: Path | None = None) -> Path:
    path = path or cache_path(inst_id, bar)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(dedupe(candles), key=lambda c: c.ts)
    path.write_text(
        "".join(json.dumps([c.ts, c.open, c.high, c.low, c.close, c.volume]) + "\n" for c in rows),
        encoding="utf-8",
    )
    return path


def dedupe(candles: list[Candle]) -> list[Candle]:
    """One candle per timestamp. Pages overlap at their boundary."""
    by_ts: dict[int, Candle] = {}
    for c in candles:
        by_ts[c.ts] = c
    return [by_ts[ts] for ts in sorted(by_ts)]


def fetch(
    inst_id: str,
    bar: str,
    days: float,
    *,
    now_ms: int | None = None,
    fetcher=fetch_history_candles,
    pause: float = 0.15,
    max_pages: int = 500,
) -> list[Candle]:
    """Walk backwards until `days` of history is covered.

    Stops on an empty page (history exhausted) or when a page fails to move the
    cursor, so a misbehaving endpoint cannot spin forever.
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    floor_ts = now - int(days * 86_400_000)
    collected: list[Candle] = []
    cursor: int | None = None
    for _ in range(max_pages):
        page = fetcher(inst_id, bar, PAGE, cursor)
        if not page:
            break
        collected.extend(page)
        oldest = min(c.ts for c in page)
        if cursor is not None and oldest >= cursor:
            break  # no progress; do not loop on a stuck cursor
        cursor = oldest
        if oldest <= floor_ts:
            break
        if pause:
            time.sleep(pause)
    rows = [c for c in dedupe(collected) if c.ts >= floor_ts]
    return rows


def coverage(candles: list[Candle], bar: str) -> dict:
    """What the cache actually holds — gaps included, since OKX has outages."""
    if not candles:
        return {"bars": 0, "gaps": 0, "missing_bars": 0}
    width = bar_ms(bar)
    gaps = 0
    missing = 0
    for prev, nxt in zip(candles, candles[1:]):
        step = nxt.ts - prev.ts
        if step > width:
            gaps += 1
            missing += step // width - 1
    return {
        "bars": len(candles),
        "first": candles[0].ts,
        "last": candles[-1].ts,
        "gaps": gaps,
        "missing_bars": int(missing),
    }
