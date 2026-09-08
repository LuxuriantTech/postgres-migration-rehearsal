const state = {
  catalogue: null,
  selectedId: null,
  requestGeneration: 0,
  activeRun: false,
  cleanupBlocked: false
};

const UNCONFIRMED_CLEANUP_RECOVERY =
  "Local cleanup was not confirmed. Do not run another rehearsal. Stop the local service and inspect its cleanup receipt.";

const elements = {
  form: document.querySelector("#scenario-form"),
  select: document.querySelector("#scenario-select"),
  purpose: document.querySelector("#scenario-purpose"),
  count: document.querySelector("#scenario-count"),
  provenance: document.querySelector("#scenario-provenance"),
  source: document.querySelector("#source-schema"),
  destination: document.querySelector("#destination-schema"),
  run: document.querySelector("#run-button"),
  reset: document.querySelector("#reset-button"),
  runningStatus: document.querySelector("#running-status"),
  recovery: document.querySelector("#run-recovery"),
  status: document.querySelector("#live-status"),
  phaseB: document.querySelector("#phase-b-evidence"),
  output: document.querySelector("#run-output"),
  content: document.querySelector("#run-content")
};

function node(tag, options = {}) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text) element.textContent = options.text;
  return element;
}

function replaceTextList(container, values) {
  container.replaceChildren();
  for (const value of values) {
    const item = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = value;
    item.append(code);
    container.append(item);
  }
}

function selectedScenario() {
  return state.catalogue?.scenarios.find((scenario) => scenario.id === state.selectedId) ?? null;
}

function renderScenario() {
  const scenario = selectedScenario();
  if (!scenario) return;
  elements.purpose.textContent = scenario.purpose;
  elements.count.textContent = `${scenario.row_count} synthetic ${scenario.row_count === 1 ? "invoice" : "invoices"}`;
  elements.provenance.textContent = `Fixture v${scenario.provenance.fixture_version} · ${scenario.provenance.license_status}`;
  replaceTextList(elements.source, scenario.source_schema);
  replaceTextList(elements.destination, scenario.destination_schema);
}

function populateScenarios(catalogue) {
  state.catalogue = catalogue;
  state.selectedId = catalogue.default_scenario_id;
  elements.select.replaceChildren();
  for (const scenario of catalogue.scenarios) {
    const option = document.createElement("option");
    option.value = scenario.id;
    option.textContent = scenario.title;
    elements.select.append(option);
  }
  elements.select.value = state.selectedId;
  elements.select.disabled = false;
  elements.run.disabled = false;
  renderScenario();
}

function showCatalogueError() {
  elements.purpose.textContent = "The local scenario catalogue could not be loaded. Reload to try again.";
  elements.status.textContent = "Scenario catalogue unavailable.";
}

function evidenceList(title, items, className = "result-list") {
  const section = node("section", { className: "result-section" });
  section.append(node("h4", { text: title }));
  const list = node("ul", { className });
  for (const item of items) list.append(item);
  section.append(list);
  return section;
}

function dataTable(title, columns, rows) {
  const article = node("article", { className: "sample-panel" });
  article.append(node("h4", { text: title }));
  const table = node("table");
  const head = node("thead");
  const headRow = node("tr");
  for (const column of columns) {
    const cell = node("th", { text: column.label });
    cell.setAttribute("scope", "col");
    headRow.append(cell);
  }
  head.append(headRow);
  const body = node("tbody");
  for (const row of rows) {
    const tableRow = node("tr");
    for (const column of columns) tableRow.append(node("td", { text: String(row[column.key]) }));
    body.append(tableRow);
  }
  table.append(head, body);
  article.append(table);
  return article;
}

function revealOutput() {
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  elements.output.scrollIntoView({ behavior, block: "start" });
}

function hideRunningStatus() {
  elements.runningStatus.hidden = true;
  elements.runningStatus.textContent = "";
}

function showRunningStatus(message) {
  elements.runningStatus.textContent = message;
  elements.runningStatus.hidden = false;
  elements.runningStatus.focus({ preventScroll: true });
}

function cleanupConfirmed(cleanupState) {
  return ["complete", "not_needed"].includes(cleanupState);
}

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isUsableRehearsalPayload(responseOk, payload) {
  if (!isRecord(payload)) return false;
  if (!responseOk) {
    return (
      ["complete", "not_needed", "blocked_identity_drift"].includes(payload.cleanup_state)
      && isRecord(payload.error)
      && [payload.error.title, payload.error.detail, payload.error.recovery].every(
        (value) => typeof value === "string"
      )
    );
  }
  const evidence = payload.evidence;
  return (
    payload.cleanup_state === "complete"
    && [payload.run_id, payload.verdict_label, payload.summary].every((value) => typeof value === "string")
    && [payload.source_rows, payload.destination_rows].every(Array.isArray)
    && Array.isArray(payload.stages)
    && payload.stages.every(
      (stage) => isRecord(stage) && [stage.label, stage.status, stage.explanation].every(
        (value) => typeof value === "string"
      )
    )
    && Array.isArray(payload.checks)
    && payload.checks.every(
      (check) => isRecord(check) && [check.label, check.status, check.detail].every(
        (value) => typeof value === "string"
      )
    )
    && Array.isArray(payload.rollback_plan)
    && payload.rollback_plan.every(
      (step) => isRecord(step) && Number.isInteger(step.order) && typeof step.label === "string"
    )
    && Array.isArray(payload.warnings)
    && payload.warnings.every(
      (warning) => isRecord(warning) && [warning.label, warning.detail].every(
        (value) => typeof value === "string"
      )
    )
    && isRecord(evidence)
    && typeof evidence.postgresql_version === "string"
    && typeof evidence.operation_limit === "string"
    && isRecord(evidence.migration_sha256)
    && ["0001", "0002", "0003", "0004", "0005"].every(
      (version) => typeof evidence.migration_sha256[version] === "string"
    )
  );
}

function errorRecovery(payload) {
  return cleanupConfirmed(payload.cleanup_state)
    ? payload.error.recovery
    : UNCONFIRMED_CLEANUP_RECOVERY;
}

function renderResult(payload) {
  hideRunningStatus();
  elements.content.replaceChildren();
  const verdict = node("header", { className: "verdict" });
  const label = node("p", { className: "result-kicker", text: "Current bounded result" });
  const title = node("h3", { text: payload.verdict_label });
  title.tabIndex = -1;
  const summary = node("p", { className: "verdict-summary", text: payload.summary });
  const cleanup = node("p", {
    className: "cleanup-state",
    text: payload.cleanup_state === "complete" ? "Cleanup complete" : "Cleanup requires attention"
  });
  verdict.append(label, title, summary, cleanup);

  const checkItems = payload.checks.map((check) => {
    const item = node("li", { className: `check-item status-${check.status}` });
    item.append(
      node("span", { className: "status-word", text: check.status.toUpperCase() }),
      node("strong", { text: check.label }),
      node("p", { text: check.detail })
    );
    return item;
  });

  const rollbackItems = payload.rollback_plan.map((step) => {
    const item = node("li", { className: "rollback-item" });
    item.append(
      node("span", { className: "rollback-order", text: String(step.order).padStart(2, "0") }),
      node("span", { text: step.label }),
      node("small", { text: step.observed ? "Observed locally" : "Planned only" })
    );
    return item;
  });

  const warningItems = payload.warnings.map((warning) => {
    const item = node("li", { className: "warning-item" });
    item.append(node("strong", { text: warning.label }), node("p", { text: warning.detail }));
    return item;
  });

  const digestItems = Object.entries(payload.evidence.migration_sha256).map(([version, digest]) => {
    const item = node("li");
    item.append(node("span", { text: `${version} · ${digest}` }));
    return item;
  });

  const evidence = node("section", { className: "raw-evidence" });
  evidence.append(node("h4", { text: "Migration evidence and digests" }));
  const evidenceMeta = node("dl", { className: "evidence-meta" });
  for (const [term, value] of [
    ["Engine", "Existing PMR migration, backfill, and down interfaces"],
    ["Database", `PostgreSQL ${payload.evidence.postgresql_version} · disposable local instance`],
    ["Run ID", payload.run_id],
    ["Limit", payload.evidence.operation_limit]
  ]) {
    evidenceMeta.append(node("dt", { text: term }), node("dd", { text: value }));
  }
  const digestList = node("ul", { className: "digest-list" });
  for (const item of digestItems) digestList.append(item);
  evidence.append(evidenceMeta, digestList);

  const grid = node("div", { className: "result-grid" });
  grid.append(
    evidenceList("Checks and gates", checkItems),
    evidenceList("Rollback plan", rollbackItems, "rollback-list")
  );
  const evidenceStamp = node("p", {
    className: "evidence-stamp",
    text: `Evidence reference · 0001 · ${payload.evidence.migration_sha256["0001"]}`
  });
  const stageTrail = node("section", { className: "observed-stages" });
  stageTrail.id = "result-stages";
  stageTrail.append(node("h4", { text: "Observed stage trail" }));
  const stageList = node("ol", { className: "observed-stage-list" });
  for (const [index, stage] of payload.stages.entries()) {
    const item = node("li", { className: `observed-stage status-${stage.status}` });
    item.append(
      node("span", { className: "observed-stage-number", text: String(index + 1).padStart(2, "0") }),
      node("strong", { text: stage.label }),
      node("small", { className: "observed-stage-status", text: stage.status.replaceAll("_", " ").toUpperCase() }),
      node("p", { text: stage.explanation })
    );
    stageList.append(item);
  }
  stageTrail.append(stageList);
  const technical = node("details", { className: "result-details" });
  technical.id = "result-technical";
  technical.append(node("summary", { text: "Inspect technical stages and digests" }));
  technical.append(stageTrail, evidence);
  const emptyNotice = payload.destination_rows.length === 0
    ? node("p", { className: "empty-notice", text: "No destination rows were created." })
    : null;
  const comparison = node("section", { className: "sample-comparison" });
  comparison.id = "result-comparison";
  if (payload.source_rows.length > 0) {
    comparison.append(
      dataTable(
        "Source sample",
        [
          { key: "invoice_id", label: "Invoice" },
          { key: "amount_cents", label: "Amount cents" }
        ],
        payload.source_rows
      ),
      dataTable(
        "Destination sample",
        [
          { key: "invoice_id", label: "Invoice" },
          { key: "amount_minor", label: "Amount minor" },
          { key: "currency_code", label: "Currency" }
        ],
        payload.destination_rows
      )
    );
  }
  elements.content.append(
    verdict,
    evidenceStamp,
    ...(emptyNotice ? [emptyNotice] : []),
    ...(payload.source_rows.length > 0 ? [comparison] : []),
    grid,
    evidenceList("Limits to carry forward", warningItems),
    technical
  );
  elements.output.hidden = false;
  title.focus({ preventScroll: true });
  revealOutput();
}

function renderError(payload) {
  hideRunningStatus();
  elements.content.replaceChildren();
  const box = node("section", { className: "error-state" });
  const title = node("h3", { text: payload.error.title });
  title.tabIndex = -1;
  box.append(
    node("p", { className: "result-kicker", text: payload.verdict === "INPUT_REJECTED" ? "Input rejected" : "Run interrupted" }),
    title,
    node("p", { text: payload.error.detail }),
    node("p", { className: "recovery", text: errorRecovery(payload) })
  );
  if (payload.error.field) {
    box.append(node("p", { className: "error-field", text: `Field · ${payload.error.field}` }));
  }
  if (payload.cleanup_state === "not_needed") {
    box.append(node("p", { className: "cleanup-state error-cleanup", text: "No database operation started." }));
  } else if (payload.cleanup_state === "complete") {
    box.append(node("p", { className: "cleanup-state error-cleanup", text: "Cleanup complete" }));
  } else {
    box.append(node("p", { className: "cleanup-state error-cleanup", text: "Cleanup not confirmed" }));
  }
  elements.content.append(box);
  elements.output.hidden = false;
  title.focus({ preventScroll: true });
  revealOutput();
}

function setRunning(running) {
  elements.form.setAttribute("aria-busy", String(running));
  elements.run.disabled = running;
  elements.select.disabled = running;
  elements.run.textContent = running ? "Running local stages…" : "Run local rehearsal";
}

function blockRunUntilCleanupReview() {
  state.cleanupBlocked = true;
  elements.form.setAttribute("aria-busy", "false");
  elements.select.disabled = true;
  elements.run.disabled = true;
  elements.run.textContent = "Run unavailable";
  elements.recovery.textContent = UNCONFIRMED_CLEANUP_RECOVERY;
  elements.recovery.hidden = false;
  elements.status.textContent = UNCONFIRMED_CLEANUP_RECOVERY;
}

async function loadCatalogue() {
  try {
    const response = await fetch("/api/v1/scenarios", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("catalogue request failed");
    populateScenarios(await response.json());
  } catch {
    showCatalogueError();
  }
}

function renderHistoricalEvidence(payload) {
  elements.phaseB.replaceChildren();
  const statusRow = node("div", { className: "historical-status" });
  statusRow.append(node("strong", { text: `${payload.report_gate_count} / ${payload.report_gate_count} gates passed` }));
  const boundary = node("p", {
    className: "historical-boundary",
    text: "Historical only; this control room cannot rerun or alter Phase B."
  });
  const records = node("div", { className: "historical-records" });
  const rawRecord = node("article", { className: "historical-record" });
  rawRecord.append(
    node("p", { className: "historical-record-label", text: "Raw terminal record" }),
    node("strong", {
      className: "historical-record-status",
      text: payload.result_status.replace(/^PHASE_B_GATES_PASSED_/, "")
    }),
    node("p", { text: "Phase B ended here; this terminal record did not contain a completed independent review." })
  );
  const reviewRecord = node("article", { className: "historical-record" });
  reviewRecord.append(
    node("p", { className: "historical-record-label", text: "Later review record" }),
    node("strong", { className: "historical-record-status", text: payload.independent_review_status }),
    node("p", {
      text: "Its raw reviewer output was not retained, so this is not durable independent-review proof."
    })
  );
  records.append(rawRecord, reviewRecord);
  const metrics = node("dl", { className: "historical-metrics" });
  for (const [term, value] of [
    ["Test suite", `${payload.summary.tests_passed} tests`],
    ["Branch evidence", `${payload.summary.covered_branches} / ${payload.summary.total_branches} branches`],
    ["Measured coverage", `${payload.summary.branch_coverage_percent}%`]
  ]) {
    const item = node("div");
    item.append(node("dt", { text: term }), node("dd", { text: value }));
    metrics.append(item);
  }
  const provenance = node("dl", { className: "historical-provenance" });
  for (const [term, value] of [
    ["Source commit", payload.source_commit],
    ["Terminal manifest status", payload.result_status]
  ]) {
    provenance.append(node("dt", { text: term }), node("dd", { text: value }));
  }
  const detail = node("details", { className: "historical-detail" });
  detail.append(node("summary", { text: "Artifact digest references" }));
  const digests = node("dl", { className: "historical-provenance" });
  for (const [term, value] of [
    ["Report SHA-256", payload.report_sha256],
    ["Result SHA-256", payload.result_sha256],
    ["Freeze / protocol SHA-256", payload.protocol_sha256]
  ]) {
    digests.append(node("dt", { text: term }), node("dd", { text: value }));
  }
  const limits = node("ul", { className: "historical-limits" });
  for (const limit of payload.limits) limits.append(node("li", { text: limit }));
  detail.append(digests);
  const reference = node("p", {
    className: "historical-reference",
    text: `Report evidence · ${payload.report_sha256}`
  });
  elements.phaseB.append(statusRow, boundary, records, metrics, provenance, limits, reference, detail);
}

async function loadHistoricalEvidence() {
  try {
    const response = await fetch("/api/v1/evidence/phase-b", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("historical evidence request failed");
    renderHistoricalEvidence(await response.json());
  } catch {
    elements.phaseB.replaceChildren(
      node("h3", { text: "Historical record unavailable" }),
      node("p", { text: "The local Phase A control remains separate and usable." })
    );
  }
}

elements.select.addEventListener("change", () => {
  state.selectedId = elements.select.value;
  renderScenario();
});

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.selectedId) return;
  const generation = ++state.requestGeneration;
  state.activeRun = true;
  let settledCleanupState = "unconfirmed";
  elements.recovery.hidden = true;
  setRunning(true);
  const runningMessage = "Local rehearsal started. The disposable database stages are running.";
  elements.status.textContent = runningMessage;
  showRunningStatus(runningMessage);
  try {
    const response = await fetch("/api/v1/rehearsals", {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ scenario_id: state.selectedId })
    });
    const payload = await response.json();
    if (!isUsableRehearsalPayload(response.ok, payload)) throw new Error("unusable rehearsal response");
    settledCleanupState = payload.cleanup_state;
    if (generation !== state.requestGeneration) return;
    if (response.ok) {
      renderResult(payload);
      elements.status.textContent = `${payload.verdict_label}. Review the checks and evidence below.`;
    } else {
      renderError(payload);
      elements.status.textContent = `${payload.error.title}. ${errorRecovery(payload)}`;
    }
  } catch {
    settledCleanupState = "unconfirmed";
    if (generation !== state.requestGeneration) return;
    const payload = {
      cleanup_state: "unconfirmed",
      error: {
        title: "Local rehearsal unavailable",
        detail: "The local service did not return a usable result.",
        recovery: UNCONFIRMED_CLEANUP_RECOVERY
      }
    };
    renderError(payload);
    elements.status.textContent = `Local rehearsal unavailable. ${errorRecovery(payload)}`;
  } finally {
    state.activeRun = false;
    setRunning(false);
    if (generation !== state.requestGeneration) {
      if (cleanupConfirmed(settledCleanupState)) {
        state.cleanupBlocked = false;
        elements.recovery.hidden = true;
        elements.status.textContent = "Control room reset. The previous local cleanup completed.";
      } else {
        blockRunUntilCleanupReview();
      }
    } else if (!cleanupConfirmed(settledCleanupState)) {
      blockRunUntilCleanupReview();
    }
  }
});

elements.reset.addEventListener("click", () => {
  const cleanupPending = state.activeRun;
  state.requestGeneration += 1;
  hideRunningStatus();
  if (state.catalogue) {
    state.selectedId = state.catalogue.default_scenario_id;
    elements.select.value = state.selectedId;
    renderScenario();
  }
  elements.content.replaceChildren();
  elements.output.hidden = true;
  if (state.cleanupBlocked) {
    blockRunUntilCleanupReview();
    elements.reset.focus();
    return;
  }
  setRunning(cleanupPending);
  if (cleanupPending) {
    elements.status.textContent = "View reset. The current local cleanup is still finishing.";
    elements.recovery.textContent = "The current local cleanup is still finishing.";
    elements.recovery.hidden = false;
    elements.reset.focus();
  } else {
    elements.recovery.hidden = true;
    elements.status.textContent = "Control room reset to the initial scenario.";
    elements.select.focus();
  }
});

void loadCatalogue();
void loadHistoricalEvidence();
