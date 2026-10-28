/* Temperature monitoring page: trend chart, statistics, gaps and recent readings. */
(function () {
  const cfgEl = document.getElementById("monitor-config");
  if (!cfgEl || typeof Chart === "undefined") return;
  const cfg = cfgEl.dataset;
  const { esc, fmtTime, badge, onoff, temp, getJson, tz } = window.TM;
  const select = document.getElementById("mon-controller");
  const custom = document.getElementById("mon-custom");
  const auto = document.getElementById("mon-auto");
  let range = "24h";
  let timer = null;

  const tickFmt = (span) => new Intl.DateTimeFormat("en-GB", span > 86400000
    ? { timeZone: tz, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
    : { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false });
  let fmt = tickFmt(0);

  const chart = new Chart(document.getElementById("mon-chart"), {
    type: "line",
    data: { datasets: [
      { label: "Temperature °C", data: [], borderColor: "#1f77b4", backgroundColor: "#1f77b4", borderWidth: 1.6,
        pointRadius: 0, spanGaps: false },
      { label: "Set point °C", data: [], borderColor: "#6c757d", borderDash: [6, 4], borderWidth: 1.2, pointRadius: 0,
        stepped: true, spanGaps: false },
      { label: "Fault change", type: "scatter", data: [], pointStyle: "triangle", pointRadius: 6,
        backgroundColor: "#d62728", borderColor: "#d62728" },
      { label: "Failed poll", type: "scatter", data: [], pointStyle: "crossRot", pointRadius: 4,
        borderColor: "#6c757d", backgroundColor: "#6c757d" },
    ] },
    options: {
      animation: false, parsing: false, normalized: true, maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: { type: "linear", bounds: "data", ticks: { maxTicksLimit: 10, callback: (v) => fmt.format(new Date(v)) } },
        y: { title: { display: true, text: "°C" } },
      },
      plugins: {
        tooltip: { callbacks: {
          title: (items) => items.length ? fmtTime(items[0].parsed.x) : "",
          label: (item) => item.dataset.label === "Failed poll" ? "Failed poll (no value)"
            : item.dataset.label === "Fault change" ? "Faults: " + (item.raw.codes || "")
            : `${item.dataset.label}: ${item.parsed.y == null ? "—" : item.parsed.y.toFixed(1)}`,
        } },
        legend: { position: "bottom" },
      },
    },
  });

  function params() {
    const p = new URLSearchParams({ controller: select.value, range });
    if (range === "custom") {
      p.set("start", document.getElementById("mon-start").value);
      p.set("end", document.getElementById("mon-end").value);
    }
    return p;
  }

  function stat(label, value, extra) {
    return `<div class="col-6 col-md-3 col-xl"><div class="card"><div class="card-body py-2">
      <div class="summary-value">${esc(value)}</div><div class="small text-muted">${esc(label)}${extra ? " · " + esc(extra) : ""}</div></div></div></div>`;
  }

  function render(data) {
    const pts = data.points;
    const xMin = Date.parse(data.range.start), xMax = Date.parse(data.range.end);
    fmt = tickFmt(xMax - xMin);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.data.datasets[0].data = pts.map((p) => ({ x: p[0], y: p[1] }));
    chart.data.datasets[1].data = pts.map((p) => ({ x: p[0], y: p[2] }));
    chart.data.datasets[2].data = data.faults.map((f) => ({ x: f[0], y: f[1], codes: f[2] })).filter((f) => f.y != null);
    const values = pts.map((p) => p[1]).filter((v) => v != null);
    const floor = values.length ? Math.min(...values) - 1 : 0;
    chart.data.datasets[3].data = data.comm_errors.map((c) => ({ x: c[0], y: floor }));
    chart.update();

    const st = data.stats;
    document.getElementById("mon-stats").innerHTML =
      stat("Minimum", temp(st.min_c)) + stat("Maximum", temp(st.max_c)) + stat("Average", temp(st.avg_c)) +
      stat("Readings", st.readings, st.valid_temperatures + " valid") +
      stat("Failed polls", st.comm_failures) + stat("With faults", st.readings_with_faults) +
      stat("Gaps", st.gaps, st.gap_seconds ? Math.round(st.gap_seconds / 60) + " min total" : "");

    const c = data.controller;
    document.getElementById("mon-current").innerHTML =
      `${badge(c.status)} ${c.simulated ? '<span class="badge text-bg-info">SIMULATED</span>' : ""}
       <span class="fs-4 fw-bold ${c.live ? "" : "text-muted text-decoration-line-through"}">${esc(temp(c.temperature_c))}</span>
       <span>SP ${esc(temp(c.setpoint_c))}</span><span>Compressor ${onoff(c.compressor_on)}</span>
       <span>Alarm ${onoff(c.alarm_on, "danger")}</span>
       <span class="small text-muted">Last reading ${esc(fmtTime(c.last_reading_at))}${c.live ? "" : " – NOT LIVE"}</span>
       ${c.status.detail ? `<span class="small text-muted">${esc(c.status.detail)}</span>` : ""}`;

    document.getElementById("mon-chart-note").textContent =
      `${data.range.start_local} – ${data.range.end_local} (${data.timezone}) · ${data.count} readings` +
      (data.decimated ? " · chart shows min/max per time bucket" : "") +
      ` · gap = more than ${Math.round(data.gap_threshold_s)} s without a reading`;

    document.querySelector("#mon-gaps tbody").innerHTML = data.gaps.length ? data.gaps.map((g) =>
      `<tr><td>${esc(fmtTime(g.start))}</td><td>${esc(fmtTime(g.end))}</td><td>${Math.round(g.duration_s / 60)} min</td></tr>`).join("")
      : '<tr><td colspan="3" class="text-muted">No gaps in this period.</td></tr>';

    document.querySelector("#mon-recent tbody").innerHTML = data.recent.length ? data.recent.map((r) =>
      `<tr><td class="text-nowrap">${esc(r.time)}${r.backfilled ? ' <span class="badge text-bg-secondary">buffered</span>' : ""}</td>
       <td>${esc(temp(r.temperature_c))}</td><td>${esc(temp(r.setpoint_c))}</td><td>${onoff(r.compressor_on)}</td>
       <td>${onoff(r.alarm_on, "danger")}</td><td>${r.faults.map((f) => `<span class="badge text-bg-danger">${esc(f)}</span>`).join(" ")}</td>
       <td>${r.comm_status === "ok" ? "ok" : `<span class="badge text-bg-danger">${esc(r.comm_status)}</span>`}</td>
       <td>${esc(r.quality)}${r.source === "simulated" ? ' <span class="badge text-bg-info">SIM</span>' : ""}</td></tr>`).join("")
      : '<tr><td colspan="8" class="text-muted">No readings in this period.</td></tr>';

    const logs = new URLSearchParams({ controller: select.value });
    document.getElementById("mon-logs-link").href = cfg.logsUrl + "?" + logs.toString();
  }

  async function load() {
    try {
      render(await getJson(cfg.api + "?" + params().toString()));
    } catch (e) {
      document.getElementById("mon-chart-note").textContent = "⚠ Could not load data: " + e.message;
    }
  }

  function schedule() {
    clearInterval(timer);
    if (auto.checked && range !== "custom") timer = setInterval(load, Math.max(2, Number(cfg.refresh)) * 1000);
  }

  document.getElementById("mon-ranges").addEventListener("click", (event) => {
    const btn = event.target.closest("[data-range]");
    if (!btn) return;
    range = btn.dataset.range;
    document.querySelectorAll("#mon-ranges button").forEach((b) => b.classList.toggle("active", b === btn));
    custom.classList.toggle("d-none", range !== "custom");
    if (range !== "custom") { load(); }
    schedule();
  });
  document.getElementById("mon-apply").addEventListener("click", load);
  select.addEventListener("change", load);
  auto.addEventListener("change", schedule);
  load();
  schedule();
})();
