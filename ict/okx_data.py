from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

OKX = "https://www.okx.com/api/v5"


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


def fetch_last(inst_id: str) -> float:
    payload = _get("/market/ticker", {"instId": inst_id})
    rows = payload.get("data") or []
    if not rows:
        raise RuntimeError(f"no ticker for {inst_id}")
    return float(rows[0]["last"])
