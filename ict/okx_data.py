from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _okx_cmd() -> str:
    found = shutil.which("okx")
    if found:
        return found
    cmd = Path.home() / "AppData" / "Roaming" / "npm" / "okx.cmd"
    if cmd.exists():
        return str(cmd)
    raise RuntimeError("okx CLI not found. Install @okx_ai/okx-trade-cli and ensure it is on PATH.")


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def fetch_candles(inst_id: str, bar: str, limit: int) -> list[Candle]:
    result = subprocess.run(
        [_okx_cmd(), "market", "candles", inst_id, "--bar", bar, "--limit", str(limit), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"okx candles failed for {inst_id} {bar}: {result.stderr or result.stdout}")
    raw = json.loads(result.stdout)
    candles = [
        Candle(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in raw
    ]
    candles.sort(key=lambda c: c.ts)
    return candles


def fetch_last(inst_id: str) -> float:
    result = subprocess.run(
        [_okx_cmd(), "market", "ticker", inst_id, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"okx ticker failed for {inst_id}: {result.stderr or result.stdout}")
    data = json.loads(result.stdout)
    if isinstance(data, list):
        data = data[0]
    return float(data.get("last") or data.get("lastPx") or data["last"])
