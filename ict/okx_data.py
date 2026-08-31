from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

OKX = "https://www.okx.com/api/v5"
BAR_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
}


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _get(path: str, params: dict[str, str]) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{OKX}{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "okx-ict-paper/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OKX request failed {url}: {exc}") from exc
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX error {payload.get('code')}: {payload.get('msg')} ({url})")
    return payload


def fetch_candles(inst_id: str, bar: str, limit: int) -> list[Candle]:
    payload = _get("/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
    candles = [
        Candle(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in payload.get("data") or []
    ]
    candles.sort(key=lambda c: c.ts)
    return candles


def bar_ms(bar: str) -> int:
    if bar not in BAR_MS:
        raise ValueError(f"unsupported bar {bar}")
    return BAR_MS[bar]


def closed_candle(candles: list[Candle], bar: str, now_ms: int | None = None) -> Candle | None:
    """Newest candle that has fully closed. OKX's latest row is often still forming."""
    width = bar_ms(bar)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    for candle in reversed(candles):
        if candle.ts + width <= now:
            return candle
    return None


def seconds_until_bar_close(bar: str, now: float | None = None) -> float:
    width = bar_ms(bar) / 1000.0
    t = now if now is not None else time.time()
    elapsed = t % width
    remaining = width - elapsed
    return remaining if remaining > 0 else width


def near_bar_boundary(bar: str, now: float | None = None, window: float = 12.0) -> bool:
    """True in the last `window` seconds of a bar, or the first `window` of the next."""
    width = bar_ms(bar) / 1000.0
    left = seconds_until_bar_close(bar, now)
    return left <= window or left >= width - window


def fetch_last(inst_id: str) -> float:
    payload = _get("/market/ticker", {"instId": inst_id})
    rows = payload.get("data") or []
    if not rows:
        raise RuntimeError(f"no ticker for {inst_id}")
    return float(rows[0]["last"])
