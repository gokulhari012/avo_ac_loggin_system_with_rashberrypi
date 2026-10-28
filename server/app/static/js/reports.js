/* Reports page: show only the options relevant to the selected report type. */
(function () {
  const type = document.getElementById("r-type");
  if (!type) return;
  const help = {
    temperature_history: "Opens Data Logs with this period; filter there and export Excel/PDF.",
    excursions: "Enter a high limit, a low limit, or both.",
    downtime: "Downtime is inferred from missing heartbeats; communication events are recorded facts.",
    audit_trail: "All audit events matching the filters.",
  };
  function update() {
    document.querySelectorAll(".report-opt").forEach((el) => {
      el.classList.toggle("d-none", !el.dataset.for.split(" ").includes(type.value));
    });
    document.getElementById("report-help").textContent = help[type.value] || "";
  }
  type.addEventListener("change", update);
  update();
})();
