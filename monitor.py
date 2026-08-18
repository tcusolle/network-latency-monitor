#!/usr/bin/env python3
"""Continuous latency logger for the ISP bypass test.

Pings the modem (first hop) plus two external anchors on a fixed interval
and writes every result straight into a local SQLite database. The
dashboard (server.py + dashboard.html) reads that same database live, so
there's no CSV to import later -- the graph updates as pings land.

Usage:
    ./monitor.py                # every 60s, Ctrl-C to stop
    ./monitor.py --interval 30
    nohup ./monitor.py &        # keep running after closing the terminal
"""
import argparse
import datetime
import pathlib
import re
import sqlite3
import subprocess
import sys
import time

DIR = pathlib.Path(__file__).resolve().parent
DB_PATH = DIR / "latency.db"
RTT_RE = re.compile(r"time=([\d.]+)")


def get_default_gateway() -> str | None:
    try:
        # -n forces numeric output; without it, macOS can reverse/mDNS-resolve
        # the gateway to a hostname (e.g. "modem.example.local"), which
        # then makes every "modem" ping pay for a DNS lookup too.
        out = subprocess.check_output(["route", "-n", "get", "default"], text=True)
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("gateway:"):
            return line.split(":", 1)[1].strip()
    return None


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,        -- ISO 8601, local time
            target TEXT NOT NULL,    -- IP pinged
            label TEXT NOT NULL,     -- modem_first_hop / cloudflare / google_dns
            rtt_ms REAL,             -- NULL when the ping failed
            status TEXT NOT NULL     -- ok / timeout / error
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pings_ts ON pings(ts)")
    conn.execute("PRAGMA journal_mode=WAL")  # lets the dashboard read while we write
    conn.commit()


def ping_once(host: str, timeout: int) -> tuple[float | None, str]:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-t", str(timeout), host],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception:
        return None, "error"
    match = RTT_RE.search(result.stdout)
    if match:
        return float(match.group(1)), "ok"
    return None, "timeout"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=60, help="seconds between rounds")
    parser.add_argument("--ping-timeout", type=int, default=2, help="seconds to wait per ping")
    args = parser.parse_args()

    gateway = get_default_gateway()
    if not gateway:
        print("Could not detect a default gateway -- is the Mac online?", file=sys.stderr)
        sys.exit(1)

    targets = [
        ("modem_first_hop", gateway),
        ("cloudflare", "1.1.1.1"),
        ("google_dns", "8.8.8.8"),
    ]

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    print(f"Logging to {DB_PATH}")
    print(f"Gateway detected: {gateway}")
    print(f"Testing every {args.interval}s against: {[t[1] for t in targets]}")
    print("Press Ctrl-C to stop.")

    try:
        while True:
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            rows = []
            for label, host in targets:
                rtt, status = ping_once(host, args.ping_timeout)
                rows.append((ts, host, label, rtt, status))
            conn.executemany(
                "INSERT INTO pings (ts, target, label, rtt_ms, status) VALUES (?,?,?,?,?)",
                rows,
            )
            conn.commit()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
