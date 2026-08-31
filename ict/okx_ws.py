"""OKX WebSocket: last prints (public) and confirmed candles (business). No auth, no orders."""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

PUBLIC_URL = "wss://ws.okx.com:8443/ws/v5/public"
BUSINESS_URL = "wss://ws.okx.com:8443/ws/v5/business"
WS_URL = PUBLIC_URL  # tickers; candles moved to BUSINESS_URL in 2023


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


def subscribe_error(raw: str) -> str | None:
    if raw in {"ping", "pong", ""}:
        return None
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(msg, dict) and msg.get("event") == "error":
        return str(msg.get("msg") or msg)
    return None


def parse_message(raw: str) -> list[Tick | BarClose]:
    """Parse one OKX WS frame. Ignore pings, acks, errors, and forming candles."""
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


def ticker_subscribe(instruments: list[str]) -> dict[str, Any]:
    return {"op": "subscribe", "args": [{"channel": "tickers", "instId": inst} for inst in instruments]}


def candle_subscribe(instruments: list[str], bar: str) -> dict[str, Any]:
    ch = candle_channel(bar)
    return {"op": "subscribe", "args": [{"channel": ch, "instId": inst} for inst in instruments]}


def subscribe_payload(instruments: list[str], bar: str) -> dict[str, Any]:
    """Channel names only. Tickers go to PUBLIC_URL; candles to BUSINESS_URL."""
    args = ticker_subscribe(instruments)["args"] + candle_subscribe(instruments, bar)["args"]
    return {"op": "subscribe", "args": args}


def drain_events(
    events: queue.Queue[Tick | BarClose],
    timeout: float = 1.0,
) -> tuple[dict[str, Tick], list[BarClose]]:
    """Collapse ticker spam to the latest last per inst. Keep every bar close."""
    ticks: dict[str, Tick] = {}
    closes: list[BarClose] = []
    try:
        first = events.get(timeout=timeout)
    except queue.Empty:
        return ticks, closes
    batch: list[Tick | BarClose] = [first]
    while True:
        try:
            batch.append(events.get_nowait())
        except queue.Empty:
            break
    for ev in batch:
        if isinstance(ev, Tick):
            ticks[ev.inst_id] = ev
        elif isinstance(ev, BarClose):
            closes.append(ev)
    return ticks, closes


class PublicFeed:
    """Tickers on /public, candles on /business. Events land on `.events`."""

    def __init__(self, instruments: list[str], bar: str = "15m") -> None:
        self.instruments = list(instruments)
        self.bar = bar
        self.events: queue.Queue[Tick | BarClose] = queue.Queue()
        self.connected = threading.Event()
        self.last_event_at = 0.0
        self._stop = threading.Event()
        self._up = {"public": threading.Event(), "business": threading.Event()}
        self._apps: dict[str, Any] = {}
        self._threads: list[threading.Thread] = []

    def _sync_connected(self) -> None:
        if self._up["public"].is_set() and self._up["business"].is_set():
            self.connected.set()
        else:
            self.connected.clear()

    def _start_socket(self, url: str, payload: dict[str, Any], name: str) -> None:
        import websocket

        feed = self

        def on_open(ws) -> None:  # noqa: ANN001
            ws.send(json.dumps(payload))
            feed._up[name].set()
            feed._sync_connected()

        def on_message(ws, message: str) -> None:  # noqa: ANN001
            if message == "ping":
                ws.send("pong")
                return
            if message == "pong":
                return
            err = subscribe_error(message)
            if err:
                print(f"WS {name}: {err}", file=sys.stderr)
                return
            parsed = parse_message(message)
            if parsed:
                feed.last_event_at = time.time()
            for ev in parsed:
                feed.events.put(ev)

        def on_error(_ws, error: object) -> None:
            print(f"WS {name} error: {error}", file=sys.stderr)

        def on_close(*_args: object) -> None:
            feed._up[name].clear()
            feed._sync_connected()

        def runner() -> None:
            while not feed._stop.is_set():
                app = websocket.WebSocketApp(
                    url,
                    on_open=on_open,
                    on_message=on_message,
                    on_close=on_close,
                    on_error=on_error,
                )
                feed._apps[name] = app
                app.run_forever(ping_interval=20, ping_timeout=10)
                feed._up[name].clear()
                feed._sync_connected()
                if feed._stop.wait(2.0):
                    break

        thread = threading.Thread(target=runner, name=f"okx-{name}-ws", daemon=True)
        self._threads.append(thread)
        thread.start()

    def start(self) -> None:
        self._start_socket(PUBLIC_URL, ticker_subscribe(self.instruments), "public")
        self._start_socket(BUSINESS_URL, candle_subscribe(self.instruments, self.bar), "business")

    def stale(self, seconds: float = 8.0) -> bool:
        if not self._up["public"].is_set():
            return True
        if not self.last_event_at:
            return True
        return (time.time() - self.last_event_at) > seconds

    def stop(self) -> None:
        self._stop.set()
        for app in list(self._apps.values()):
            try:
                app.close()
            except Exception:
                pass
        for ev in self._up.values():
            ev.clear()
        self.connected.clear()
