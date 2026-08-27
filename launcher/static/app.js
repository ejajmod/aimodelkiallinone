const ACTIVE_STATES = new Set(["queued", "downloading", "installing", "restarting"]);
const TERMINAL_STATES = new Set(["complete", "failed", "cancelled", "interrupted"]);

const grid = document.querySelector("#workflowGrid");
const count = document.querySelector("#workflowCount");
const notice = document.querySelector("#notice");
const panel = document.querySelector("#jobPanel");
const statusLabel = document.querySelector("#jobStatus");
const title = document.querySelector("#jobTitle");
const percent = document.querySelector("#jobPercent");
const progressBar = document.querySelector("#progressBar");
const message = document.querySelector("#jobMessage");
const stats = document.querySelector("#jobStats");
const files = document.querySelector("#jobFiles");
const cancelButton = document.querySelector("#cancelButton");
const comfyButton = document.querySelector("#comfyButton");
const toast = document.querySelector("#toast");
const themeToggle = document.querySelector("#themeToggle");
const themeLabel = themeToggle.querySelector(".theme-label");
const themeColor = document.querySelector('meta[name="theme-color"]');

const THEME_STORAGE_KEY = "aimodelki-allinone-theme";

function applyTheme(theme, persist = false) {
  const selectedTheme = theme === "light" ? "light" : "dark";
  const isDark = selectedTheme === "dark";
  document.documentElement.dataset.theme = selectedTheme;
  themeToggle.setAttribute("aria-checked", String(isDark));
  themeToggle.setAttribute("aria-label", isDark ? "Włącz tryb jasny" : "Włącz tryb ciemny");
  themeLabel.textContent = isDark ? "Tryb ciemny" : "Tryb jasny";
  themeColor.content = isDark ? "#0b0611" : "#ffffff";
  if (persist) {
    try { localStorage.setItem(THEME_STORAGE_KEY, selectedTheme); } catch (_) { /* storage may be unavailable */ }
  }
}

let savedTheme = null;
try { savedTheme = localStorage.getItem(THEME_STORAGE_KEY); } catch (_) { /* storage may be unavailable */ }
applyTheme(savedTheme || "dark");

themeToggle.addEventListener("click", () => {
  const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(nextTheme, true);
});

let workflows = [];
let currentJob = { status: "idle" };
let pollTimer = null;
let serviceUrls = { comfyui: "#", jupyter: "#" };

const query = new URLSearchParams(location.search);
if (query.has("token")) {
  sessionStorage.setItem("modelDockToken", query.get("token"));
  history.replaceState({}, "", location.pathname);
}

function headers() {
  const token = sessionStorage.getItem("modelDockToken");
  return token ? { "X-Launcher-Token": token } : {};
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
  if (!response.ok) {
    let detail = `Błąd ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* response was not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function formatBytes(value) {
  if (!value) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index > 2 ? 2 : 1)} ${units[index]}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;",
  }[character]));
}

function showToast(text) {
  toast.textContent = text;
  toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 4200);
}

function cardState(workflow) {
  if (workflow.installed) return "GOTOWY";
  if (!workflow.configured) return "KONFIGURACJA";
  return "ZAINSTALUJ";
}

function renderCards() {
  const busy = ACTIVE_STATES.has(currentJob.status);
  grid.innerHTML = workflows.map((workflow, index) => {
    const selected = currentJob.workflow_id === workflow.id;
    const disabled = busy || !workflow.configured;
    const missing = workflow.missing_env?.length ? `Brak: ${workflow.missing_env.join(", ")}` : "";
    return `
      <button class="workflow-card ${selected ? "active" : ""}" data-workflow="${escapeHtml(workflow.id)}"
        data-accent="${escapeHtml(workflow.accent)}" ${disabled ? "disabled" : ""}
        title="${escapeHtml(missing)}" aria-label="${escapeHtml(workflow.title)} — ${escapeHtml(cardState(workflow))}">
        <span class="card-top">
          <span class="package-number">${String(index + 1).padStart(2, "0")} / ${escapeHtml(workflow.category)}</span>
          <span class="package-state">${cardState(workflow)}</span>
        </span>
        <h3>${escapeHtml(workflow.title)}</h3>
        <p>${escapeHtml(workflow.description)}</p>
        <span class="card-bottom">
          <span>≈ ${workflow.estimated_size_gb.toFixed(1)} GB</span>
          <span class="card-arrow">↘</span>
        </span>
      </button>`;
  }).join("");

  grid.querySelectorAll("[data-workflow]").forEach((card) => {
    card.addEventListener("click", () => beginInstall(card.dataset.workflow));
  });
}

function statusText(status) {
  return {
    queued: "W KOLEJCE", downloading: "POBIERANIE", installing: "INSTALOWANIE",
    restarting: "RESTART COMFYUI", complete: "GOTOWE", failed: "BŁĄD",
    cancelled: "ZATRZYMANO", interrupted: "PRZERWANO",
  }[status] || "STATUS";
}

function renderJob(job) {
  currentJob = job || { status: "idle" };
  renderCards();
  if (!job || job.status === "idle") {
    panel.hidden = true;
    return;
  }

  panel.hidden = false;
  panel.classList.toggle("complete", job.status === "complete");
  panel.classList.toggle("failed", ["failed", "cancelled", "interrupted"].includes(job.status));
  statusLabel.innerHTML = `<i></i> ${statusText(job.status)}`;
  title.textContent = job.workflow_title || "Instalacja";
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  percent.textContent = `${progress}%`;
  progressBar.style.width = `${progress}%`;
  message.textContent = job.error || job.message || "Pracuję…";
  files.textContent = job.total_items ? `Element ${job.current_index || 0} z ${job.total_items}` : "";

  const fragments = [];
  if (job.downloaded_bytes && job.total_bytes) fragments.push(`${formatBytes(job.downloaded_bytes)} / ${formatBytes(job.total_bytes)}`);
  if (job.speed_bytes_per_second) fragments.push(`${formatBytes(job.speed_bytes_per_second)}/s`);
  if (job.current_item) fragments.push(job.current_item);
  stats.textContent = fragments.join("  ·  ");

  cancelButton.hidden = !ACTIVE_STATES.has(job.status);
  cancelButton.disabled = job.message?.startsWith("Zatrzymywanie");
  comfyButton.hidden = job.status !== "complete";
  if (TERMINAL_STATES.has(job.status)) stopPolling();
}

async function beginInstall(workflowId) {
  const workflow = workflows.find((item) => item.id === workflowId);
  if (!workflow || ACTIVE_STATES.has(currentJob.status)) return;
  try {
    const response = await api(`/api/install/${encodeURIComponent(workflowId)}`, { method: "POST" });
    renderJob(response.job);
    startPolling();
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) { showToast(error.message); }
}

async function refreshJob() {
  try { renderJob(await api("/api/jobs/current")); }
  catch (error) { showToast(error.message); stopPolling(); }
}

function startPolling() {
  stopPolling();
  pollTimer = window.setInterval(refreshJob, 800);
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = null;
}

cancelButton.addEventListener("click", async () => {
  try {
    const response = await api("/api/jobs/current/cancel", { method: "POST" });
    renderJob(response.job);
  } catch (error) { showToast(error.message); }
});

async function boot() {
  try {
    const data = await api("/api/bootstrap");
    workflows = data.workflows;
    serviceUrls = data.services;
    count.textContent = workflows.length;
    document.querySelector("#jupyterLink").href = serviceUrls.jupyter;
    document.querySelector("#comfyTopLink").href = serviceUrls.comfyui;
    comfyButton.href = serviceUrls.comfyui;
    if (data.catalog_error) {
      notice.hidden = false;
      notice.textContent = `Błąd katalogu: ${data.catalog_error}`;
    } else if (workflows.some((workflow) => !workflow.configured)) {
      notice.hidden = false;
      const unavailable = workflows
        .filter((workflow) => !workflow.configured)
        .map((workflow) => `${workflow.title}: ${workflow.missing_env.join(", ") || "brak konfiguracji"}`)
        .join("; ");
      notice.textContent = `Niektóre pakiety wymagają dodatkowej konfiguracji. ${unavailable}`;
    }
    renderJob(data.job);
    if (ACTIVE_STATES.has(data.job.status)) startPolling();
  } catch (error) {
    grid.innerHTML = "";
    notice.hidden = false;
    notice.textContent = error.message.includes("token") || error.message.includes("401")
      ? "Launcher jest chroniony. Otwórz adres ponownie z parametrem ?token=TWÓJ_TOKEN."
      : error.message;
  }
}

boot();
