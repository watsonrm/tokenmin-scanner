#!/usr/bin/env python3
"""Tokenmin hosted-mode stub.

SPDX-License-Identifier: Apache-2.0

A minimal HTTP wrapper around `tokenmin_engine.analyze`. Implements just enough
of the hosted contract to test the `--submit-url` path end-to-end against a
local server. Not production: no TLS, no rate limiting, no auth check beyond
echoing whether a bearer was supplied, no storage.

A real hosted service (see ROADMAP.md, "Hosted analyze endpoint") replaces this
skeleton with a managed deployment that adds persistent storage, auth, rate
limiting, and cross-corpus benchmarking.

Contract (matches the scanner client in skills/tokenmin/tokenmin.py):
    POST /analyze
    Content-Type: application/json
    Authorization: Bearer <token>      # optional during preview
    Body: {"snapshot": <anonymized snapshot dict>}
    Response: {"report": "<markdown>"}  (200) | {"error": "..."} (4xx/5xx)
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Make the bundled engine importable when run from the repo root.
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "skills" / "tokenmin"))

import tokenmin_engine  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path != "/analyze":
            self._json(404, {"error": f"unknown path {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            body = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": f"bad JSON: {exc}"})
            return
        snapshot = body.get("snapshot")
        if not isinstance(snapshot, dict):
            self._json(400, {"error": "missing or non-dict 'snapshot' field"})
            return
        try:
            report = tokenmin_engine.analyze(snapshot)
        except Exception as exc:  # surface engine errors during preview
            self._json(500, {"error": f"engine error: {exc.__class__.__name__}: {exc}"})
            return
        self._json(200, {"report": report})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": f"unknown path {self.path}"})

    def log_message(self, fmt: str, *args) -> None:  # quieter default logs
        sys.stderr.write("tokenmin-server: " + (fmt % args) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tokenmin-server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    args = p.parse_args(argv)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"tokenmin-server: listening on http://{args.host}:{args.port}  "
        "POST /analyze  ·  GET /healthz",
        file=sys.stderr,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
