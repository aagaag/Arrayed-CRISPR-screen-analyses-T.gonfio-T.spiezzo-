/**
 * Interactive plate well-selector for defining control wells.
 *
 * Renders a 384-well (16×24) or 96-well (8×12) plate grid as an HTML table.
 * Users click wells to select them, then assign roles (NT, positive control,
 * or custom) via buttons. The result is a JSON dict {wellId: role} that can
 * be submitted with the run request.
 *
 * Public API:
 *   wellSelector.getControlAssignments()  → { "A01": "NT", "B13": "pos_ctrl", ... }
 *   wellSelector.setControlAssignments(obj)
 *   wellSelector.clear()
 */

const wellSelector = (() => {
  // ── Constants ──────────────────────────────────────────────────────────
  const PLATE_384 = { rows: 16, cols: 24, label: "384-well" };
  const PLATE_96 = { rows: 8, cols: 12, label: "96-well" };

  const ROLE_COLORS = {
    NT: "#2ecc71",
    pos_ctrl: "#3498db",
  };
  const CUSTOM_ROLE_COLOR = "#f39c12";
  const SELECTED_COLOR = "#f1c40f";
  const EMPTY_COLOR = "#ecf0f1";

  // ── State ──────────────────────────────────────────────────────────────
  let plate = PLATE_384;
  let selected = new Set();           // well IDs currently highlighted
  let assignments = {};               // { wellId: role }

  // ── Helpers ────────────────────────────────────────────────────────────
  function wellId(row, col) {
    return String.fromCharCode(65 + row) + String(col + 1).padStart(2, "0");
  }

  function roleColor(role) {
    return ROLE_COLORS[role] || CUSTOM_ROLE_COLOR;
  }

  function wellColor(wid) {
    if (selected.has(wid)) return SELECTED_COLOR;
    if (assignments[wid]) return roleColor(assignments[wid]);
    return EMPTY_COLOR;
  }

  // ── Grid rendering ─────────────────────────────────────────────────────
  function renderGrid() {
    const container = document.getElementById("ws-grid");
    if (!container) return;
    container.innerHTML = "";

    const table = document.createElement("table");
    table.className = "ws-table";

    // Header row (column numbers)
    const thead = document.createElement("tr");
    thead.appendChild(document.createElement("th")); // empty corner
    for (let c = 0; c < plate.cols; c++) {
      const th = document.createElement("th");
      th.textContent = c + 1;
      thead.appendChild(th);
    }
    table.appendChild(thead);

    // Data rows
    for (let r = 0; r < plate.rows; r++) {
      const tr = document.createElement("tr");
      const rowLabel = document.createElement("th");
      rowLabel.textContent = String.fromCharCode(65 + r);
      tr.appendChild(rowLabel);
      for (let c = 0; c < plate.cols; c++) {
        const td = document.createElement("td");
        const wid = wellId(r, c);
        td.className = "ws-well";
        td.dataset.well = wid;
        td.style.backgroundColor = wellColor(wid);
        td.title = assignments[wid] ? `${wid} [${assignments[wid]}]` : wid;
        td.addEventListener("click", () => toggleWell(wid));
        tr.appendChild(td);
      }
      table.appendChild(tr);
    }
    container.appendChild(table);
  }

  function refreshWell(wid) {
    const td = document.querySelector(`.ws-well[data-well="${wid}"]`);
    if (!td) return;
    td.style.backgroundColor = wellColor(wid);
    td.title = assignments[wid] ? `${wid} [${assignments[wid]}]` : wid;
  }

  function refreshAll() {
    document.querySelectorAll(".ws-well").forEach((td) => {
      const wid = td.dataset.well;
      td.style.backgroundColor = wellColor(wid);
      td.title = assignments[wid] ? `${wid} [${assignments[wid]}]` : wid;
    });
  }

  // ── Selection ──────────────────────────────────────────────────────────
  function toggleWell(wid) {
    if (selected.has(wid)) {
      selected.delete(wid);
    } else {
      selected.add(wid);
    }
    refreshWell(wid);
    updateSelectionCount();
  }

  function updateSelectionCount() {
    const badge = document.getElementById("ws-sel-count");
    if (badge) badge.textContent = selected.size > 0 ? `${selected.size} selected` : "";
  }

  // ── Text-based well spec parser ────────────────────────────────────────
  function parseWellSpec(spec) {
    const wells = new Set();
    const tokens = spec.split(",").map((s) => s.trim()).filter(Boolean);

    for (const token of tokens) {
      // col:N or col:N-M  →  all rows in column(s)
      let m = token.match(/^col\s*:\s*(\d+)(?:\s*-\s*(\d+))?$/i);
      if (m) {
        const c1 = parseInt(m[1], 10) - 1;
        const c2 = m[2] ? parseInt(m[2], 10) - 1 : c1;
        for (let c = Math.min(c1, c2); c <= Math.min(Math.max(c1, c2), plate.cols - 1); c++) {
          for (let r = 0; r < plate.rows; r++) wells.add(wellId(r, c));
        }
        continue;
      }

      // row:A or row:A-H  →  all columns in row(s)
      m = token.match(/^row\s*:\s*([A-Pa-p])(?:\s*-\s*([A-Pa-p]))?$/i);
      if (m) {
        const r1 = m[1].toUpperCase().charCodeAt(0) - 65;
        const r2 = m[2] ? m[2].toUpperCase().charCodeAt(0) - 65 : r1;
        for (let r = Math.min(r1, r2); r <= Math.min(Math.max(r1, r2), plate.rows - 1); r++) {
          for (let c = 0; c < plate.cols; c++) wells.add(wellId(r, c));
        }
        continue;
      }

      // A-H:5 or A-H:5-10  →  row range at column(s)
      m = token.match(/^([A-Pa-p])\s*-\s*([A-Pa-p])\s*:\s*(\d+)(?:\s*-\s*(\d+))?$/i);
      if (m) {
        const r1 = m[1].toUpperCase().charCodeAt(0) - 65;
        const r2 = m[2].toUpperCase().charCodeAt(0) - 65;
        const c1 = parseInt(m[3], 10) - 1;
        const c2 = m[4] ? parseInt(m[4], 10) - 1 : c1;
        for (let r = Math.min(r1, r2); r <= Math.min(Math.max(r1, r2), plate.rows - 1); r++) {
          for (let c = Math.min(c1, c2); c <= Math.min(Math.max(c1, c2), plate.cols - 1); c++) {
            wells.add(wellId(r, c));
          }
        }
        continue;
      }

      // A01-A12  →  well range (same row or spanning rows)
      m = token.match(/^([A-Pa-p])(\d{1,2})\s*-\s*([A-Pa-p])(\d{1,2})$/i);
      if (m) {
        const r1 = m[1].toUpperCase().charCodeAt(0) - 65;
        const c1 = parseInt(m[2], 10) - 1;
        const r2 = m[3].toUpperCase().charCodeAt(0) - 65;
        const c2 = parseInt(m[4], 10) - 1;
        for (let r = Math.min(r1, r2); r <= Math.min(Math.max(r1, r2), plate.rows - 1); r++) {
          const colStart = r === Math.min(r1, r2) ? Math.min(c1, c2) : 0;
          const colEnd = r === Math.max(r1, r2) ? Math.max(c1, c2) : plate.cols - 1;
          for (let c = colStart; c <= Math.min(colEnd, plate.cols - 1); c++) {
            wells.add(wellId(r, c));
          }
        }
        continue;
      }

      // A01  →  single well
      m = token.match(/^([A-Pa-p])(\d{1,2})$/i);
      if (m) {
        const r = m[1].toUpperCase().charCodeAt(0) - 65;
        const c = parseInt(m[2], 10) - 1;
        if (r >= 0 && r < plate.rows && c >= 0 && c < plate.cols) {
          wells.add(wellId(r, c));
        }
        continue;
      }
    }
    return wells;
  }

  // ── Actions ────────────────────────────────────────────────────────────
  function assignRole(role) {
    if (selected.size === 0) return;
    for (const wid of selected) {
      assignments[wid] = role;
    }
    selected.clear();
    refreshAll();
    updateSelectionCount();
    updateSummary();
  }

  function clearSelection() {
    selected.clear();
    refreshAll();
    updateSelectionCount();
  }

  function clearAllControls() {
    assignments = {};
    selected.clear();
    refreshAll();
    updateSelectionCount();
    updateSummary();
  }

  function removeRole(role) {
    for (const wid of Object.keys(assignments)) {
      if (assignments[wid] === role) delete assignments[wid];
    }
    refreshAll();
    updateSummary();
  }

  function updateSummary() {
    const summary = document.getElementById("ws-summary");
    if (!summary) return;
    const counts = {};
    for (const role of Object.values(assignments)) {
      counts[role] = (counts[role] || 0) + 1;
    }
    if (Object.keys(counts).length === 0) {
      summary.textContent = "No control wells defined. The pipeline will use controls from the layout CSV.";
      return;
    }
    const parts = Object.entries(counts).map(
      ([role, n]) => `${role}: ${n} well${n > 1 ? "s" : ""}`
    );
    summary.textContent = parts.join("  |  ");
  }

  // ── Load from layout file ───────────────────────────────────────────────
  async function loadFromLayout() {
    const layoutInput = document.getElementById("layout_csv");
    const path = layoutInput ? layoutInput.value.trim() : "";
    if (!path) {
      const feedback = document.getElementById("ws-spec-feedback");
      if (feedback) {
        feedback.textContent = "Fill in the Layout CSV field first.";
        setTimeout(() => { feedback.textContent = ""; }, 3000);
      }
      return;
    }
    try {
      const resp = await fetch("/api/layout-controls", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        const feedback = document.getElementById("ws-spec-feedback");
        if (feedback) {
          feedback.textContent = data.detail || "Failed to load layout controls.";
          setTimeout(() => { feedback.textContent = ""; }, 5000);
        }
        return;
      }
      assignments = data.assignments || {};
      selected.clear();
      refreshAll();
      updateSelectionCount();
      updateSummary();
      const feedback = document.getElementById("ws-spec-feedback");
      if (feedback) {
        const total = Object.keys(assignments).length;
        feedback.textContent = total > 0 ? `Loaded ${total} control wells from layout` : "No control wells found in layout";
        setTimeout(() => { feedback.textContent = ""; }, 4000);
      }
    } catch (err) {
      const feedback = document.getElementById("ws-spec-feedback");
      if (feedback) {
        feedback.textContent = `Error: ${err.message}`;
        setTimeout(() => { feedback.textContent = ""; }, 5000);
      }
    }
  }

  // ── Public API ─────────────────────────────────────────────────────────
  function getControlAssignments() {
    return Object.keys(assignments).length > 0 ? { ...assignments } : null;
  }

  function setControlAssignments(obj) {
    assignments = obj && typeof obj === "object" ? { ...obj } : {};
    selected.clear();
    refreshAll();
    updateSelectionCount();
    updateSummary();
  }

  function clear() {
    clearAllControls();
  }

  // ── Initialization (called once DOM is ready) ──────────────────────────
  function init() {
    const container = document.getElementById("ws-container");
    if (!container) return;

    // Plate format toggle
    const formatSel = document.getElementById("ws-format");
    if (formatSel) {
      formatSel.addEventListener("change", () => {
        plate = formatSel.value === "96" ? PLATE_96 : PLATE_384;
        assignments = {};
        selected.clear();
        renderGrid();
        updateSelectionCount();
        updateSummary();
      });
    }

    // Text spec input
    const specInput = document.getElementById("ws-spec-input");
    const specBtn = document.getElementById("ws-spec-btn");
    if (specBtn && specInput) {
      specBtn.addEventListener("click", () => {
        const wells = parseWellSpec(specInput.value);
        for (const w of wells) selected.add(w);
        refreshAll();
        updateSelectionCount();
        const feedback = document.getElementById("ws-spec-feedback");
        if (feedback) {
          feedback.textContent = wells.size > 0 ? `Added ${wells.size} wells to selection` : "No wells matched";
          setTimeout(() => { feedback.textContent = ""; }, 3000);
        }
      });
    }

    // Role buttons
    document.getElementById("ws-btn-nt")?.addEventListener("click", () => assignRole("NT"));
    document.getElementById("ws-btn-pos")?.addEventListener("click", () => assignRole("pos_ctrl"));
    document.getElementById("ws-btn-clear-sel")?.addEventListener("click", clearSelection);
    document.getElementById("ws-btn-clear-all")?.addEventListener("click", clearAllControls);

    // Load controls from layout file
    document.getElementById("ws-btn-load-layout")?.addEventListener("click", loadFromLayout);

    renderGrid();
    updateSummary();
  }

  // Auto-init when script loads (DOM should already be ready since script is at end of body)
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  return { getControlAssignments, setControlAssignments, clear };
})();
