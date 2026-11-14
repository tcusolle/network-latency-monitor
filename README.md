# Network Latency Monitor

A zero-dependency latency logger and live dashboard for diagnosing intermittent
ISP problems. It pings the modem's first hop plus two external anchors every
60 seconds, stores the results in SQLite, and serves a real-time chart so the
evidence is easy to read and to hand to a support technician.

No external dependencies: stdlib Python 3 only, no CDN, no `pip install`.
That matters here: the dashboard has to keep working while the connection it is
measuring is degraded.

## What it does

- **`monitor.py`** — pings three targets every 60s and writes results to
  `latency.db` (SQLite):
  - `modem_first_hop` — the modem's gateway IP (detected automatically)
  - `cloudflare` — 1.1.1.1
  - `google_dns` — 8.8.8.8

  Pinging the modem itself *and* two external anchors is the point: if only
  the external targets spike while the modem stays flat, the problem is
  upstream in the ISP's network, not your equipment. If the modem itself
  spikes, it's local (RF/wifi/modem).

- **`server.py`** — a small stdlib HTTP server that serves the dashboard
  and a JSON API reading live from `latency.db`. No polling of files, no
  export step — the dashboard queries the same database `monitor.py` is
  writing to, in real time.

- **`dashboard.html` / `static/app.js`** — a self-contained line chart
  (hand-rolled SVG, no charting library) with:
  - live auto-refresh (every 5s)
  - drag-to-zoom on the chart, double-click to reset, plus time-range
    presets (15 min / 1 hr / 6 hr / 24 hr / all)
  - crosshair + tooltip showing all three targets at once
  - packet-loss tick marks along the bottom
  - a status badge per target (Good / Elevated / High) based on avg
    latency and loss %, so you can tell severity at a glance
  - a table view toggle for the raw numbers

## Running it

```bash
# keep the machine awake during the test (macOS)
caffeinate -s &

# start logging (every 60s, one sample per minute)
nohup python3 monitor.py > monitor.out 2>&1 &

# start the dashboard
python3 server.py &
open http://127.0.0.1:8787
```

Both `nohup`'d processes survive closing the terminal, but not a reboot or
sleep — if the Mac restarts, re-run both commands. `monitor.py` and
`server.py` can run in either order and independently; the dashboard just
shows "waiting for monitor.py to start logging…" until there's data.

To stop:

```bash
pkill -f monitor.py
pkill -f server.py
```

## Interpreting the results

- Cable baseline to a nearby server is normally **5-25 ms**. The dashboard's
  status badges use average latency and packet loss rather than a single raw
  number.
- For an intermittent issue, what matters is **spikes relative to your own
  baseline**, **jitter**, and **packet loss**.
- Zoom into a bad window and check whether `modem_first_hop` moved too. If it
  stayed flat while both external targets degraded, the problem is upstream of
  your equipment, and that timestamped evidence is what gets a technician
  investigation.
- The **loss ticks** at the bottom of the chart matter as much as the latency
  line.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | Logger — pings targets, writes to `latency.db` |
| `server.py` | Local web server — dashboard + JSON API over `latency.db` |
| `dashboard.html` | Dashboard page shell/markup |
| `static/app.js` | Dashboard logic (fetch, chart render, zoom, tooltip) |
| `latency.db` | SQLite database (git-ignored, created on first run) |

## History

This project was developed locally before it had a repository and was not under
version control at the time. The commits here were assembled afterwards, grouped
by component. Each commit date is the last-modified time of the newest file in
that commit, so dates are approximate but real. The final commit (README and
license) was added when the project was published.
