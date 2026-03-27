const el = (id) => document.getElementById(id);

let currentRunId = null;
let currentLogIndex = 0;
let pollTimer = null;
let figuresUnlocked = false;
let latestFiguresByName = new Map();
let currentPreviewFigure = null;
let currentStepCatalog = [];
const MODE_ARRAYED = "arrayed";
const MODE_POOLED = "pooled";
const SETUP_COOKIE_NAME = "prpcscreen_setup";
const SETUP_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 180;
const SETUP_COOKIE_VERSION = 1;
const DEFAULT_SKYLINE_SHEET = "skylineplot2";

const SETUP_INPUT_IDS = [
  "mode_arrayed",
  "mode_pooled",
  "data_root",
  "raw_dir",
  "layout_csv",
  "genomics_excel",
  "output_dir",
  "skip_fret",
  "skip_glo",
  "heatmap_plate",
];

const FIGURE_LABELS = {
  "candidate_volcano_interactive.html": "Candidate volcano",
  "candidate_volcano_meanlog2_pvalue.png": "Candidate volcano (static, legacy)",
  "candidate_flashlight_ranked_meanlog2_interactive.html": "Candidate ranking flashlight",
  "distribution_log2fc_rep1_interactive.html": "Distribution histogram (Log2FC rep1)",
  "distribution_log2fc_rep1.png": "Distribution histogram (Log2FC rep1, static, legacy)",
  "genomic_skyline_meanlog2fc_interactive.html": "Genomic skyline",
  "genomic_skyline_meanlog2fc.png": "Genomic skyline (Mean log2FC)",
  "grouped_boxplot_raw_rep1_interactive.html": "Grouped violin/box plot",
  "grouped_boxplot_raw_rep1.png": "Grouped violin/box plot (Raw replicate 1)",
  "plate_heatmap_replicates_interactive.html": "Plate heatmap (rep1, rep2, diff)",
  "plate_heatmap_replicates.png": "Plate heatmap (rep1, rep2, diff)",
  "plate_heatmap_replicates_collection_interactive.html": "Plate heatmap collection",
  "plate_heatmap_replicates_collection.svg": "Plate heatmap collection (rep1, rep2, diff, SVG)",
  "plate_qc_ssmd_controls_interactive.html": "Plate quality control",
  "plate_well_series_raw_rep1_interactive.html": "Plate-well trajectory",
  "replicate_agreement_log2fc_interactive.html": "Replicate agreement",
  "replicate_agreement_log2fc.png": "Replicate agreement (Log2FC)",
};

const HIDDEN_FIGURE_NAMES = new Set([
  "plate_heatmap_replicates_collection_low.svg",
  "plate_heatmap_replicates_collection_medium.svg",
  "plate_heatmap_replicates_collection_high.svg",
  "plate_qc_ssmd_controls.png",
  "plate_well_series_raw_rep1.png",
]);

const UPLOAD_FIELDS = {
  raw_dir: {
    inputId: "raw_dir",
    pickerId: "raw_picker",
    buttonId: "raw_picker_btn",
    statusId: "raw_picker_status",
  },
  layout_csv: {
    inputId: "layout_csv",
    pickerId: "layout_picker",
    buttonId: "layout_picker_btn",
    statusId: "layout_picker_status",
  },
  genomics_excel: {
    inputId: "genomics_excel",
    pickerId: "genomics_picker",
    buttonId: "genomics_picker_btn",
    statusId: "genomics_picker_status",
  },
};

const RAW_UPLOAD_TOOLTIP_ARRAYED =
  "Arrayed mode: upload one folder containing the raw plate-export CSV/TSV/TXT files. Include the instrument export files themselves, not a zip archive.";
const RAW_UPLOAD_TOOLTIP_POOLED =
  "Pooled mode: upload one pooled counts table in CSV, TSV, TXT, XLSX, or XLS format. The table must include replicate columns such as Negative_R1.. and Positive_R1..";

function toTitleCaseWords(text) {
  return text
    .split(/\s+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function fallbackFigureLabel(filename, kind) {
  const base = filename.replace(/\.(png|svg|html?)$/i, "");
  const normalized = base
    .replace(/[_-]+/g, " ")
    .replace(/\blog2fc\b/gi, "log2FC")
    .replace(/\bssmd\b/gi, "SSMD")
    .replace(/\brep(\d+)\b/gi, "replicate $1");
  return toTitleCaseWords(normalized);
}

function cleanFigureLabel(label) {
  return String(label || "")
    .replace(/\s*\(interactive\)\s*/gi, " ")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function figureDisplayLabel(figure) {
  const key = (figure.name || "").toLowerCase();
  const rawLabel = FIGURE_LABELS[key] || fallbackFigureLabel(figure.name || "", figure.kind || "");
  return cleanFigureLabel(rawLabel);
}

function interactiveTargetForFigure(figure, byName) {
  if (!figure || !figure.name) return null;
  if (figure.kind === "html") return figure;
  const lower = figure.name.toLowerCase();
  const base = lower.replace(/\.(png|svg|html?)$/i, "");
  const candidates = [
    `${base}_interactive.html`,
    `${base}.html`,
  ];
  if (base.endsWith("_collection")) {
    const noCollection = base.replace(/_collection$/, "");
    candidates.push(`${noCollection}_interactive.html`);
  }
  for (const name of candidates) {
    const found = byName.get(name);
    if (found && found.kind === "html") {
      return found;
    }
  }
  return null;
}

function figureMenuSortKey(figure) {
  const name = String(figure?.name || "").toLowerCase();
  if (name === "plate_heatmap_replicates_collection.svg") {
    return [2, name];
  }
  const isInteractive = (figure?.kind || "") === "html" && name.includes("interactive");
  return [isInteractive ? 0 : 1, name];
}

function setStatus(text, cls) {
  const s = el("status");
  s.textContent = text;
  s.className = `status ${cls}`;
}

function setValidationStatus(text, cls = "") {
  const node = el("validation_status");
  if (!node) return;
  node.textContent = text;
  node.className = `validation-status${cls ? ` ${cls}` : ""}`;
}

function setUploadStatus(target, text, cls = "") {
  const statusId = UPLOAD_FIELDS[target]?.statusId;
  const node = statusId ? el(statusId) : null;
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

function setPreviewState(item = null) {
  const preview = el("preview");
  const frame = el("preview_frame");
  const empty = el("preview_empty");
  const openCurrentBtn = el("open_current_interactive");
  currentPreviewFigure = item || null;
  const currentInteractive = interactiveTargetForFigure(currentPreviewFigure, latestFiguresByName);

  if (openCurrentBtn) {
    if (currentInteractive && currentInteractive.url) {
      openCurrentBtn.disabled = false;
      openCurrentBtn.title = `Open interactive file: ${currentInteractive.name}`;
      openCurrentBtn.onclick = () => {
        window.open(currentInteractive.url, "_blank", "noopener,noreferrer");
      };
    } else {
      openCurrentBtn.disabled = true;
      openCurrentBtn.title = "No interactive version available for this figure.";
      openCurrentBtn.onclick = null;
    }
  }

  if (item && item.url) {
    if (item.kind === "html") {
      frame.src = item.url;
      frame.style.display = "block";
      preview.removeAttribute("src");
      preview.style.display = "none";
    } else {
      preview.src = item.url;
      preview.style.display = "block";
      frame.removeAttribute("src");
      frame.style.display = "none";
    }
    empty.style.display = "none";
  } else {
    preview.removeAttribute("src");
    preview.style.display = "none";
    frame.removeAttribute("src");
    frame.style.display = "none";
    empty.style.display = "flex";
  }
}

function selectedMode() {
  const checked = document.querySelector('input[name="mode"]:checked');
  const mode = (checked?.value || MODE_ARRAYED).trim().toLowerCase();
  return mode === MODE_POOLED ? MODE_POOLED : MODE_ARRAYED;
}

function normalizeMode(value) {
  const mode = String(value || "").trim().toLowerCase();
  return mode === MODE_POOLED ? MODE_POOLED : MODE_ARRAYED;
}

function setMode(value) {
  const mode = normalizeMode(value);
  const arrayed = el("mode_arrayed");
  const pooled = el("mode_pooled");
  if (arrayed) arrayed.checked = mode === MODE_ARRAYED;
  if (pooled) pooled.checked = mode === MODE_POOLED;
}

function readCookieValue(name) {
  const prefix = `${name}=`;
  const parts = document.cookie ? document.cookie.split("; ") : [];
  for (const part of parts) {
    if (part.startsWith(prefix)) {
      return part.slice(prefix.length);
    }
  }
  return "";
}

function clearCookie(name) {
  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `${name}=; Max-Age=0; Path=/; SameSite=Lax${secure}`;
}

function readSetupCookie() {
  const raw = readCookieValue(SETUP_COOKIE_NAME);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(decodeURIComponent(raw));
    if (!parsed || typeof parsed !== "object") return null;
    if (Number(parsed.v || 0) !== SETUP_COOKIE_VERSION) return null;
    return parsed;
  } catch {
    clearCookie(SETUP_COOKIE_NAME);
    return null;
  }
}

function clearFigureList() {
  const list = el("figures");
  if (list) {
    list.innerHTML = "";
    if (!figuresUnlocked) {
      const li = document.createElement("li");
      li.textContent = "Run the pipeline to generate and show new figures.";
      li.style.color = "#475569";
      li.style.fontSize = "13px";
      list.appendChild(li);
    }
  }
  setPreviewState(null);
}

function updateFigureRefreshState() {
  const refreshBtn = el("refresh_figs");
  if (!refreshBtn) return;
  refreshBtn.disabled = !figuresUnlocked;
  refreshBtn.title = figuresUnlocked
    ? "Refresh figures"
    : "Figures appear only after a successful pipeline run.";
}

function writeSetupCookie(setup) {
  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  const payload = encodeURIComponent(JSON.stringify(setup));
  document.cookie =
    `${SETUP_COOKIE_NAME}=${payload}; Max-Age=${SETUP_COOKIE_MAX_AGE_SECONDS}; Path=/; SameSite=Lax${secure}`;
}

function readSetupFromForm() {
  return {
    v: SETUP_COOKIE_VERSION,
    mode: selectedMode(),
    data_root: el("data_root").value.trim(),
    raw_dir: el("raw_dir").value.trim(),
    layout_csv: el("layout_csv").value.trim(),
    genomics_excel: el("genomics_excel").value.trim(),
    output_dir: el("output_dir").value.trim(),
    skip_fret: Number(el("skip_fret").value),
    skip_glo: Number(el("skip_glo").value),
    heatmap_plate: el("heatmap_plate").value.trim(),
  };
}

function persistSetupCookie() {
  writeSetupCookie(readSetupFromForm());
}

function setTextIfPresent(id, value) {
  if (value === null || value === undefined) return;
  const node = el(id);
  if (!node) return;
  node.value = String(value);
}

function setNumberIfPresent(id, value) {
  if (value === null || value === undefined) return;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return;
  const node = el(id);
  if (!node) return;
  node.value = String(parsed);
}

function restoreSetupFromCookie() {
  const saved = readSetupCookie();
  if (!saved) return false;

  setMode(saved.mode);
  setTextIfPresent("data_root", saved.data_root);
  setTextIfPresent("raw_dir", saved.raw_dir);
  setTextIfPresent("layout_csv", saved.layout_csv);
  setTextIfPresent("genomics_excel", saved.genomics_excel);
  setTextIfPresent("output_dir", saved.output_dir);
  setTextIfPresent("heatmap_plate", saved.heatmap_plate);
  setNumberIfPresent("skip_fret", saved.skip_fret);
  setNumberIfPresent("skip_glo", saved.skip_glo);
  return true;
}

function registerSetupPersistenceHandlers() {
  SETUP_INPUT_IDS.forEach((id) => {
    const node = el(id);
    if (!node) return;
    const markDirty = () => {
      persistSetupCookie();
      setValidationStatus("Validation should be rerun after input changes.");
    };
    node.addEventListener("change", markDirty);
    node.addEventListener("blur", markDirty);
  });
}

function applyModeUi() {
  const mode = selectedMode();
  const pooled = mode === MODE_POOLED;
  const layout = document.querySelector("main.layout");
  if (layout) {
    layout.classList.toggle("mode-arrayed", !pooled);
    layout.classList.toggle("mode-pooled", pooled);
  }

  const rawLabel = el("raw_label_text");
  if (rawLabel) {
    rawLabel.textContent = pooled ? "Pooled table (CSV/TSV/TXT/XLSX)" : "Raw dir/file";
  }
  const genomicsLabel = el("genomics_label_text");
  if (genomicsLabel) {
    genomicsLabel.textContent = pooled ? "Genomics XLSX (optional)" : "Genomics XLSX";
  }
  const modeNote = el("mode_note");
  if (modeNote) {
    modeNote.textContent = pooled
      ? "Pooled mode runs pooled metrics + reusable replicate/distribution/volcano figure scripts. Layout/plate-only fields are hidden."
      : "Arrayed mode runs the full plate-based workflow, including integration, QC, heatmap, and grouped views.";
  }

  document.querySelectorAll(".arrayed-only").forEach((node) => {
    node.classList.toggle("hidden", pooled);
  });

  configurePickerUi();
}

function configurePickerUi() {
  const rawPicker = el("raw_picker");
  const rawButton = el("raw_picker_btn");
  if (rawPicker && rawButton) {
    if (selectedMode() === MODE_POOLED) {
      rawButton.textContent = "Upload File";
      rawButton.title = RAW_UPLOAD_TOOLTIP_POOLED;
      rawPicker.removeAttribute("webkitdirectory");
      rawPicker.removeAttribute("directory");
      rawPicker.multiple = false;
      rawPicker.accept = ".csv,.tsv,.txt,.xlsx,.xls";
    } else {
      rawButton.textContent = "Upload Folder";
      rawButton.title = RAW_UPLOAD_TOOLTIP_ARRAYED;
      rawPicker.setAttribute("webkitdirectory", "");
      rawPicker.setAttribute("directory", "");
      rawPicker.multiple = true;
      rawPicker.accept = ".csv,.tsv,.txt";
    }
    rawPicker.value = "";
  }
}

function openScopeModal() {
  const modal = el("scope_modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
}

function closeScopeModal() {
  const modal = el("scope_modal");
  if (!modal) return;
  modal.classList.add("hidden");
  document.body.classList.remove("modal-open");
}

window.openScopeModal = openScopeModal;
window.closeScopeModal = closeScopeModal;

async function loadStepCatalog() {
  const mode = selectedMode();
  const container = el("step_groups");
  if (!container) return;
  container.innerHTML = "";
  try {
    const resp = await fetch(`/api/steps?mode=${encodeURIComponent(mode)}`);
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || "Unable to load step list.");
    }
    currentStepCatalog = Array.isArray(data.steps) ? data.steps : [];
    const groups = [];
    const preprocessing = currentStepCatalog.filter((step) => String(step.kind || "") === "preprocess");
    const figures = currentStepCatalog.filter((step) => String(step.kind || "") === "figure");
    const pipeline = currentStepCatalog.filter((step) => String(step.kind || "") === "pipeline");
    const other = currentStepCatalog.filter((step) => !["preprocess", "figure", "pipeline"].includes(String(step.kind || "")));

    if (preprocessing.length > 0) groups.push({ title: "Preprocessing", steps: preprocessing });
    if (figures.length > 0) groups.push({ title: "Figures", steps: figures });
    if (pipeline.length > 0) groups.push({ title: "Pipeline", steps: pipeline });
    if (other.length > 0) groups.push({ title: "Other", steps: other });

    groups.forEach((group) => {
      const section = document.createElement("section");
      section.className = "step-group";

      const heading = document.createElement("h4");
      heading.textContent = group.title;
      section.appendChild(heading);

      const buttonGrid = document.createElement("div");
      buttonGrid.className = "step-buttons";
      group.steps.forEach((step) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary";
        button.textContent = step.label || step.key || "Unnamed step";
        button.title = `Run only this step: ${step.label || step.key}`;
        button.addEventListener("click", () => {
          runPipeline([String(step.key)]);
        });
        buttonGrid.appendChild(button);
      });
      section.appendChild(buttonGrid);
      container.appendChild(section);
    });
  } catch (err) {
    const detail = err instanceof Error ? err.message : "Unable to load step list.";
    const fallback = document.createElement("div");
    fallback.className = "validation-status error";
    fallback.textContent = detail;
    container.appendChild(fallback);
  }
}

function readForm() {
  const payload = {
    mode: selectedMode(),
    raw_dir: el("raw_dir").value.trim(),
    layout_csv: el("layout_csv").value.trim(),
    genomics_excel: el("genomics_excel").value.trim(),
    output_dir: el("output_dir").value.trim(),
    skip_fret: Number(el("skip_fret").value),
    skip_glo: Number(el("skip_glo").value),
    heatmap_plate: el("heatmap_plate").value.trim(),
    sheet: DEFAULT_SKYLINE_SHEET,
    debug: true,
  };
  if (typeof wellSelector !== "undefined") {
    const ctrls = wellSelector.getControlAssignments();
    if (ctrls) payload.control_overrides = ctrls;
  }
  return payload;
}

async function uploadInput(target) {
  const config = UPLOAD_FIELDS[target];
  if (!config) return;
  const picker = el(config.pickerId);
  const pathInput = el(config.inputId);
  if (!picker || !pathInput) return;

  const files = Array.from(picker.files || []);
  if (files.length === 0) return;

  const formData = new FormData();
  formData.append("target", target);
  formData.append("mode", selectedMode());
  formData.append("sheet", DEFAULT_SKYLINE_SHEET);
  files.forEach((file) => {
    formData.append("files", file, file.name);
    if (target === "raw_dir" && selectedMode() === MODE_ARRAYED) {
      formData.append("relative_paths", file.webkitRelativePath || file.name);
    }
  });

  setUploadStatus(target, `Uploading ${files.length} item(s)...`, "uploading");
  try {
    const resp = await fetch("/api/upload-input", {
      method: "POST",
      body: formData,
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || "Upload failed.");
    }
    pathInput.value = String(data.path || "");
    const message = data.validation?.message || `Upload complete for ${target}.`;
    setUploadStatus(target, message, "success");
    appendLog(`[upload] ${target}: ${message}`);
    if (data.path) {
      appendLog(`[upload] server path: ${data.path}`);
    }
    persistSetupCookie();
  } catch (err) {
    const detail = err instanceof Error ? err.message : "Upload failed.";
    setUploadStatus(target, detail, "error");
    appendLog(`[upload] ${target} failed: ${detail}`);
  } finally {
    picker.value = "";
  }
}

async function validateInputs({forRun = false} = {}) {
  const payload = readForm();
  setValidationStatus("Validating inputs...", "running");
  try {
    const resp = await fetch("/api/validate-inputs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || "Validation failed.");
    }
    const checks = data.checks || {};
    const messages = [];
    if (checks.raw_dir?.message) messages.push(`Raw input: ${checks.raw_dir.message}`);
    if (checks.layout_csv?.message) messages.push(`Layout: ${checks.layout_csv.message}`);
    if (checks.genomics_excel?.message) messages.push(`Genomics: ${checks.genomics_excel.message}`);
    messages.forEach((line) => appendLog(`[validate] ${line}`));
    setValidationStatus("Inputs validated successfully.", "success");
    if (!forRun) {
      appendLog("[validate] All required inputs look compatible.");
    }
    return true;
  } catch (err) {
    const detail = err instanceof Error ? err.message : "Validation failed.";
    setValidationStatus(detail, "error");
    appendLog(`[validate] ${detail}`);
    return false;
  }
}

async function scanRoot() {
  const root = el("data_root").value.trim();
  if (!root) {
    appendLog("Data root is empty.");
    return;
  }
  appendLog(`Auto-filling paths from root: ${root}`);
  const resp = await fetch("/api/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    appendLog(`Auto-fill failed: ${data.detail || "unknown error"}`);
    return;
  }

  const rawList = el("raw_list");
  rawList.innerHTML = "";
  data.raw_candidates.forEach((v) => {
    const o = document.createElement("option");
    o.value = v;
    rawList.appendChild(o);
  });

  const layoutList = el("layout_list");
  layoutList.innerHTML = "";
  data.layout_candidates.forEach((v) => {
    const o = document.createElement("option");
    o.value = v;
    layoutList.appendChild(o);
  });

  const genomicsList = el("genomics_list");
  genomicsList.innerHTML = "";
  data.genomics_candidates.forEach((v) => {
    const o = document.createElement("option");
    o.value = v;
    genomicsList.appendChild(o);
  });

  el("raw_dir").value = data.raw_selected || "";
  el("layout_csv").value = data.layout_selected || "";
  el("genomics_excel").value = data.genomics_selected || "";

  if (selectedMode() === MODE_POOLED) {
    const pooledCandidate = (data.raw_candidates || []).find((candidate) =>
      /\.(csv|tsv|txt|xlsx|xls)$/i.test(String(candidate))
    );
    if (pooledCandidate) {
      el("raw_dir").value = pooledCandidate;
    }
  }

  appendLog(
    `Auto-fill complete. Raw candidates: ${data.counts.raw}; Layout CSVs: ${data.counts.layout}; Genomics files: ${data.counts.genomics}`
  );
  if (Array.isArray(data.scan_roots) && data.scan_roots.length > 0) {
    appendLog(`Scanned roots: ${data.scan_roots.join(" | ")}`);
  }
  if (data.layout_selected) {
    appendLog(`Auto-selected Layout CSV: ${data.layout_selected}`);
  }
  if (data.genomics_selected) {
    appendLog(`Auto-selected Genomics XLSX: ${data.genomics_selected}`);
  }
  persistSetupCookie();
}

async function fetchStatus() {
  if (!currentRunId) return;
  const resp = await fetch(`/api/status/${currentRunId}?from_index=${currentLogIndex}`);
  const data = await resp.json();
  if (!resp.ok) {
    if (resp.status === 404) {
      appendLog(
        "Status error: Run not found. The web server likely restarted during execution (commonly from --reload). " +
          "For long runs, start server without --reload and run again."
      );
      setStatus("Failed", "failed");
    } else {
      appendLog(`Status error: ${data.detail || "unknown error"}`);
    }
    clearInterval(pollTimer);
    pollTimer = null;
    return;
  }

  data.logs.forEach(appendLog);
  currentLogIndex = data.next_index;

  if (data.status === "running" || data.status === "queued") {
    setStatus("Running", "running");
    return;
  }
  if (data.status === "completed") {
    setStatus("Completed", "completed");
    clearInterval(pollTimer);
    pollTimer = null;
    figuresUnlocked = true;
    updateFigureRefreshState();
    await refreshFigures();
    return;
  }
  if (data.status === "failed") {
    setStatus("Failed", "failed");
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function runPipeline(stepKeys = null) {
  clearLog();
  setStatus("Starting", "running");
  figuresUnlocked = false;
  updateFigureRefreshState();
  clearFigureList();
  const payload = readForm();
  if (Array.isArray(stepKeys) && stepKeys.length > 0) {
    payload.step_keys = stepKeys;
  }
  persistSetupCookie();
  const valid = await validateInputs({ forRun: true });
  if (!valid) {
    setStatus("Validation Failed", "failed");
    return;
  }
  const executionLabel = Array.isArray(stepKeys) && stepKeys.length > 0
    ? `steps=${stepKeys.join(",")}`
    : "full_pipeline";
  appendLog(
    `Run request: mode=${payload.mode} | execution=${executionLabel} | raw=${payload.raw_dir} | layout=${payload.layout_csv || "(none)"} | genomics=${payload.genomics_excel || "(none)"} | output=${payload.output_dir} | debug=${payload.debug}`
  );
  const resp = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (!resp.ok) {
    setStatus("Failed", "failed");
    appendLog(`Failed to start run: ${data.detail || "unknown error"}`);
    return;
  }
  currentRunId = data.run_id;
  currentLogIndex = 0;
  appendLog(`Run id: ${currentRunId}`);
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(fetchStatus, 1000);
}

async function refreshFigures() {
  if (!figuresUnlocked) {
    clearFigureList();
    return;
  }
  const output = el("output_dir").value.trim() || "results";
  const resp = await fetch(`/api/figures?output_dir=${encodeURIComponent(output)}`);
  const data = await resp.json();
  const list = el("figures");
  list.innerHTML = "";
  const visibleFigures = (data.figures || []).filter(
    (item) => !HIDDEN_FIGURE_NAMES.has(String(item.name || "").toLowerCase())
  );
  const byName = new Map(
    visibleFigures.map((item) => [String(item.name || "").toLowerCase(), item])
  );
  latestFiguresByName = byName;
  const orderedFigures = [...visibleFigures].sort((a, b) => {
    const ka = figureMenuSortKey(a);
    const kb = figureMenuSortKey(b);
    return ka[0] - kb[0] || ka[1].localeCompare(kb[1]);
  });
  orderedFigures.forEach((f) => {
    const li = document.createElement("li");
    li.className = "figure-entry";

    const previewBtn = document.createElement("button");
    const label = figureDisplayLabel(f);
    previewBtn.className = "preview-btn";
    previewBtn.textContent = label;
    previewBtn.title = `File: ${f.name}`;
    previewBtn.type = "button";
    previewBtn.addEventListener("click", () => {
      setPreviewState(f);
    });

    const interactiveTarget = interactiveTargetForFigure(f, byName);
    const openBtn = document.createElement("button");
    openBtn.className = "secondary open-interactive-btn";
    openBtn.textContent = "Open Interactive";
    openBtn.type = "button";
    if (interactiveTarget && interactiveTarget.url) {
      openBtn.title = `Open interactive file: ${interactiveTarget.name}`;
      openBtn.addEventListener("click", () => {
        window.open(interactiveTarget.url, "_blank", "noopener,noreferrer");
      });
    } else {
      openBtn.disabled = true;
      openBtn.title = "No interactive version available for this figure.";
    }

    li.appendChild(previewBtn);
    li.appendChild(openBtn);
    list.appendChild(li);
  });
  if (orderedFigures.length > 0) {
    setPreviewState(orderedFigures[0]);
  } else {
    setPreviewState(null);
  }
}

async function unlockExistingFiguresIfPresent() {
  const output = el("output_dir").value.trim() || "results";
  try {
    const resp = await fetch(`/api/figures?output_dir=${encodeURIComponent(output)}`);
    const data = await resp.json();
    if (!resp.ok) {
      return;
    }
    if (Array.isArray(data.figures) && data.figures.length > 0) {
      figuresUnlocked = true;
      updateFigureRefreshState();
      await refreshFigures();
    }
  } catch {
    // Ignore preload failures; normal run flow still works.
  }
}

el("scan_btn").addEventListener("click", scanRoot);
el("run_btn").addEventListener("click", runPipeline);
el("validate_btn").addEventListener("click", () => {
  validateInputs();
});
el("refresh_figs").addEventListener("click", () => {
  if (!figuresUnlocked) {
    appendLog("Figures will be shown after a successful pipeline run.");
    return;
  }
  refreshFigures();
});
const scopeButton = el("scope_btn");
if (scopeButton) {
  scopeButton.addEventListener("click", openScopeModal);
}
const scopeCloseButton = el("scope_close_btn");
if (scopeCloseButton) {
  scopeCloseButton.addEventListener("click", closeScopeModal);
}
const scopeModal = el("scope_modal");
if (scopeModal) {
  scopeModal.addEventListener("click", (event) => {
    if (event.target && event.target.id === "scope_modal") {
      closeScopeModal();
    }
  });
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeScopeModal();
  }
});
Object.entries(UPLOAD_FIELDS).forEach(([target, config]) => {
  const button = el(config.buttonId);
  const picker = el(config.pickerId);
  if (button && picker) {
    button.addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => {
      uploadInput(target);
    });
  }
});
document.querySelectorAll('input[name="mode"]').forEach((node) => {
  node.addEventListener("change", () => {
    applyModeUi();
    loadStepCatalog();
  });
});

restoreSetupFromCookie();
setStatus("Idle", "idle");
setValidationStatus("Validation has not been run yet.");
applyModeUi();
loadStepCatalog();
registerSetupPersistenceHandlers();
persistSetupCookie();
clearFigureList();
updateFigureRefreshState();
unlockExistingFiguresIfPresent();

el("preview").addEventListener("error", () => {
  setPreviewState(null);
});
