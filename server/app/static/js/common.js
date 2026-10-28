/* Shared helpers (no inline scripts: the Content-Security-Policy forbids them). */
window.TM = (function () {
  const tz = document.body.dataset.timezone || "UTC";
  const fullFmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function fmtTime(isoOrMs) {
    if (isoOrMs == null || isoOrMs === "") return "—";
    const d = new Date(isoOrMs);
    if (isNaN(d)) return "—";
    const p = Object.fromEntries(fullFmt.formatToParts(d).map((x) => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day} ${p.hour === "24" ? "00" : p.hour}:${p.minute}:${p.second}`;
  }

  function ago(iso) {
    if (!iso) return "never";
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 60) return s + " s ago";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min ago";
    return Math.floor(s / 86400) + " d ago";
  }

  function badge(status) {
    return `<span class="badge text-bg-${esc(status.level)} status-badge" title="${esc(status.detail)}">` +
      `${esc(status.icon)} ${esc(status.label)}</span>`;
  }

  function onoff(value, onLevel) {
    if (value == null) return '<span class="text-muted">—</span>';
    return value ? `<span class="badge text-bg-${onLevel || "primary"}">ON</span>`
      : '<span class="badge text-bg-light border">OFF</span>';
  }

  function temp(value) {
    return value == null ? "—" : Number(value).toFixed(1) + " °C";
  }

  async function getJson(url) {
    const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (response.redirected && response.url.includes("/login")) {
      window.location.reload();  // session expired
      throw new Error("session expired");
    }
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.json();
  }

  document.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-copy]");
    if (!btn) return;
    const input = document.querySelector(btn.dataset.copy);
    if (!input) return;
    input.select();
    (navigator.clipboard ? navigator.clipboard.writeText(input.value) : Promise.reject())
      .catch(() => document.execCommand("copy"))
      .finally(() => { btn.textContent = "Copied"; });
  });

  return { tz, esc, fmtTime, ago, badge, onoff, temp, getJson };
})();
