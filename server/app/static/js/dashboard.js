/* Dashboard: renders controller cards and node table; refreshes without page reloads. */
(function () {
  const cfgEl = document.getElementById("dashboard-config");
  if (!cfgEl) return;
  const cfg = cfgEl.dataset;
  const { esc, fmtTime, ago, badge, onoff, temp, getJson } = window.TM;
  let latest = JSON.parse(document.getElementById("dashboard-data").textContent);

  function link(template, id) { return template.replace(/0$/, String(id)); }

  function controllerCard(c) {
    const s = c.status;
    const noValue = s.code === "probe_fault" || s.code === "invalid";
    const outdated = !c.live && !noValue;
    const reading = s.code === "probe_fault" ? "PROBE FAULT" : s.code === "invalid" ? "INVALID" : temp(c.temperature_c);
    let caveat = "";
    if (noValue) {
      caveat = '<div class="small fw-semibold text-danger">⚠ No valid temperature in the latest reading</div>';
    } else if (outdated) {
      const what = s.code === "comm_failure" ? "Last good value " + ago(c.last_good_at)
        : s.code === "no_data" ? "Waiting for first reading" : "Not live – last reading " + ago(c.last_reading_at);
      caveat = `<div class="small fw-semibold text-danger-emphasis">⌛ ${esc(what)}</div>`;
    }
    const faults = c.faults.length
      ? c.faults.map((f) => `<span class="badge text-bg-danger me-1">${esc(f)}</span>`).join("")
      : '<span class="text-muted small">No active faults</span>';
    const title = cfg.canNodes === "1"
      ? `<a href="${link(cfg.controllerUrl, c.id)}" class="fw-semibold">${esc(c.name)}</a>`
      : `<span class="fw-semibold">${esc(c.name)}</span>`;
    return `
    <div class="col-sm-6 col-lg-4 col-xxl-3">
      <div class="card ctrl-card h-100 border-${esc(s.level)} ${outdated ? "not-live" : ""}">
        <div class="card-header py-2 d-flex justify-content-between align-items-start gap-2">
          <div>${title}<div class="small text-muted">${esc(c.node_name)}${c.site ? " · " + esc(c.site) : ""}</div></div>
          <div class="text-end">${badge(s)}${c.simulated ? '<div><span class="badge text-bg-info mt-1">SIMULATED</span></div>' : ""}</div>
        </div>
        <div class="card-body py-2">
          <div class="state-label">Temperature</div>
          <div class="temp-value ${noValue ? "text-danger" : ""}" aria-label="Temperature">${esc(reading)}</div>
          ${caveat}
          <div class="row small g-1 mt-1">
            <div class="col-6"><span class="state-label">Set point</span><br>${esc(temp(c.setpoint_c))}</div>
            <div class="col-3"><span class="state-label">Comp.</span><br>${onoff(c.compressor_on)}</div>
            <div class="col-3"><span class="state-label">Alarm</span><br>${onoff(c.alarm_on, "danger")}</div>
          </div>
          <div class="mt-2">${faults}${c.defrost_active ? '<span class="badge text-bg-info">Defrost</span>' : ""}</div>
          ${s.detail && (outdated || noValue) ? `<div class="small text-muted mt-1">${esc(s.detail)}</div>` : ""}
        </div>
        <div class="card-footer small text-muted py-1 d-flex justify-content-between">
          <span title="${esc(fmtTime(c.last_reading_at))}">Reading: ${esc(c.last_reading_at ? ago(c.last_reading_at) : "none")}</span>
          <a href="${esc(cfg.monitorUrl)}?controller=${c.id}">Trend ›</a>
        </div>
      </div>
    </div>`;
  }

  function nodeRow(n) {
    const name = cfg.canNodes === "1" ? `<a href="${link(cfg.nodeUrl, n.id)}">${esc(n.name)}</a>` : esc(n.name);
    const clock = n.clock_synchronized === false ? '<span class="badge text-bg-danger">not synced</span>'
      : n.clock_synchronized ? "synced" : "—";
    return `<tr><td>${name} <small class="text-muted">${esc(n.uid)}</small>${n.simulation ? ' <span class="badge text-bg-info">SIMULATED</span>' : ""}</td>
      <td>${esc(n.site)}</td><td>${badge(n.status)}</td>
      <td title="${esc(fmtTime(n.last_seen_at))}">${esc(n.last_seen_at ? ago(n.last_seen_at) : "never")}</td>
      <td title="${esc(fmtTime(n.last_upload_at))}">${esc(n.last_upload_at ? ago(n.last_upload_at) : "never")}</td>
      <td>${n.queue_pending == null ? "—" : esc(n.queue_pending)}</td>
      <td>${n.config_version == null ? "—" : "v" + esc(n.config_version)}</td>
      <td>${esc(n.software_version || "—")}</td><td>${clock}</td></tr>`;
  }

  function render(data) {
    latest = data;
    document.querySelectorAll("#summary-cards [data-key]").forEach((el) => {
      el.textContent = data.summary[el.dataset.key];
    });
    const cards = document.getElementById("controller-cards");
    cards.innerHTML = data.controllers.length ? data.controllers.map(controllerCard).join("")
      : '<div class="col"><div class="alert alert-info">No controllers yet. Register a node and wait for its first readings.</div></div>';
    document.querySelector("#node-table tbody").innerHTML = data.nodes.length ? data.nodes.map(nodeRow).join("")
      : '<tr><td colspan="9" class="text-muted">No nodes registered.</td></tr>';
    document.getElementById("dash-updated").textContent = fmtTime(data.generated_at);
  }

  async function refresh() {
    try {
      render(await getJson(cfg.api));
      document.getElementById("dash-error").classList.add("d-none");
    } catch (e) {
      document.getElementById("dash-error").classList.remove("d-none");
    }
  }

  render(latest);
  setInterval(refresh, Math.max(2, Number(cfg.refresh || 10)) * 1000);
})();
