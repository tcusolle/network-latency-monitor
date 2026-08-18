// ISP Latency Monitor dashboard.
// Polls /api/pings (served straight from latency.db by server.py) and
// renders a hand-rolled SVG line chart: no CDN, so it still works when
// the connection under test is flaky.

const SERIES = [
  { key: "modem_first_hop", name: "Modem (first hop)", varName: "--series-modem" },
  { key: "cloudflare", name: "Cloudflare (1.1.1.1)", varName: "--series-cf" },
  { key: "google_dns", name: "Google DNS (8.8.8.8)", varName: "--series-google" },
];

const REF_LINE_MS = 50; // neutral marker, not a verdict -- just a stable reference

// Status badge thresholds -- the single source of truth for classifyStatus()
// and the on-page legend, so the two can't drift apart.
const THRESHOLDS = {
  goodAvgMs: 30, // avg <= this AND 0% loss -> good
  warnAvgMs: 80, // avg above this (or any loss) -> warning; above this (or loss over critLossPct) -> critical
  critLossPct: 2,
};

const state = {
  rounds: [], // [{ ts, tsDate, values: { label: { rtt, status } } }]
  domain: { type: "preset", minutes: 1440 },
  lastPreset: { type: "preset", minutes: 1440 },
  visible: { modem_first_hop: true, cloudflare: true, google_dns: true },
  tableView: false,
  dragging: null,
};

const el = (id) => document.getElementById(id);
const svgNS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const node = document.createElementNS(svgNS, tag);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  return node;
}
function cssVar(name) {
  return getComputedStyle(el("app")).getPropertyValue(name).trim();
}

// ---------- data ----------

async function fetchRows() {
  const res = await fetch("/api/pings?limit=200000");
  if (!res.ok) throw new Error("fetch failed: " + res.status);
  return res.json();
}

function groupIntoRounds(rows) {
  const map = new Map();
  for (const r of rows) {
    let round = map.get(r.ts);
    if (!round) {
      round = { ts: r.ts, tsDate: new Date(r.ts), values: {} };
      map.set(r.ts, round);
    }
    round.values[r.label] = { rtt: r.rtt_ms, status: r.status };
  }
  return Array.from(map.values()).sort((a, b) => a.tsDate - b.tsDate);
}

function computeStats(rounds) {
  const out = {};
  for (const s of SERIES) out[s.key] = { total: 0, ok: 0, sum: 0, max: 0, vals: [], last: null, lastStatus: null };
  for (const round of rounds) {
    for (const s of SERIES) {
      const v = round.values[s.key];
      if (!v) continue;
      const st = out[s.key];
      st.total++;
      if (v.status === "ok" && v.rtt != null) {
        st.ok++;
        st.sum += v.rtt;
        st.max = Math.max(st.max, v.rtt);
        st.vals.push(v.rtt);
      }
      st.last = v.rtt;
      st.lastStatus = v.status;
    }
  }
  for (const s of SERIES) {
    const st = out[s.key];
    st.avg = st.ok ? st.sum / st.ok : null;
    st.vals.sort((a, b) => a - b);
    st.p95 = st.vals.length ? st.vals[Math.floor(0.95 * (st.vals.length - 1))] : null;
    st.lossPct = st.total ? 100 * (1 - st.ok / st.total) : 0;
  }
  return out;
}

function classifyStatus(st) {
  if (st.total === 0) return "warning";
  if (st.lossPct > THRESHOLDS.critLossPct || (st.avg != null && st.avg > THRESHOLDS.warnAvgMs)) return "critical";
  if (st.lossPct > 0 || (st.avg != null && st.avg > THRESHOLDS.goodAvgMs)) return "warning";
  return "good";
}

function renderStatusLegend() {
  const wrap = el("status-legend");
  wrap.innerHTML = "";
  const entries = [
    { cls: "good", label: "Good", desc: `avg ≤ ${THRESHOLDS.goodAvgMs}ms, 0% loss` },
    { cls: "warning", label: "Elevated", desc: `avg ${THRESHOLDS.goodAvgMs}–${THRESHOLDS.warnAvgMs}ms, or any loss up to ${THRESHOLDS.critLossPct}%` },
    { cls: "critical", label: "High", desc: `avg > ${THRESHOLDS.warnAvgMs}ms, or loss > ${THRESHOLDS.critLossPct}%` },
  ];
  for (const e of entries) {
    const item = document.createElement("div");
    item.className = "item";
    const dot = document.createElement("span");
    dot.className = "dot " + e.cls;
    const label = document.createElement("span");
    label.className = "crit-label";
    label.textContent = e.label;
    const desc = document.createElement("span");
    desc.className = "desc";
    desc.textContent = e.desc;
    item.append(dot, label, desc);
    wrap.appendChild(item);
  }
}

// ---------- stat tiles ----------

function metricEl(value, label) {
  const m = document.createElement("div");
  m.className = "metric";
  const v = document.createElement("div");
  v.className = "v";
  v.textContent = value;
  const l = document.createElement("div");
  l.className = "l";
  l.textContent = label;
  m.append(v, l);
  return m;
}

function renderStats(stats) {
  const row = el("stats-row");
  row.innerHTML = "";
  for (const s of SERIES) {
    const st = stats[s.key];
    const statusKey = classifyStatus(st);
    const label = statusKey === "good" ? "Good" : statusKey === "warning" ? "Elevated" : "High";

    const tile = document.createElement("div");
    tile.className = "stat-tile";

    const head = document.createElement("div");
    head.className = "tile-head";
    const name = document.createElement("div");
    name.className = "tile-name";
    name.textContent = s.name;
    const badge = document.createElement("div");
    badge.className = "status-badge status-" + statusKey;
    const dot = document.createElement("span");
    dot.className = "dot " + statusKey;
    badge.append(dot, document.createTextNode(label));
    head.append(name, badge);

    const metrics = document.createElement("div");
    metrics.className = "tile-metrics";
    metrics.append(
      metricEl(st.last != null ? st.last.toFixed(0) + " ms" : st.lastStatus === "timeout" ? "timeout" : "—", "last"),
      metricEl(st.avg != null ? st.avg.toFixed(1) + " ms" : "—", "avg"),
      metricEl(st.p95 != null ? st.p95.toFixed(0) + " ms" : "—", "p95"),
      metricEl(st.lossPct.toFixed(1) + "%", "loss")
    );

    tile.append(head, metrics);
    row.appendChild(tile);
  }
}

// ---------- chart ----------

function effectiveXDomain() {
  if (!state.rounds.length) {
    const now = Date.now();
    return [now - 3600000, now];
  }
  const latest = state.rounds[state.rounds.length - 1].tsDate.getTime();
  const earliest = state.rounds[0].tsDate.getTime();
  if (state.domain.type === "custom") return [state.domain.x0, state.domain.x1];
  if (state.domain.type === "all") return [earliest, latest];
  const span = state.domain.minutes * 60000;
  return [Math.max(earliest, latest - span), latest];
}

function effectiveYDomain(xDomain) {
  let max = 0;
  for (const r of state.rounds) {
    const t = r.tsDate.getTime();
    if (t < xDomain[0] || t > xDomain[1]) continue;
    for (const s of SERIES) {
      if (!state.visible[s.key]) continue;
      const v = r.values[s.key];
      if (v && v.status === "ok" && v.rtt != null) max = Math.max(max, v.rtt);
    }
  }
  const top = Math.max(max * 1.2, REF_LINE_MS * 1.2, 10);
  return [0, top];
}

function niceTicks(domainMax, count) {
  const step = domainMax / count;
  const ticks = [];
  for (let i = 0; i <= count; i++) ticks.push(Math.round((step * i) * 10) / 10);
  return ticks;
}

function formatTimeTick(t, spanMs) {
  const d = new Date(t);
  if (spanMs <= 2 * 3600000) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  if (spanMs <= 3 * 86400000) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function buildPath(xDomain, key, xScale, yScale) {
  let d = "";
  let started = false;
  for (const r of state.rounds) {
    const t = r.tsDate.getTime();
    if (t < xDomain[0] || t > xDomain[1]) continue;
    const v = r.values[key];
    if (v && v.status === "ok" && v.rtt != null) {
      const x = xScale(t).toFixed(1);
      const y = yScale(v.rtt).toFixed(1);
      d += (started ? "L" : "M") + x + "," + y + " ";
      started = true;
    } else {
      started = false;
    }
  }
  return d.trim();
}

function nearestRound(t) {
  const rounds = state.rounds;
  if (!rounds.length) return null;
  let lo = 0, hi = rounds.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (rounds[mid].tsDate.getTime() < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0) {
    const before = rounds[lo - 1], after = rounds[lo];
    if (Math.abs(before.tsDate - t) <= Math.abs(after.tsDate - t)) return before;
  }
  return rounds[lo];
}

function renderChart() {
  const svg = el("chart");
  svg.innerHTML = "";
  const wrapRect = el("chart-wrap").getBoundingClientRect();
  const width = Math.max(wrapRect.width, 320);
  const height = 420;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  if (!state.rounds.length) {
    const msg = svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "axis-label" });
    msg.textContent = "No data yet — start monitor.py to begin logging";
    svg.appendChild(msg);
    return;
  }

  const margin = { top: 16, right: 16, bottom: 40, left: 52 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const plotBottom = margin.top + plotHeight;

  const xDomain = effectiveXDomain();
  const yDomain = effectiveYDomain(xDomain);
  const span = xDomain[1] - xDomain[0] || 1;

  const xScale = (t) => margin.left + ((t - xDomain[0]) / span) * plotWidth;
  const xInvert = (px) => xDomain[0] + ((px - margin.left) / plotWidth) * span;
  const yScale = (v) => margin.top + plotHeight - ((v - yDomain[0]) / (yDomain[1] - yDomain[0])) * plotHeight;

  // gridlines + y ticks
  const yTicks = niceTicks(yDomain[1], 4);
  for (const tick of yTicks) {
    const y = yScale(tick);
    svg.appendChild(svgEl("line", { x1: margin.left, x2: margin.left + plotWidth, y1: y.toFixed(1), y2: y.toFixed(1), class: "gridline" }));
    const label = svgEl("text", { x: margin.left - 8, y: (y + 3).toFixed(1), "text-anchor": "end", class: "axis-label" });
    label.textContent = tick + " ms";
    svg.appendChild(label);
  }

  // reference line
  if (REF_LINE_MS <= yDomain[1]) {
    const y = yScale(REF_LINE_MS);
    svg.appendChild(svgEl("line", { x1: margin.left, x2: margin.left + plotWidth, y1: y.toFixed(1), y2: y.toFixed(1), class: "refline" }));
    const label = svgEl("text", { x: margin.left + plotWidth - 4, y: (y - 4).toFixed(1), "text-anchor": "end", class: "refline-label" });
    label.textContent = `reference ${REF_LINE_MS}ms`;
    svg.appendChild(label);
  }

  // x ticks
  const xTickCount = Math.min(6, state.rounds.length);
  for (let i = 0; i <= xTickCount; i++) {
    const t = xDomain[0] + (span * i) / xTickCount;
    const x = xScale(t);
    svg.appendChild(svgEl("line", { x1: x.toFixed(1), x2: x.toFixed(1), y1: margin.top, y2: plotBottom, class: "gridline" }));
    const label = svgEl("text", { x: x.toFixed(1), y: plotBottom + 32, "text-anchor": "middle", class: "axis-label" });
    label.textContent = formatTimeTick(t, span);
    svg.appendChild(label);
  }
  svg.appendChild(svgEl("line", { x1: margin.left, x2: margin.left + plotWidth, y1: plotBottom, y2: plotBottom, class: "baseline" }));

  // loss ticks (any target timed out that round)
  for (const r of state.rounds) {
    const t = r.tsDate.getTime();
    if (t < xDomain[0] || t > xDomain[1]) continue;
    const anyLoss = SERIES.some((s) => state.visible[s.key] && r.values[s.key] && r.values[s.key].status !== "ok");
    if (anyLoss) {
      const x = xScale(t);
      const tick = svgEl("line", { x1: x.toFixed(1), x2: x.toFixed(1), y1: plotBottom + 4, y2: plotBottom + 11, "stroke-width": 2 });
      tick.style.stroke = "var(--status-critical)";
      svg.appendChild(tick);
    }
  }

  // series lines
  for (const s of SERIES) {
    if (!state.visible[s.key]) continue;
    const d = buildPath(xDomain, s.key, xScale, yScale);
    if (!d) continue;
    const path = svgEl("path", { d, class: "series-line" });
    path.style.stroke = `var(${s.varName})`;
    svg.appendChild(path);
  }

  // interaction layer: crosshair + drag-to-zoom
  const hitRect = svgEl("rect", { x: margin.left, y: margin.top, width: plotWidth, height: plotHeight, fill: "transparent", cursor: "crosshair" });
  const crosshair = svgEl("line", { y1: margin.top, y2: plotBottom, class: "crosshair-line", style: "display:none" });
  const zoomRect = svgEl("rect", { y: margin.top, height: plotHeight, class: "zoom-rect", style: "display:none" });
  svg.append(hitRect, crosshair, zoomRect);

  const tooltip = el("tooltip");

  function pointerX(evt) {
    const rect = svg.getBoundingClientRect();
    const scaleX = width / rect.width;
    return (evt.clientX - rect.left) * scaleX;
  }

  hitRect.addEventListener("pointermove", (evt) => {
    const px = Math.min(Math.max(pointerX(evt), margin.left), margin.left + plotWidth);
    if (state.dragging) {
      const x0 = Math.min(state.dragging.startPx, px);
      const x1 = Math.max(state.dragging.startPx, px);
      zoomRect.setAttribute("x", x0.toFixed(1));
      zoomRect.setAttribute("width", (x1 - x0).toFixed(1));
      zoomRect.style.display = "block";
      return;
    }
    const round = nearestRound(xInvert(px));
    if (!round) return;
    const x = xScale(round.tsDate.getTime());
    crosshair.setAttribute("x1", x.toFixed(1));
    crosshair.setAttribute("x2", x.toFixed(1));
    crosshair.style.display = "block";

    tooltip.innerHTML = "";
    const timeRow = document.createElement("div");
    timeRow.className = "tt-time";
    timeRow.textContent = round.tsDate.toLocaleString();
    tooltip.appendChild(timeRow);
    for (const s of SERIES) {
      if (!state.visible[s.key]) continue;
      const v = round.values[s.key];
      const row = document.createElement("div");
      row.className = "tt-row";
      const k = document.createElement("div");
      k.className = "k";
      const key = document.createElement("span");
      key.className = "key";
      key.style.background = `var(${s.varName})`;
      const nameSpan = document.createElement("span");
      nameSpan.textContent = s.name;
      k.append(key, nameSpan);
      const valSpan = document.createElement("div");
      valSpan.className = "v";
      valSpan.textContent = v ? (v.status === "ok" && v.rtt != null ? v.rtt.toFixed(0) + " ms" : v.status) : "—";
      row.append(k, valSpan);
      tooltip.appendChild(row);
    }
    const rect = svg.getBoundingClientRect();
    const scaleX = rect.width / width;
    const left = rect.left + x * scaleX;
    tooltip.style.display = "block";
    tooltip.style.left = Math.min(left + 14, window.innerWidth - 220) + "px";
    tooltip.style.top = rect.top + window.scrollY + margin.top + "px";
  });

  hitRect.addEventListener("pointerleave", () => {
    if (state.dragging) return;
    crosshair.style.display = "none";
    tooltip.style.display = "none";
  });

  hitRect.addEventListener("pointerdown", (evt) => {
    const px = Math.min(Math.max(pointerX(evt), margin.left), margin.left + plotWidth);
    state.dragging = { startPx: px };
    hitRect.setPointerCapture(evt.pointerId);
  });

  hitRect.addEventListener("pointerup", (evt) => {
    if (!state.dragging) return;
    const px = Math.min(Math.max(pointerX(evt), margin.left), margin.left + plotWidth);
    const startPx = state.dragging.startPx;
    const dx = Math.abs(px - startPx);
    state.dragging = null;
    zoomRect.style.display = "none";
    if (dx > 6) {
      const x0 = xInvert(Math.min(px, startPx));
      const x1 = xInvert(Math.max(px, startPx));
      state.domain = { type: "custom", x0, x1 };
      setActivePreset(-1);
      el("reset-zoom").classList.add("visible");
      renderChart();
    }
  });

  hitRect.addEventListener("dblclick", () => {
    state.domain = state.lastPreset;
    el("reset-zoom").classList.remove("visible");
    setActivePreset(state.domain.type === "all" ? 0 : state.domain.minutes);
    renderChart();
  });
}

// ---------- legend ----------

function renderLegend() {
  const legend = el("legend");
  legend.innerHTML = "";
  for (const s of SERIES) {
    const item = document.createElement("div");
    item.className = "legend-item" + (state.visible[s.key] ? "" : " dim");
    const key = document.createElement("span");
    key.className = "key";
    key.style.background = `var(${s.varName})`;
    const label = document.createElement("span");
    label.textContent = s.name;
    item.append(key, label);
    item.addEventListener("click", () => {
      state.visible[s.key] = !state.visible[s.key];
      renderLegend();
      renderChart();
    });
    legend.appendChild(item);
  }
}

// ---------- table ----------

function renderTable() {
  const body = el("data-table-body");
  body.innerHTML = "";
  const rows = state.rounds.slice(-500).reverse();
  for (const r of rows) {
    const tr = document.createElement("tr");
    const cells = [r.tsDate.toLocaleString()];
    for (const s of SERIES) {
      const v = r.values[s.key];
      cells.push(v ? (v.status === "ok" && v.rtt != null ? v.rtt.toFixed(0) + " ms" : v.status) : "—");
    }
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  el("table-note").textContent = `Showing latest ${rows.length} of ${state.rounds.length} rounds.`;
}

// ---------- controls ----------

function setActivePreset(minutes) {
  document.querySelectorAll(".preset-btn").forEach((btn) => {
    btn.classList.toggle("active", Number(btn.dataset.preset) === minutes);
  });
}

document.querySelectorAll(".preset-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const minutes = Number(btn.dataset.preset);
    state.domain = minutes === 0 ? { type: "all" } : { type: "preset", minutes };
    state.lastPreset = state.domain;
    setActivePreset(minutes);
    el("reset-zoom").classList.remove("visible");
    renderChart();
  });
});

el("reset-zoom").addEventListener("click", () => {
  state.domain = state.lastPreset;
  el("reset-zoom").classList.remove("visible");
  setActivePreset(state.domain.type === "all" ? 0 : state.domain.minutes);
  renderChart();
});

el("toggle-table").addEventListener("click", () => {
  state.tableView = !state.tableView;
  el("table-card").classList.toggle("visible", state.tableView);
  el("toggle-table").textContent = state.tableView ? "Hide table" : "View as table";
  if (state.tableView) renderTable();
});

// ---------- refresh loop ----------

async function refresh() {
  try {
    const rows = await fetchRows();
    state.rounds = groupIntoRounds(rows);
    el("conn-note").textContent = state.rounds.length
      ? `${state.rounds.length} rounds logged · last update ${new Date().toLocaleTimeString()}`
      : "waiting for monitor.py to start logging…";
    renderStats(computeStats(state.rounds));
    renderLegend();
    renderChart();
    if (state.tableView) renderTable();
  } catch (e) {
    el("conn-note").textContent = "dashboard server unreachable — is server.py running?";
  }
}

window.addEventListener("resize", () => {
  if (state.rounds.length) renderChart();
});

renderStatusLegend();
refresh();
setInterval(refresh, 5000);
