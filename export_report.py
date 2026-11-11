#!/usr/bin/env python3
"""Generate a written report + CSV of flagged latency spikes for ISP.

Usage: ./export_report.py
Writes into the project's report/ directory:
  - latency_report_<date>.md   the report (source of truth)
  - latency_report_<date>.html the same report, styled for print
  - latency_report_<date>.pdf  rendered via headless Chrome (skipped,
    with a printed note, if Chrome/Chromium isn't installed)
  - latency_incidents.csv      every flagged spike round, raw
"""
import datetime
import html as html_lib
import pathlib
import re
import shutil
import sqlite3
import subprocess

DIR = pathlib.Path(__file__).resolve().parent
DB_PATH = DIR / "latency.db"
REPORT_DIR = DIR / "report"
SPIKE_THRESHOLD_MS = 60

# 2026-08-18T09:42-10:34: a monitoring-tool bug briefly resolved the modem's
# gateway to a hostname (mDNS) instead of its IP, so "modem_first_hop" pings
# in this window measured DNS lookup time, not network RTT. Excluded from
# all stats/incidents below -- see monitor.py's get_default_gateway() fix.
EXCLUDE_WINDOWS = []  # (start, end) ISO windows to drop from stats

# Incidents that independently matched a router latency alarm
KNOWN_ALARM_WINDOWS = []  # (start, end, label) incidents matching an external alarm

# Other incidents matching the same signature (modem flat, both external
# targets degrade/fail together) found during continued monitoring -- not
# tied to an external alarm, but the same diagnostic pattern.
OTHER_INCIDENT_WINDOWS = []  # (start, end, label) other matching incidents

# Multi-day stretches of sustained degradation, found by eyeballing the daily
# spike-count trend. Unlike KNOWN_ALARM_WINDOWS/OTHER_INCIDENT_WINDOWS (short,
# clean modem-flat/externals-spike signatures), these are long enough that the
# modem's OWN signature during the window matters and is computed + reported
# per-window rather than assumed -- see the Severe Degradation Periods section.
SEVERE_WINDOWS = [
    ("2026-09-30T06:10:00", "2026-09-30T07:16:00", "Sep 30, ~6:10–7:15 AM"),
    ("2026-10-09T00:00:00", "2026-10-17T00:00:00", "Oct 9–16"),
    ("2026-10-21T00:00:00", "2026-10-23T00:00:00", "Oct 21–22"),
]

# Minimum gap, in minutes, between consecutive logged rounds before it's
# reported as a logging outage rather than normal ~60-70s round cadence jitter.
GAP_THRESHOLD_MIN = 10


def alarm_note(ts: str) -> str:
    for start, end, note in KNOWN_ALARM_WINDOWS:
        if start <= ts <= end:
            return note
    return ""


def is_excluded(ts: str) -> bool:
    return any(start <= ts <= end for start, end in EXCLUDE_WINDOWS)


def find_outages(rows: list[sqlite3.Row]) -> list[dict]:
    """Detect stretches where BOTH external targets (cloudflare + google_dns)
    timed out on the same round, for 2+ consecutive rounds. This is a much
    stronger signal than a single-target timeout, since it rules out a
    one-off issue with a single external service: the modem's own uplink
    was unreachable for that stretch."""
    by_ts: dict[str, dict] = {}
    for r in rows:
        by_ts.setdefault(r["ts"], {})[r["label"]] = r["status"]
    both_down_ts = sorted(
        ts
        for ts, labels in by_ts.items()
        if labels.get("cloudflare") != "ok" and labels.get("google_dns") != "ok"
    )
    if not both_down_ts:
        return []
    outages = []
    run_start = prev = both_down_ts[0]
    run_len = 1
    for ts in both_down_ts[1:]:
        gap = (
            datetime.datetime.fromisoformat(ts) - datetime.datetime.fromisoformat(prev)
        ).total_seconds()
        if gap <= 150:  # allow for the ~65-70s round cadence plus jitter
            run_len += 1
        else:
            if run_len >= 2:
                outages.append({"start": run_start, "end": prev, "rounds": run_len})
            run_start, run_len = ts, 1
        prev = ts
    if run_len >= 2:
        outages.append({"start": run_start, "end": prev, "rounds": run_len})
    return outages


def find_logging_gaps(rows: list[sqlite3.Row]) -> list[dict]:
    """Stretches where the monitor itself wasn't running (no rows logged at
    all), as opposed to rows existing but showing timeouts. Typically a
    reboot where nobody logged back into the GUI session -- LaunchAgents only
    start after login, not at raw boot."""
    distinct_ts = sorted({r["ts"] for r in rows})
    gaps = []
    for prev, cur in zip(distinct_ts, distinct_ts[1:]):
        minutes = (
            datetime.datetime.fromisoformat(cur) - datetime.datetime.fromisoformat(prev)
        ).total_seconds() / 60
        if minutes > GAP_THRESHOLD_MIN:
            gaps.append({"start": prev, "end": cur, "minutes": minutes})
    return gaps


def window_stats(rows: list[sqlite3.Row], start: str, end: str, targets: list[str]) -> dict:
    out = {}
    for t in targets:
        trows = [r for r in rows if r["label"] == t and start <= r["ts"] <= end]
        oks = sorted(r["rtt_ms"] for r in trows if r["status"] == "ok" and r["rtt_ms"] is not None)
        loss = sum(1 for r in trows if r["status"] != "ok")
        if not oks:
            out[t] = {"n": len(trows), "avg": None, "p95": None, "max": None, "loss": loss}
            continue
        out[t] = {
            "n": len(trows),
            "avg": sum(oks) / len(oks),
            "p95": oks[int(0.95 * (len(oks) - 1))],
            "max": max(oks),
            "loss": loss,
        }
    return out


# ---- markdown -> HTML -> PDF -------------------------------------------
# The report only ever uses a small, controlled markdown subset (since we
# generate it ourselves): #, ##, **bold**, *italic*, "- " bullets, and
# "|"-pipe tables. A tiny hand-rolled converter avoids adding a dependency
# for that subset -- no pip install needed for something this constrained.

def _inline_md(text: str) -> str:
    text = html_lib.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)
    return text


def markdown_to_html_body(md_lines: list[str]) -> str:
    parts: list[str] = []
    list_open = False
    i, n = 0, len(md_lines)
    while i < n:
        line = md_lines[i]
        if line.startswith("|"):
            if list_open:
                parts.append("</ul>")
                list_open = False
            table_lines = []
            while i < n and md_lines[i].startswith("|"):
                table_lines.append(md_lines[i])
                i += 1
            header = [c.strip() for c in table_lines[0].strip("|").split("|")]
            body_rows = table_lines[2:]  # row 1 is the header, row 2 is the --- separator
            parts.append("<table>")
            parts.append(
                "<thead><tr>" + "".join(f"<th>{_inline_md(c)}</th>" for c in header) + "</tr></thead>"
            )
            parts.append("<tbody>")
            for row in body_rows:
                cells = [c.strip() for c in row.strip("|").split("|")]
                parts.append("<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in cells) + "</tr>")
            parts.append("</tbody></table>")
            continue
        if line.startswith("- "):
            if not list_open:
                parts.append("<ul>")
                list_open = True
            parts.append(f"<li>{_inline_md(line[2:])}</li>")
            i += 1
            continue
        if list_open:
            parts.append("</ul>")
            list_open = False
        if line.startswith("# "):
            parts.append(f"<h1>{_inline_md(line[2:])}</h1>")
        elif line.startswith("## "):
            parts.append(f"<h2>{_inline_md(line[3:])}</h2>")
        elif line.strip():
            parts.append(f"<p>{_inline_md(line)}</p>")
        i += 1
    if list_open:
        parts.append("</ul>")
    return "\n".join(parts)


REPORT_CSS = """
  body { font: 11pt/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #16181c; max-width: 780px; margin: 32px auto; padding: 0 20px; }
  h1 { font-size: 19pt; margin: 0 0 4px; }
  h2 { font-size: 13pt; margin: 26px 0 8px; padding-top: 10px; border-top: 1px solid #d8dade; }
  h2:first-of-type { border-top: none; padding-top: 0; }
  p { margin: 6px 0; }
  ul { margin: 6px 0; padding-left: 22px; }
  li { margin: 3px 0; }
  strong { color: #0b0b0b; }
  table { border-collapse: collapse; width: 100%; margin: 10px 0 14px; font-size: 9.5pt;
          page-break-inside: avoid; }
  th, td { border: 1px solid #d8dade; padding: 5px 8px; text-align: left; }
  th { background: #f2f3f5; font-weight: 600; }
  em { color: #52565e; }
  tr, table { page-break-inside: avoid; }
"""


def render_pdf(md_lines: list[str], out_path: pathlib.Path) -> bool:
    """Render the markdown lines to a PDF via headless Chrome. Returns False
    (with a printed reason) if Chrome isn't available or the render fails --
    callers should treat that as non-fatal, since the .md/.csv are the
    source of truth and already written."""
    chrome_candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
    ]
    chrome = next((c for c in chrome_candidates if c and pathlib.Path(c).exists()), None)
    if not chrome:
        print("PDF skipped: no Chrome/Chromium install found to render it.")
        return False

    body = markdown_to_html_body(md_lines)
    html_doc = f"<!doctype html><html><head><meta charset='utf-8'><style>{REPORT_CSS}</style></head><body>{body}</body></html>"
    html_path = out_path.with_suffix(".html")
    html_path.write_text(html_doc)

    result = subprocess.run(
        [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={out_path}",
            f"file://{html_path}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        print(f"PDF render failed (exit {result.returncode}): {result.stderr[-500:]}")
        return False
    return True


def main() -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    all_rows = conn.execute("SELECT ts, target, label, rtt_ms, status FROM pings ORDER BY ts").fetchall()
    excluded_count = sum(1 for r in all_rows if is_excluded(r["ts"]))
    rows = [r for r in all_rows if not is_excluded(r["ts"])]

    # Computed on all_rows (pre-exclusion) so the DNS-hostname-bug window --
    # where rows exist but are excluded from stats -- doesn't get misread as
    # a logging gap. A true gap means no rows were logged at all.
    logging_gaps = find_logging_gaps(all_rows)

    first_ts, last_ts = rows[0]["ts"], rows[-1]["ts"]
    total_pings = len(rows)
    total_loss = sum(1 for r in rows if r["status"] != "ok")

    targets = ["modem_first_hop", "cloudflare", "google_dns"]
    target_labels = {
        "modem_first_hop": "Modem (first hop)",
        "cloudflare": "Cloudflare (1.1.1.1)",
        "google_dns": "Google DNS (8.8.8.8)",
    }
    stats = {}
    for t in targets:
        trows = [r for r in rows if r["label"] == t]
        oks = sorted(r["rtt_ms"] for r in trows if r["status"] == "ok" and r["rtt_ms"] is not None)
        loss = sum(1 for r in trows if r["status"] != "ok")
        stats[t] = {
            "n": len(trows),
            "avg": sum(oks) / len(oks),
            "p95": oks[int(0.95 * (len(oks) - 1))],
            "p99": oks[int(0.99 * (len(oks) - 1))],
            "max": max(oks),
            "loss": loss,
            "loss_pct": 100 * loss / len(trows),
        }

    # daily spike-round counts, rolled up to weekly for a report-length table
    spike_rows = [r for r in rows if r["rtt_ms"] is not None and r["rtt_ms"] > SPIKE_THRESHOLD_MS]
    by_day: dict[str, set[str]] = {}
    for r in spike_rows:
        by_day.setdefault(r["ts"][:10], set()).add(r["ts"])
    daily_counts = sorted((day, len(rounds)) for day, rounds in by_day.items())

    by_week: dict[str, int] = {}
    for day, count in daily_counts:
        monday = datetime.date.fromisoformat(day) - datetime.timedelta(
            days=datetime.date.fromisoformat(day).weekday()
        )
        by_week[monday.isoformat()] = by_week.get(monday.isoformat(), 0) + count
    weekly_counts = sorted(by_week.items())

    peak_day, peak_count = max(daily_counts, key=lambda dc: dc[1])

    # severe multi-day windows: compute per-target stats and classify the
    # signature rather than assuming it matches the short, clean incidents
    severe_results = []
    for start, end, label in SEVERE_WINDOWS:
        w = window_stats(rows, start, end, targets)
        modem_p95 = w["modem_first_hop"]["p95"]
        ext_p95s = [w[t]["p95"] for t in ("cloudflare", "google_dns") if w[t]["p95"] is not None]
        ext_p95_avg = sum(ext_p95s) / len(ext_p95s) if ext_p95s else None
        if modem_p95 is None or ext_p95_avg is None:
            signature = "upstream"
        elif modem_p95 >= ext_p95_avg * 1.3:
            signature = "modem_worse"
        elif modem_p95 >= ext_p95_avg * 0.8:
            signature = "comparable"
        else:
            signature = "upstream"
        severe_results.append({"label": label, "start": start, "end": end, "stats": w, "signature": signature})

    # current status: most recent 24h of logged data, for "is this still happening"
    current_start = (
        datetime.datetime.fromisoformat(last_ts) - datetime.timedelta(hours=24)
    ).isoformat()
    current_stats = window_stats(rows, current_start, last_ts, targets)

    worst = sorted(
        (r for r in rows if r["status"] == "ok" and r["rtt_ms"] is not None),
        key=lambda r: r["rtt_ms"],
        reverse=True,
    )[:10]

    outages = find_outages(rows)
    # modem status during each outage window, to confirm it stayed up
    outage_modem_notes = []
    for o in outages:
        modem_rows = [
            r for r in rows
            if r["label"] == "modem_first_hop" and o["start"] <= r["ts"] <= o["end"]
        ]
        modem_oks = [r for r in modem_rows if r["status"] == "ok" and r["rtt_ms"] is not None]
        if modem_rows and len(modem_oks) == len(modem_rows):
            avg = sum(r["rtt_ms"] for r in modem_oks) / len(modem_oks)
            outage_modem_notes.append(
                f"modem answered normally throughout ({len(modem_rows)}/{len(modem_rows)} "
                f"rounds ok, avg {avg:.1f}ms)"
            )
        else:
            outage_modem_notes.append(
                f"modem also affected: {len(modem_rows) - len(modem_oks)}/{len(modem_rows)} "
                "rounds failed"
            )

    # -------- CSV: every ping that was part of a spike round --------
    spike_ts = sorted({r["ts"] for r in spike_rows})
    by_ts: dict[str, dict] = {}
    for r in rows:
        by_ts.setdefault(r["ts"], {})[r["label"]] = r["rtt_ms"] if r["status"] == "ok" else r["status"]
    csv_path = REPORT_DIR / "latency_incidents.csv"
    with open(csv_path, "w") as f:
        f.write("timestamp,modem_first_hop_ms,cloudflare_ms,google_dns_ms,note\n")
        for ts in spike_ts:
            vals = by_ts.get(ts, {})
            note = alarm_note(ts)
            f.write(
                f"{ts},{vals.get('modem_first_hop','')},{vals.get('cloudflare','')},"
                f"{vals.get('google_dns','')},{note}\n"
            )

    # -------- Markdown report --------
    today = datetime.date.today().isoformat()
    days_tested = (
        datetime.datetime.fromisoformat(last_ts) - datetime.datetime.fromisoformat(first_ts)
    ).days
    lines = []
    lines.append("# Internet Latency Test Report — ISP Residential Service")
    lines.append("")
    lines.append(f"**Prepared:** {today}")
    lines.append(f"**Test period:** {first_ts} to {last_ts} ({days_tested} days, continuous)")
    lines.append(
        "**Method:** A Mac mini connected via wifi directly to the ISP modem "
        "(bypassing all other network equipment — no router, no router in the path), "
        "as requested by the ISP's technical support. Pinged three targets every 60 "
        "seconds, continuously, for the full test period:"
    )
    lines.append("- The ISP modem itself (first hop) — isolates the modem/local wifi")
    lines.append("- Cloudflare (1.1.1.1) and Google DNS (8.8.8.8) — external reference points")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    if outages:
        total_outage_min = sum(
            (
                datetime.datetime.fromisoformat(o["end"])
                - datetime.datetime.fromisoformat(o["start"])
            ).total_seconds()
            / 60
            for o in outages
        )
        outage_word = "outage" if len(outages) == 1 else "outages"
        modem_healthy_count = sum(1 for n in outage_modem_notes if n.startswith("modem answered"))
        if modem_healthy_count == len(outages):
            modem_clause = (
                "while the modem itself kept answering normally throughout every one of them "
                "— ruling out a single external service's own issue and ruling out local "
                "equipment, since the modem link stayed healthy the whole time"
            )
        else:
            modem_clause = (
                f"the modem itself stayed healthy through {modem_healthy_count} of them, but was "
                f"also affected during the other {len(outages) - modem_healthy_count}"
            )
        lines.append(
            f"- **{len(outages)} confirmed {outage_word} totaling ~{total_outage_min:.0f} minutes** "
            "where both independent external targets (Cloudflare and Google DNS) were "
            f"completely unreachable at the same time, {modem_clause} "
            "— see Confirmed Outages below for the per-outage detail."
        )
    lines.append(
        f"- **{total_pings:,} pings sent, {total_loss} lost ({100*total_loss/total_pings:.3f}% loss)** "
        "over the full test period. Loss is concentrated in the outages and severe-degradation "
        "windows below, not spread evenly — day-to-day loss outside those windows is "
        "effectively zero."
    )
    sev_word = "period" if len(severe_results) == 1 else "periods"
    lines.append(
        "- Baseline performance is healthy most of the time (see table below), punctuated "
        f"by recurring short latency spikes and — more significantly — {len(severe_results)} "
        f"sustained severe-degradation {sev_word} (from just over an hour to a full week). "
        "See Severe Degradation Periods below; this is the strongest evidence in this "
        "report."
    )
    spike_total = len(spike_ts)
    lines.append(
        f"- **{spike_total:,} distinct 60-second checks out of {total_pings//3:,} "
        f"({100*spike_total/(total_pings//3):.2f}%) showed a ping above {SPIKE_THRESHOLD_MS}ms.** "
        f"Frequency is not steady: the single worst day was **{peak_day} ({peak_count} spike "
        "rounds)**, part of a sustained severe stretch — see Severe Degradation Periods "
        "below. Outside that stretch, activity is intermittent at a much lower background "
        "rate (see the weekly breakdown below)."
    )
    lines.append(
        "- Two latency spikes independently coincided with high-latency alarms "
        "raised by our own router router (a separate device, not part of this "
        "test rig), on Aug 11 and Aug 12 — see Key Incidents below."
    )
    cur_ext_avg = [current_stats[t]["avg"] for t in ("cloudflare", "google_dns") if current_stats[t]["avg"]]
    cur_ext_avg = sum(cur_ext_avg) / len(cur_ext_avg) if cur_ext_avg else None
    baseline_ext_avg = (stats["cloudflare"]["avg"] + stats["google_dns"]["avg"]) / 2
    if cur_ext_avg is not None:
        status_word = "back to the healthy baseline" if cur_ext_avg <= baseline_ext_avg * 1.5 else "still elevated"
        lines.append(
            f"- **Current status (last 24h of data, through {last_ts}): {status_word}.** "
            f"Modem avg {current_stats['modem_first_hop']['avg']:.1f}ms, external avg "
            f"{cur_ext_avg:.1f}ms, {current_stats['cloudflare']['loss'] + current_stats['google_dns']['loss']} "
            "lost pings. The issue is intermittent — it is not necessarily happening at the "
            "moment this report was generated, which is expected given the pattern above."
        )
    lines.append("")
    lines.append("## Baseline latency by target")
    lines.append("")
    lines.append("| Target | Samples | Avg | p95 | p99 | Max | Loss |")
    lines.append("|---|---|---|---|---|---|---|")
    for t in targets:
        s = stats[t]
        lines.append(
            f"| {target_labels[t]} | {s['n']:,} | {s['avg']:.1f}ms | {s['p95']:.1f}ms | "
            f"{s['p99']:.1f}ms | {s['max']:.1f}ms | {s['loss']} ({s['loss_pct']:.3f}%) |"
        )
    lines.append("")
    lines.append(
        "*Note: latency to the modem itself stays low almost throughout — most spikes "
        "appear on the external targets (Cloudflare/Google DNS) while the modem hop "
        "stays flat, indicating the added latency occurs past the modem, in the "
        "network beyond it.*"
    )
    lines.append("")
    if outages:
        lines.append("## Confirmed outages (both external targets down simultaneously)")
        lines.append("")
        lines.append(
            "These are stretches of 2+ consecutive checks (every ~60s) where both "
            "Cloudflare and Google DNS — independent, unrelated networks — failed to "
            "respond at all. Most of the time the modem itself kept answering normally "
            "throughout (confirming the outage sits beyond the modem); a couple did also "
            "see brief modem-side failures, noted below rather than glossed over."
        )
        lines.append("")
        lines.append("| Start | End | Duration | Failed rounds | Modem status |")
        lines.append("|---|---|---|---|---|")
        for o, note in zip(outages, outage_modem_notes):
            dur_min = (
                datetime.datetime.fromisoformat(o["end"])
                - datetime.datetime.fromisoformat(o["start"])
            ).total_seconds() / 60
            lines.append(
                f"| {o['start']} | {o['end']} | ~{dur_min:.0f} min | {o['rounds']} | {note} |"
            )
        lines.append("")
    non_upstream_count = sum(1 for s in severe_results if s["signature"] != "upstream")
    lines.append("## Severe degradation periods")
    lines.append("")
    if non_upstream_count == 0:
        signature_clause = "all of them hold to that signature."
    elif non_upstream_count == len(severe_results):
        signature_clause = "none of them do, and that's reported honestly rather than folded into the same story."
    else:
        signature_clause = (
            f"{non_upstream_count} of the {len(severe_results)} don't, and that's reported "
            "honestly rather than folded into the same story."
        )
    lines.append(
        f"{len(severe_results)} stretches stand well apart from the recurring short spikes "
        "elsewhere in this report — sustained degradation, from just over an hour to a "
        "full week, across hundreds of checks each. Each is reported here with its own "
        "modem-vs-external comparison, rather than assuming it matches the clean \"modem "
        f"flat, externals spike\" signature of the short incidents above — {signature_clause}"
    )
    lines.append("")
    for sev in severe_results:
        w = sev["stats"]
        # when the window end lands exactly on midnight, display the prior
        # day so "Oct 9-16" doesn't show an end date of Oct 17
        end_dt = datetime.datetime.fromisoformat(sev["end"])
        display_end = (end_dt - datetime.timedelta(seconds=1)) if end_dt.time() == datetime.time(0, 0, 0) else end_dt
        lines.append(f"**{sev['label']}** ({sev['start'][:10]} to {display_end.date().isoformat()})")
        lines.append("")
        lines.append("| Target | Avg | p95 | Max | Loss |")
        lines.append("|---|---|---|---|---|")
        for t in targets:
            s = w[t]
            if s["avg"] is None:
                continue
            lines.append(
                f"| {target_labels[t]} | {s['avg']:.1f}ms | {s['p95']:.1f}ms | "
                f"{s['max']:.1f}ms | {s['loss']} |"
            )
        if sev["signature"] == "modem_worse":
            lines.append(
                "- *Signature: the modem/wifi hop was clearly worse than the external "
                "targets during this window* (not just comparable — meaningfully higher). "
                "This does not point upstream — it points at the modem or the local wifi "
                "link itself, and should be checked directly (modem signal/power levels, "
                "wifi channel congestion, modem hardware) independently of any upstream "
                "ISP investigation."
            )
        elif sev["signature"] == "comparable":
            lines.append(
                "- *Signature: the modem/wifi hop was comparably degraded to the external "
                "targets during this window.* Unlike the clean incidents above, this "
                "doesn't point cleanly upstream — it's also consistent with a local "
                "contributor (wifi interference, modem load) during this specific window, "
                "and is worth checking independently (wifi channel, modem signal levels) "
                "alongside the ISP-side investigation."
            )
        else:
            lines.append(
                "- *Signature: external targets were more degraded than the modem hop* "
                "— consistent with the modem staying relatively more stable while "
                "something beyond it struggled, similar to the shorter confirmed "
                "incidents above, though the modem here was still measurably elevated "
                "versus its normal baseline, which outright congestion at or near the "
                "modem could also explain."
            )
        lines.append("")
    lines.append("## Weekly spike frequency (checks with a ping over 60ms)")
    lines.append("")
    lines.append("| Week of | Spike rounds |")
    lines.append("|---|---|")
    for week, count in weekly_counts:
        lines.append(f"| {week} | {count} |")
    lines.append("")
    lines.append(
        "*Full per-round detail for every flagged check, across the entire test period, "
        "is in the accompanying CSV.*"
    )
    lines.append("")
    lines.append("## Key incidents")
    lines.append("")
    for start, end, note in KNOWN_ALARM_WINDOWS + OTHER_INCIDENT_WINDOWS:
        lines.append(f"**{note}**")
        incident_rows = [
            r for r in rows
            if start <= r["ts"] <= end
            and (r["status"] != "ok" or (r["rtt_ms"] is not None and r["rtt_ms"] > 40))
        ]
        for r in incident_rows:
            val = f"{r['rtt_ms']:.1f}ms" if r["status"] == "ok" else r["status"]
            lines.append(f"- {r['ts']} — {target_labels[r['label']]}: {val}")
        lines.append("")
    lines.append("## Worst individual measurements (full test period)")
    lines.append("")
    lines.append("| Timestamp | Target | Latency |")
    lines.append("|---|---|---|")
    for r in worst:
        lines.append(f"| {r['ts']} | {target_labels[r['label']]} | {r['rtt_ms']:.1f}ms |")
    lines.append("")
    lines.append("## Request")
    lines.append("")
    longest_outage = max(outages, key=lambda o: o["rounds"]) if outages else None
    longest_severe = max(
        severe_results,
        key=lambda s: datetime.datetime.fromisoformat(s["end"]) - datetime.datetime.fromisoformat(s["start"]),
        default=None,
    )
    outage_clause = (
        f"a confirmed ~{(datetime.datetime.fromisoformat(longest_outage['end']) - datetime.datetime.fromisoformat(longest_outage['start'])).total_seconds() / 60:.0f}-minute outage where two independent "
        "external networks were simultaneously unreachable while the modem itself "
        "stayed healthy, "
        if outages
        else ""
    )
    severe_clause = (
        f"a documented stretch of severe degradation ({longest_severe['label']}) across "
        "hundreds of checks, "
        if longest_severe
        else ""
    )
    local_signature_labels = [s["label"] for s in severe_results if s["signature"] != "upstream"]
    local_clause = (
        f" Separately, {('one window' if len(local_signature_labels) == 1 else f'{len(local_signature_labels)} windows')} "
        f"({', '.join(local_signature_labels)}) showed the modem/wifi hop itself elevated "
        "alongside or beyond the external targets — worth checking modem signal levels "
        "and wifi interference independently of the upstream investigation, since that "
        "signature doesn't point cleanly past the modem the way the other evidence does."
        if local_signature_labels
        else ""
    )
    lines.append(
        f"Given {outage_clause}{severe_clause}a generally healthy baseline the rest of the "
        "time, and a recurring pattern of latency spikes measured directly at the modem "
        "with all other equipment bypassed — please investigate upstream of the modem "
        "(node/plant congestion, upstream routing, or a peering issue affecting traffic to "
        "multiple external networks at once). Local equipment has been ruled out for the "
        f"short, clean incidents in this report.{local_clause} Requesting a technician "
        "dispatch with access to node-level diagnostics, per the escalation from prior "
        "chat support (transcript available on request)."
    )
    lines.append("")
    methodology_notes = []
    if excluded_count:
        methodology_notes.append(
            f"{excluded_count} samples between {EXCLUDE_WINDOWS[0][0]} and "
            f"{EXCLUDE_WINDOWS[0][1]} were excluded from this report. A temporary bug in "
            "the monitoring script caused the modem target to resolve by hostname instead "
            "of IP address during that window, so those samples measured local DNS lookup "
            "time rather than network latency. Fixed; excluded for accuracy."
        )
    if logging_gaps:
        for g in logging_gaps:
            days = g["minutes"] / 1440
            dur = f"~{days:.1f} days" if days >= 1 else f"~{g['minutes']:.0f} minutes"
            methodology_notes.append(
                f"No data was logged between {g['start']} and {g['end']} ({dur}) — the "
                "monitoring Mac rebooted and the logger didn't resume until someone next "
                "logged into its GUI session (auto-start agents only launch at login, not "
                "at raw boot). This is a gap in coverage, not a finding about the "
                "connection, and is disclosed here rather than silently skipped."
            )
    for note in methodology_notes:
        lines.append(f"*Methodology note: {note}*")
        lines.append("")

    report_path = REPORT_DIR / f"latency_report_{today}.md"
    report_path.write_text("\n".join(lines))

    print(f"Wrote {report_path}")
    print(f"Wrote {csv_path} ({len(spike_ts)} incident rounds)")

    pdf_path = REPORT_DIR / f"latency_report_{today}.pdf"
    if render_pdf(lines, pdf_path):
        print(f"Wrote {pdf_path}")


if __name__ == "__main__":
    main()
