const urlInput = document.querySelector("#urlInput");
const snatchButton = document.querySelector("#snatchButton");
const logToggleButton = document.querySelector("#logToggleButton");
const maxPagesInput = document.querySelector("#maxPagesInput");
const deepInput = document.querySelector("#deepInput");
const linksOnlyInput = document.querySelector("#linksOnlyInput");
const phaseText = document.querySelector("#phaseText");
const countText = document.querySelector("#countText");
const meterFill = document.querySelector("#meterFill");
const statusIcon = document.querySelector("#statusIcon");
const logSection = document.querySelector("#logSection");
const logBox = document.querySelector("#logBox");
const clearLogButton = document.querySelector("#clearLogButton");
const zipButton = document.querySelector("#zipButton");

let pollTimer = null;
let currentJobId = null;
let isRunning = false;

const snatchMarkup = `
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 3v12" />
    <path d="m7 10 5 5 5-5" />
    <path d="M5 18v3h14v-3" />
  </svg>
  <span>Snatch Images</span>
`;

const stopMarkup = `
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M7 7h10v10H7z" />
  </svg>
  <span>Stop Download</span>
`;

function setProgress(progress, saved, total) {
  const savedCount = Number(saved) || 0;
  const totalCount = Number(total) || 0;
  const percent =
    totalCount > 0
      ? savedCount >= totalCount
        ? 100
        : Math.floor((savedCount / totalCount) * 100)
      : 0;
  const isComplete = totalCount > 0 && savedCount >= totalCount;
  meterFill.style.width = `${percent}%`;
  countText.textContent = `${savedCount} / ${totalCount} ${percent}%`;
  meterFill.classList.toggle("is-complete", isComplete);
  countText.classList.toggle("is-complete", isComplete);
}

function setZipReady(url) {
  if (!url) {
    zipButton.classList.add("is-disabled");
    zipButton.setAttribute("aria-disabled", "true");
    zipButton.href = "#";
    return;
  }

  zipButton.classList.remove("is-disabled");
  zipButton.setAttribute("aria-disabled", "false");
  zipButton.href = url;
}

function setRunningState(running) {
  isRunning = running;
  urlInput.disabled = running;
  maxPagesInput.disabled = running;
  deepInput.disabled = running;
  linksOnlyInput.disabled = running;
  snatchButton.disabled = false;
  snatchButton.classList.toggle("is-danger", running);
  snatchButton.title = running ? "Stop download" : "Snatch images";
  snatchButton.innerHTML = running ? stopMarkup : snatchMarkup;
}

function setStatusIcon(state) {
  statusIcon.className = `status-icon is-${state}`;
  if (state === "complete") {
    statusIcon.innerHTML = `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="m5 12 5 5L20 7" />
      </svg>
    `;
    return;
  }

  statusIcon.innerHTML = "<span></span><span></span><span></span>";
}

function renderLogs(lines) {
  if (!lines || lines.length === 0) {
    logBox.innerHTML = '<p class="muted">Ready.</p>';
    return;
  }

  logBox.innerHTML = lines
    .map((line) => {
      const [time, ...rest] = line.split("  ");
      const message = rest.join("  ") || line;
      return `<p><span>${escapeHtml(time)}</span><span>${escapeHtml(message)}</span></p>`;
    })
    .join("");
  logBox.scrollTop = logBox.scrollHeight;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function startJob() {
  if (isRunning) {
    await requestStop();
    return;
  }

  const url = urlInput.value.trim();
  if (!url) {
    urlInput.focus();
    return;
  }

  currentJobId = null;
  setRunningState(true);
  setStatusIcon("working");
  phaseText.textContent = "Starting...";
  setProgress(0, 0, 0);
  setZipReady(null);
  renderLogs(["Now  Starting job."]);

  const response = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url,
      maxPages: Number(maxPagesInput.value || 200),
      deep: deepInput.checked,
      linksOnly: linksOnlyInput.checked,
    }),
  });

  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Could not start job.");
  }

  currentJobId = data.id;
  pollJob(data.id);
}

async function requestStop() {
  if (!currentJobId) {
    return;
  }

  const confirmed = window.confirm(
    "Stopping will halt the whole progress. Any unfinished downloads will be cancelled."
  );
  if (!confirmed) {
    return;
  }

  snatchButton.disabled = true;
  phaseText.textContent = "Stopping...";
  await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" });
}

async function pollJob(jobId) {
  clearInterval(pollTimer);

  async function tick() {
    const response = await fetch(`/api/jobs/${jobId}`);
    const job = await response.json();

    phaseText.textContent = job.phase || "Working...";
    setProgress(job.progress, job.saved, job.total);
    renderLogs(job.logs);

    if (job.status === "complete") {
      clearInterval(pollTimer);
      currentJobId = null;
      setRunningState(false);
      setStatusIcon("complete");
      setProgress(100, job.saved, job.total);
      setZipReady(job.downloadUrl);
    }

    if (job.status === "cancelled") {
      clearInterval(pollTimer);
      currentJobId = null;
      setRunningState(false);
      setStatusIcon("waiting");
      setZipReady(null);
    }

    if (job.status === "error") {
      clearInterval(pollTimer);
      currentJobId = null;
      setRunningState(false);
      setStatusIcon("waiting");
      setZipReady(null);
      phaseText.textContent = "Failed";
    }
  }

  await tick();
  pollTimer = setInterval(tick, 1000);
}

snatchButton.addEventListener("click", () => {
  startJob().catch((error) => {
    currentJobId = null;
    setRunningState(false);
    setStatusIcon("waiting");
    phaseText.textContent = "Failed";
    renderLogs([`Now  ${error.message}`]);
  });
});

urlInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    snatchButton.click();
  }
});

logToggleButton.addEventListener("click", () => {
  const isHidden = logSection.classList.toggle("is-hidden");
  logToggleButton.classList.toggle("is-collapsed", isHidden);
  logToggleButton.setAttribute("aria-expanded", String(!isHidden));
  logToggleButton.title = isHidden ? "Show progress log" : "Hide progress log";
});

clearLogButton.addEventListener("click", () => {
  renderLogs([]);
});

setProgress(0, 0, 0);
setZipReady(null);
setRunningState(false);
setStatusIcon("waiting");
