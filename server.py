#!/usr/bin/env python3
"""Local dashboard server for the ISP latency monitor.

Serves dashboard.html/static assets and a small JSON API that reads
live from latency.db (the file monitor.py is writing to). Stdlib only.

Usage:
    ./server.py                 # http://127.0.0.1:8787
    ./server.py --port 9000
"""
import argparse
import json
import pathlib
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DIR = pathlib.Path(__file__).resolve().parent
DB_PATH = DIR / "latency.db"
STATIC_DIR = DIR / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
}


def ro_connection() -> sqlite3.Connection | None:
    """Read-only connection to the live DB, or None if it doesn't exist yet."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class Handler(BaseHTTPRequestHandler):
    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status: int = 200) -> None:
        self._send_bytes(json.dumps(data).encode(), "application/json", status)

    def _send_file(self, path: pathlib.Path) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        content_type = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        self._send_bytes(path.read_bytes(), content_type)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/dashboard.html"):
            self._send_file(DIR / "dashboard.html")
        elif path.startswith("/static/"):
            self._send_file(STATIC_DIR / path[len("/static/") :])
        elif path == "/api/pings":
            self._handle_pings(parse_qs(parsed.query))
        elif path == "/api/stats":
            self._handle_stats()
        else:
            self.send_error(404)

    def _handle_pings(self, query: dict) -> None:
        conn = ro_connection()
        if conn is None:
            self._send_json([])
            return
        since = query.get("since", [None])[0]
        limit = min(int(query.get("limit", [50000])[0]), 200000)
        sql = "SELECT ts, target, label, rtt_ms, status FROM pings"
        params: list = []
        if since:
            sql += " WHERE ts >= ?"
            params.append(since)
        sql += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        self._send_json([dict(r) for r in rows])

    def _handle_stats(self) -> None:
        conn = ro_connection()
        if conn is None:
            self._send_json([])
            return
        rows = conn.execute(
            """
            SELECT label,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_count,
                   AVG(CASE WHEN status = 'ok' THEN rtt_ms END) AS avg_rtt,
                   MAX(CASE WHEN status = 'ok' THEN rtt_ms END) AS max_rtt,
                   MIN(ts) AS first_ts,
                   MAX(ts) AS last_ts
            FROM pings
            GROUP BY label
            """
        ).fetchall()
        conn.close()
        self._send_json([dict(r) for r in rows])

    def log_message(self, format, *args) -> None:  # noqa: A002 (stdlib signature)
        pass  # keep the console quiet; errors still go through send_error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Dashboard running at http://127.0.0.1:{args.port}")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
