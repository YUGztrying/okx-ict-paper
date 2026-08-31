#!/usr/bin/env python
"""Mobile blotter for the paper desk. Read-only — no orders."""

from __future__ import annotations

import argparse
import json
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ict.journal import desk_payload
from ict.okx_data import fetch_last

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "dashboard" / "index.html"
INST_RE = re.compile(r"^[A-Z0-9-]{3,24}$")


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/desk":
            payload = json.dumps(desk_payload()).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
            return
        if path == "/api/tickers":
            raw = parse_qs(parsed.query).get("instIds", [""])[0]
            ids = [part.strip() for part in raw.split(",") if part.strip()]
            tickers = []
            for inst in ids:
                if not INST_RE.match(inst):
                    continue
                try:
                    tickers.append({"instId": inst, "last": fetch_last(inst)})
                except Exception as exc:
                    tickers.append({"instId": inst, "error": str(exc)})
            self._send(200, json.dumps({"tickers": tickers}).encode(), "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    ip = lan_ip()
    print(f"Desk blotter", flush=True)
    print(f"  phone (same Wi-Fi):  http://{ip}:{args.port}", flush=True)
    print(f"  this PC:             http://127.0.0.1:{args.port}", flush=True)
    print("Read-only. Ctrl+C to stop.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
