const urlInput = document.querySelector("#urlInput");
const snatchButton = document.querySelector("#snatchButton");
const settingsButton = document.querySelector("#settingsButton");
const optionsPanel = document.querySelector("#optionsPanel");
const maxPagesInput = document.querySelector("#maxPagesInput");
const deepInput = document.querySelector("#deepInput");
const linksOnlyInput = document.querySelector("#linksOnlyInput");
const phaseText = document.querySelector("#phaseText");
const countText = document.querySelector("#countText");
const meterFill = document.querySelector("#meterFill");
const spinner = document.querySelector("#spinner");
const logBox = document.querySelector("#logBox");
const clearLogButton = document.querySelector("#clearLogButton");
const zipButton = document.querySelector("#zipButton");

let pollTimer = null;

function setProgress(progress, saved, total) {
  const percent = Math.max(0, Math.min(100, Number(progress) || 0));
  meterFill.style.width = `${percent}%`;
  countText.textContent = `${saved || 0} / ${total || 0} ${percent}%`;
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

function renderLogs(lines) {
  if (!lines || lines.length === 0) {
    logBox.innerHTML = '<p class="muted">Ready.</p>';
    return;
  }

  logBox.innerHTML = lines
    .map((line) => {
      const [time, ...rest] = line.split("  ");
      const message = rest.join("  ") || line;
      return `<p><span>${escapeHtml(time)}</span><span></span><span>${escapeHtml(message)}</span></p>`;
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
  const url = urlInput.value.trim();
  if (!url) {
    urlInput.focus();
    return;
  }

  snatchButton.disabled = true;
  spinner.classList.add("is-active");
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

  pollJob(data.id);
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
      snatchButton.disabled = false;
      spinner.classList.remove("is-active");
      setZipReady(job.downloadUrl);
    }

    if (job.status === "error") {
      clearInterval(pollTimer);
      snatchButton.disabled = false;
      spinner.classList.remove("is-active");
      setZipReady(null);
      phaseText.textContent = "Failed";
    }
  }

  await tick();
  pollTimer = setInterval(tick, 1000);
}

snatchButton.addEventListener("click", () => {
  startJob().catch((error) => {
    snatchButton.disabled = false;
    spinner.classList.remove("is-active");
    phaseText.textContent = "Failed";
    renderLogs([`Now  ${error.message}`]);
  });
});

urlInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    snatchButton.click();
  }
});

settingsButton.addEventListener("click", () => {
  optionsPanel.classList.toggle("is-hidden");
});

clearLogButton.addEventListener("click", () => {
  renderLogs([]);
});

setProgress(0, 0, 0);
setZipReady(null);
