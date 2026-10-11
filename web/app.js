"use strict";

const $ = (id) => document.getElementById(id);
let exports = [], today = "", activeJob = null, autoDownload = false;
let pollTimer = null;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "Please try again.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function formatDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return value || "—";
  const date = new Date(`${value}T12:00:00Z`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-GB", {day: "numeric", month: "short", year: "numeric", timeZone: "UTC"}).format(date);
}

function rangeLabel(start, end) {
  return start === end ? formatDate(start) : `${formatDate(start)} – ${formatDate(end)}`;
}

function quickRange(kind) {
  if (!today) return;
  const start = new Date(`${today}T12:00:00Z`);
  if (kind === "week") start.setUTCDate(start.getUTCDate() - 6);
  $("start-date").value = start.toISOString().slice(0, 10);
  $("end-date").value = today;
  document.querySelectorAll("[data-range]").forEach((button) => {
    const selected = button.dataset.range === kind;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  $("end-date").setCustomValidity("");
}

function setStatus(label, detail = "", mode = "idle", percent = null) {
  $("status-label").textContent = label;
  $("status-detail").textContent = detail;
  $("status-detail").hidden = !detail;
  $("collection-status").className = `collection-status ${mode}`;
  $("status-dot").className = `dot${mode === "idle" ? " neutral" : ""}`;
  $("progress-track").hidden = mode !== "running";
  $("progress-track").classList.toggle("indeterminate", percent === null);
  $("progress-fill").style.width = `${percent ?? 30}%`;
}

function setBusy(busy) {
  $("collect-button").disabled = busy;
  $("collect-button").querySelector("span").textContent = busy ? "Preparing CSV…" : "Generate & download CSV";
  ["start-date", "end-date", "export-select"].forEach((id) => { $(id).disabled = busy; });
  document.querySelectorAll("[data-range]").forEach((button) => { button.disabled = busy; });
  selectExport($("export-select").value);
}

// Only filenames and date ranges are used in the browser; no news rows are fetched.
function selectExport(filename) {
  const item = exports.find((item) => item.filename === filename);
  $("export-panel").hidden = !exports.length;
  $("download-button").hidden = !item || $("collect-button").disabled;
  if (!item) return;
  $("export-select").value = filename;
  $("range-label").textContent = rangeLabel(item.start, item.end);
  $("download-button").href = `/download/${encodeURIComponent(filename)}`;
  $("download-button").setAttribute("download", filename);
}

async function refreshExports(preferred = null) {
  const result = await api("/api/exports");
  exports = result.exports;
  today = result.today;
  const selected = preferred || $("export-select").value;
  const options = exports.map((item) => {
    const option = document.createElement("option");
    option.value = item.filename;
    option.textContent = rangeLabel(item.start, item.end);
    return option;
  });
  $("export-select").replaceChildren(...options);
  $("export-select").hidden = !exports.length;
  selectExport(exports.some((item) => item.filename === selected) ? selected : exports[0]?.filename);
  return result;
}

async function pollJob(jobId) {
  if (activeJob !== jobId) return;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (activeJob !== jobId) return;
    if (job.state === "running") {
      const percent = job.total ? Math.min(99, Math.round(job.current / job.total * 100)) : null;
      setStatus("Collection in progress", job.message, "running", percent);
      pollTimer = setTimeout(() => pollJob(jobId), 500);
      return;
    }
    activeJob = null;
    clearTimeout(pollTimer);
    setBusy(false);
    if (job.state === "failed") {
      autoDownload = false;
      setStatus("Could not generate CSV", job.message, "error");
      $("download-button").hidden = !$("export-select").value;
      return;
    }
    setStatus(job.state === "partial" ? "Some news was collected" : "Your CSV is ready",
              job.message, job.state === "partial" ? "warning" : "complete");
    await refreshExports(job.filename);
    selectExport(job.filename);
    if (job.state === "partial") {
      setStatus("Partial CSV available", "Some items could not be read. Download the available rows or generate the range again.", "warning");
    } else if (autoDownload && !$("download-button").hidden) {
      $("download-button").click();
    }
    autoDownload = false;
  } catch (error) {
    if (activeJob !== jobId) {
      autoDownload = false;
      setStatus("Could not refresh saved downloads", "Refresh the page to download your generated CSV.", "error");
      return;
    }
    if (error.status === 404) {
      activeJob = null;
      autoDownload = false;
      setBusy(false);
      setStatus("Collection unavailable", "Generate the CSV again to start a new collection.", "error");
      return;
    }
    setStatus("Reconnecting…", "The collection continues in the background. Checking again shortly.", "running");
    pollTimer = setTimeout(() => pollJob(jobId), 2500);
  }
}

$("collect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (activeJob) return;
  const start = $("start-date").value, end = $("end-date").value;
  $("end-date").setCustomValidity(start > end ? "The end date must be on or after the start date." : "");
  if (!$("collect-form").reportValidity()) return;
  setBusy(true);
  setStatus("Starting your collection", "Connecting to DSE…", "running");
  $("download-button").hidden = true;
  try {
    const job = await api("/api/scrape", {method: "POST", headers: {
      "Content-Type": "application/json", "X-Requested-With": "DSEWorkspace"
    }, body: JSON.stringify({start, end})});
    activeJob = job.id;
    autoDownload = true;
    await pollJob(activeJob);
  } catch (error) {
    autoDownload = false;
    setBusy(false);
    setStatus("Could not start collection", error.message, "error");
    $("download-button").hidden = !$("export-select").value;
  }
});

document.querySelectorAll("[data-range]").forEach((button) => button.addEventListener("click", () => quickRange(button.dataset.range)));
["start-date", "end-date"].forEach((id) => $(id).addEventListener("input", () => {
  $("end-date").setCustomValidity("");
  document.querySelectorAll("[data-range]").forEach((button) => {
    button.classList.remove("active"); button.setAttribute("aria-pressed", "false");
  });
}));
$("export-select").addEventListener("change", () => selectExport($("export-select").value));

(async function initialize() {
  try {
    const result = await refreshExports();
    quickRange("today");
    // A page refresh reconnects to a running job rather than launching a new one.
    if (result.active) {
      activeJob = result.active;
      setBusy(true);
      $("download-button").hidden = true;
      await pollJob(activeJob);
    }
  } catch (error) {
    today = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Dhaka", year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date());
    quickRange("today");
    setStatus("Connection unavailable", error.message, "error");
  }
})();
