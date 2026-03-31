const el = (id) => document.getElementById(id);

const MODE = "pooled";
const DEFAULT_SHEET = "";

let currentRunId = null;
let currentLogIndex = 0;
let pollTimer = null;
let currentInteractiveUrl = "";

function setStatus(text, cls) {
  const node = el("status");
  node.textContent = text;
  node.className = `status ${cls}`;
}

function setValidationStatus(text, cls = "") {
  const node = el("validation_status");
  node.textContent = text;
  node.className = `validation-status${cls ? ` ${cls}` : ""}`;
}

function setUploadStatus(id, text, cls = "") {
  const node = el(id);
  if (!node) return;
  node.textContent = text;
  node.className = `upload-status${cls ? ` ${cls}` : ""}`;
}

function appendLog(line) {
  const log = el("log");
  log.textContent += `${line}\n`;
  log.scrollTop = log.scrollHeight;
}

function clearLog() {
  el("log").textContent = "";
}

function setPreview(url = "") {
  const frame = el("preview_frame");
  const empty = el("preview_empty");
  const openBtn = el("open_current_interactive");
  currentInteractiveUrl = url || "";
  if (currentInteractiveUrl) {
    frame.src = currentInteractiveUrl;
    frame.style.display = "block";
    empty.style.display = "none";
    openBtn.disabled = false;
    openBtn.onclick = () => window.open(currentInteractiveUrl, "_blank", "noopener,noreferrer");
  } else {
    frame.removeAttribute("src");
    frame.style.display = "none";
    empty.style.display = "flex";
    openBtn.disabled = true;
    openBtn.onclick = null;
  }
}

function buildPayload() {
  return {
    mode: MODE,
    raw_dir: String(el("raw_dir").value || "").trim(),
    layout_csv: "",
    genomics_excel: String(el("genomics_excel").value || "").trim(),
    output_dir: String(el("output_dir").value || "").trim(),
    sheet: DEFAULT_SHEET,
    skip_fret: 38,
    skip_glo: 9,
    heatmap_plate: "all",
    debug: false,
    step_keys: [],
    control_overrides: {},
  };
}

function requireRawDir() {
  const raw = String(el("raw_dir").value || "").trim();
  if (raw) return raw;
  throw new Error("Please select or upload a Screen results file before validating or running the pipeline.");
}

async function uploadInput(target, file) {
  const fd = new FormData();
  fd.append("target", target);
  fd.append("mode", MODE);
  fd.append("sheet", DEFAULT_SHEET);
  fd.append("files", file);
  const resp = await fetch("/api/upload-input", { method: "POST", body: fd });
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.detail || "Upload failed.");
  }
  return data;
}

async function validateInputs() {
  requireRawDir();
  const payload = buildPayload();
  const resp = await fetch("/api/validate-inputs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.detail || "Validation failed.");
  }
  const raw = data.checks?.raw_dir?.summary || "Pooled table validated.";
  const genomics = data.checks?.genomics_excel?.summary || "No genomics workbook provided.";
  setValidationStatus(`${raw} ${genomics}`, "ok");
}

async function scanRoot() {
  const root = String(el("data_root").value || "").trim();
  const resp = await fetch("/api/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.detail || "Scan failed.");
  }
  const rawList = el("raw_list");
  const genomicsList = el("genomics_list");
  rawList.innerHTML = "";
  genomicsList.innerHTML = "";
  for (const item of data.raw_candidates || []) {
    const opt = document.createElement("option");
    opt.value = item;
    rawList.appendChild(opt);
  }
  for (const item of data.genomics_candidates || []) {
    const opt = document.createElement("option");
    opt.value = item;
    genomicsList.appendChild(opt);
  }
  if (data.raw_selected) el("raw_dir").value = data.raw_selected;
  if (data.genomics_selected) el("genomics_excel").value = data.genomics_selected;
  const rawCount = Number(data.counts?.raw || 0);
  const genomicsCount = Number(data.counts?.genomics || 0);
  if (!data.raw_selected) {
    setValidationStatus(
      `No screen-results file was found under ${root || "the selected data root"}. Upload a file or type its full path, then run again.`,
      "error"
    );
    return;
  }
  setValidationStatus(`Auto-fill found ${rawCount} pooled candidate(s) and ${genomicsCount} genomics workbook(s).`);
}

async function fetchVolcanoFigure() {
  const output = String(el("output_dir").value || "").trim();
  if (!output) return;
  const resp = await fetch(`/api/figures?output_dir=${encodeURIComponent(output)}`);
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.detail || "Unable to load figures.");
  }
  const volcano = (data.figures || []).find((item) => String(item.name || "").toLowerCase() === "candidate_volcano_interactive.html");
  setPreview(volcano?.url || "");
}

async function pollStatus() {
  if (!currentRunId) return;
  const resp = await fetch(`/api/status/${encodeURIComponent(currentRunId)}?from_index=${currentLogIndex}`);
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.detail || "Status polling failed.");
  }
  for (const line of data.logs || []) appendLog(line);
  currentLogIndex = Number(data.next_index || currentLogIndex);
  if (data.status === "completed") {
    setStatus("Completed", "ok");
    await fetchVolcanoFigure();
    currentRunId = null;
    return;
  }
  if (data.status === "failed") {
    setStatus("Failed", "error");
    if (data.error) appendLog(`ERROR: ${data.error}`);
    currentRunId = null;
    return;
  }
  pollTimer = window.setTimeout(pollStatus, 1200);
}

async function runPipeline() {
  requireRawDir();
  const payload = buildPayload();
  clearLog();
  setPreview("");
  setStatus("Running", "busy");
  setValidationStatus("Pipeline running...");
  const resp = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (!resp.ok) {
    setStatus("Failed", "error");
    throw new Error(data.detail || "Run failed to start.");
  }
  currentRunId = data.run_id;
  currentLogIndex = 0;
  if (pollTimer) window.clearTimeout(pollTimer);
  pollStatus().catch((err) => {
    setStatus("Failed", "error");
    appendLog(`ERROR: ${err.message}`);
  });
}

function wireUpload(buttonId, pickerId, target, inputId, statusId) {
  el(buttonId).addEventListener("click", () => el(pickerId).click());
  el(pickerId).addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      setUploadStatus(statusId, "Uploading...", "busy");
      const data = await uploadInput(target, file);
      el(inputId).value = data.path || "";
      setUploadStatus(statusId, data.validation?.summary || "Uploaded.", "ok");
    } catch (err) {
      setUploadStatus(statusId, err.message || "Upload failed.", "error");
    } finally {
      event.target.value = "";
    }
  });
}

el("scan_btn").addEventListener("click", () => {
  scanRoot().catch((err) => setValidationStatus(err.message || "Scan failed.", "error"));
});

el("validate_btn").addEventListener("click", () => {
  validateInputs().catch((err) => {
    setValidationStatus(err.message || "Validation failed.", "error");
    setStatus("Idle", "idle");
  });
});

el("run_btn").addEventListener("click", () => {
  runPipeline().catch((err) => {
    setStatus("Failed", "error");
    setValidationStatus(err.message || "Run failed.", "error");
  });
});

wireUpload("raw_picker_btn", "raw_picker", "raw_dir", "raw_dir", "raw_picker_status");
wireUpload("genomics_picker_btn", "genomics_picker", "genomics_excel", "genomics_excel", "genomics_picker_status");

setStatus("Idle", "idle");
setPreview("");
scanRoot().catch((err) => {
  setValidationStatus(err.message || "Auto-fill failed.", "error");
});
