"""OKX public WebSocket: last prints and confirmed candle closes. No auth, no orders."""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"


@dataclass(frozen=True)
class Tick:
    inst_id: str
    last: float


@dataclass(frozen=True)
class BarClose:
    inst_id: str
    ts: int
    close: float


def candle_channel(bar: str) -> str:
    return "candle" + bar


def parse_message(raw: str) -> list[Tick | BarClose]:
    """Parse one OKX public WS frame. Ignore pings, acks, and forming candles."""
    if raw in {"ping", "pong", ""}:
        return []
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(msg, dict):
        return []
    arg = msg.get("arg") or {}
    channel = str(arg.get("channel") or "")
    rows = msg.get("data") or []
    out: list[Tick | BarClose] = []
    if channel == "tickers":
        for row in rows:
            if not isinstance(row, dict):
                continue
            inst = row.get("instId") or arg.get("instId")
            last = row.get("last")
            if inst and last is not None:
                out.append(Tick(str(inst), float(last)))
        return out
    if channel.startswith("candle") and not channel.endswith("utc"):
        inst = str(arg.get("instId") or "")
        for row in rows:
            if not isinstance(row, list) or len(row) < 9:
                continue
            if str(row[8]) != "1":
                continue
            if not inst:
                continue
            out.append(BarClose(inst, int(row[0]), float(row[4])))
    return out


def is_decision_bar(inst_id: str, ts: int, seen_close: dict[str, int]) -> bool:
    """True when this confirm=1 is a new close, not the seed and not a duplicate.

    The start tick already decided on the current closed bar. The first ts we
    record per instrument is that seed. A later ts is the trader waiting for
    the next close. If REST seed failed, seen_close[inst]=0 so the first real
    confirm=1 still fires (duplicate veto log beats a missed 15m).
    """
    if seen_close.get(inst_id) == ts:
        return False
    if inst_id not in seen_close:
        seen_close[inst_id] = ts
        return False
    seen_close[inst_id] = ts
    return True


def subscribe_payload(instruments: list[str], bar: str) -> dict[str, Any]:
    args = []
    for inst in instruments:
        args.append({"channel": "tickers", "instId": inst})
        args.append({"channel": candle_channel(bar), "instId": inst})
    return {"op": "subscribe", "args": args}


class PublicFeed:
    """Background OKX public socket. Events land on `.events`."""

    def __init__(self, instruments: list[str], bar: str = "15m") -> None:
        self.instruments = list(instruments)
        self.bar = bar
        self.events: queue.Queue[Tick | BarClose] = queue.Queue()
        self.connected = threading.Event()
        self.last_event_at = 0.0
        self._stop = threading.Event()
        self._app = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import websocket

        feed = self

        def on_open(ws) -> None:  # noqa: ANN001
            ws.send(json.dumps(subscribe_payload(feed.instruments, feed.bar)))
            feed.connected.set()

        def on_message(ws, message: str) -> None:  # noqa: ANN001
            if message == "ping":
                ws.send("pong")
                return
            if message == "pong":
                return
            parsed = parse_message(message)
            if parsed:
                feed.last_event_at = time.time()
            for ev in parsed:
                feed.events.put(ev)

        def on_error(_ws, error: object) -> None:
            print(f"WS error: {error}", file=sys.stderr)

        def on_close(*_args: object) -> None:
            feed.connected.clear()

        def runner() -> None:
            while not feed._stop.is_set():
                feed._app = websocket.WebSocketApp(
                    WS_URL,
                    on_open=on_open,
                    on_message=on_message,
                    on_close=on_close,
                    on_error=on_error,
                )
                feed._app.run_forever(ping_interval=20, ping_timeout=10)
                feed.connected.clear()
                if feed._stop.wait(2.0):
                    break

        self._thread = threading.Thread(target=runner, name="okx-public-ws", daemon=True)
        self._thread.start()

    def stale(self, seconds: float = 8.0) -> bool:
        if not self.connected.is_set():
            return True
        if not self.last_event_at:
            return True
        return (time.time() - self.last_event_at) > seconds

    def stop(self) -> None:
        self._stop.set()
        app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        self.connected.clear()
