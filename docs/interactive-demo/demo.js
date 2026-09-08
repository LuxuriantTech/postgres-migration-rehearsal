"use strict";

const byId = (id) => document.getElementById(id);
const text = (node, value) => { node.textContent = value; return node; };
const element = (name, className, value) => {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (value !== undefined) text(node, value);
  return node;
};
const reset = (ids) => ids.forEach((id) => { const node = byId(id); if (node) node.value = node.dataset.initial || ""; });

const toolTraces = {
  safe: { name: "Approved address lookup", verdict: "PASS", reason: "Illustrative safe-trace outcome.", events: [
    ["01", "request", "lookup_customer", "bounded account 42 lookup"], ["02", "approval", "manager", "approval recorded"], ["03", "update", "address", "approved narrow update"]
  ] },
  risky: { name: "Risky directory update", verdict: "FAIL", reason: "Illustrative risky-trace outcome: three named concerns.", events: [
    ["01", "request", "directory_export", "forbidden directory export"], ["02", "request", "lookup_customer", "over-broad customer lookup"], ["03", "update", "address", "approval missing"]
  ] }
};
function renderToolCall() {
  const trace = toolTraces[byId("trace-select").value];
  const filter = byId("event-filter").value.trim().toLowerCase();
  const output = byId("tool-output"); output.replaceChildren();
  const state = element("p", `demo-status ${trace.verdict === "FAIL" ? "fail" : "pass"}`);
  state.append(`${trace.verdict}: ${trace.reason}`); output.append(state);
  const note = element("p", "quiet", trace.verdict === "FAIL" ? "This hand-authored example illustrates an expected FAIL verdict. The browser did not run an evaluator, tool, or network request." : "This hand-authored example illustrates a safe trace. The browser did not run an evaluator, tool, or network request."); output.append(note);
  const list = element("div", "demo-rows"); let visible = 0;
  trace.events.forEach(([step, type, action, detail]) => {
    const item = element("article", "demo-row");
    const searchable = `${step} ${type} ${action} ${detail}`.toLowerCase();
    if (filter && !searchable.includes(filter)) item.hidden = true; else visible += 1;
    item.append(element("strong", "", `${step}  ${type}: ${action}`), element("p", "", detail)); list.append(item);
  });
  output.append(list); byId("tool-live").textContent = `${trace.name}: ${trace.verdict}; ${visible} events shown.`;
}

const entityPairs = {
  review: { label: "Harbor Desk Lamp", decision: "REVIEW", source: "Preserved local walkthrough capture reference", a: ["Harbor Desk Lamp", "Harbor", "HBL8", "€69.00"], b: ["Harbor Desk Lamp", "Harbor", "HBL9", "€71.00"], overall: 0.74, reasons: ["Same normalized name and brand.", "SKU conflicts: hbl8 versus hbl9.", "Another candidate shares the top name score.", "Below the shown explanatory threshold."], metrics: [["Name", "1.00"], ["Brand", "1.00"], ["SKU", "0.00"], ["Price", "0.89"]] },
  match: { label: "Lumen Task Chair", decision: "MATCH illustration", source: "Hand-authored synthetic illustration", a: ["Lumen Task Chair", "Lumen", "LTC-04", "€219.00"], b: ["Lumen Task Chair", "Lumen", "LTC-04", "€219.00"], overall: 1.00, reasons: ["Name, brand, SKU, and price agree in this illustrative pair.", "MATCH is a hand-authored label; the browser did not run the matcher."], metrics: [["Name", "1.00"], ["Brand", "1.00"], ["SKU", "1.00"], ["Price", "1.00"]] },
  noMatch: { label: "Stoneware Mug", decision: "NO_MATCH illustration", source: "Hand-authored synthetic illustration", a: ["Stoneware Mug", "Morrow", "MUG-8", "€18.00"], b: ["Insulated Travel Flask", "Arc", "FLK-8", "€32.00"], overall: 0.13, reasons: ["Name, brand, SKU, and price disagree.", "NO_MATCH is a hand-authored label; the browser did not run the matcher."], metrics: [["Name", "0.12"], ["Brand", "0.00"], ["SKU", "0.20"], ["Price", "0.19"]] }
};
function recordCard(title, values) {
  const card = element("article", "panel record"); card.append(element("h3", "", title)); const list = element("dl", "keyvals");
  [["Name", values[0]], ["Brand", values[1]], ["SKU", values[2]], ["Price", values[3]]].forEach(([key, value]) => { list.append(element("dt", "", key), element("dd", "", value)); }); card.append(list); return card;
}
function renderEntity() {
  const pair = entityPairs[byId("entity-select").value]; const output = byId("entity-output"); output.replaceChildren();
  output.append(element("p", `demo-status ${pair.decision === "REVIEW" ? "" : ""}`, `${pair.decision}: ${pair.label}. Overall similarity ${pair.overall.toFixed(2)}.`));
  const compare = element("div", "comparison"); compare.append(recordCard("Catalogue A", pair.a), recordCard("Catalogue B", pair.b)); output.append(compare);
  const details = element("div", "grid two spaced"); const reasons = element("section", ""); reasons.append(element("h3", "", "Why this prepared decision")); const list = element("ul", "reason-list"); pair.reasons.forEach((reason) => list.append(element("li", "", reason))); reasons.append(list);
  const scores = element("section", "panel"); scores.append(element("h3", "", "Prepared similarities")); const scoreList = element("ul", "score-list"); pair.metrics.forEach(([name, score]) => { const item = element("li", ""); item.append(element("span", "", name), element("strong", "", score)); scoreList.append(item); }); scores.append(scoreList); details.append(reasons, scores); output.append(details);
  output.append(element("p", "quiet", `${pair.source}. Values are illustrative similarities, not probabilities. The threshold only changes the explanation; it does not rerun the local matcher.`));
  const input = byId("threshold"); const error = byId("threshold-error"); const raw = input.value.trim(); const threshold = Number(raw); const valid = raw !== "" && Number.isFinite(threshold) && threshold >= 0 && threshold <= 1;
  input.setCustomValidity(valid ? "" : "Enter a finite number from 0.00 to 1.00."); input.setAttribute("aria-invalid", String(!valid)); error.hidden = valid; text(error, valid ? "" : "Enter a finite number from 0.00 to 1.00.");
  if (valid) { const thresholdNote = element("p", ""); thresholdNote.append(`Explanation threshold: ${threshold.toFixed(2)}. This pair's illustrated similarity is ${pair.overall.toFixed(2)}, so it is ${pair.overall >= threshold ? "at or above" : "below"} the selected explanatory threshold.`); output.append(thresholdNote); }
  else output.append(element("p", "demo-status fail", "The threshold is invalid, so no threshold comparison is shown."));
  byId("entity-live").textContent = valid ? `${pair.label}, ${pair.decision}; illustrative similarity ${pair.overall.toFixed(2)}.` : "Enter a threshold from 0.00 to 1.00.";
}

const pmrStages = [
  ["Expand", "Add nullable amount_minor and currency_code columns."], ["Compatibility", "Permit temporary old and new write shapes."], ["Backfill", "Resume bounded batches and check idempotence."], ["Switch", "Move reads and writes to the target shape."], ["Contract", "Remove the legacy column after the boundary closes."]
];
function renderPmr() {
  const selected = Number(byId("pmr-stage").value); const output = byId("pmr-output"); output.replaceChildren();
  output.append(element("p", "demo-status fail", "Prepared preview only: current local engine is BLOCKED by the frozen image identity check."));
  output.append(element("p", "quiet", "Changing a stage explains the prepared six-invoice report. It does not start PostgreSQL, Docker, or a migration."));
  const list = element("div", ""); pmrStages.forEach(([name, description], index) => { const step = element("div", "stage-step"); if (index === selected) step.setAttribute("aria-current", "step"); step.append(element("span", "stage-number", String(index + 1)), element("div", "", `${name}: ${description}`)); list.append(step); }); output.append(list);
  const report = element("section", "panel spaced"); report.append(element("h3", "", "Prepared report excerpt")); report.append(element("p", "", selected < 4 ? `Selected stage: ${pmrStages[selected][0]}. The example retains invoice IDs 101 to 106 and records validation before a later switch.` : "Selected stage: Contract. The prepared report separates a completed migration from confirmed cleanup.")); report.append(element("p", "quiet", "Historical Phase B remains closed and is not represented by this control.")); output.append(report);
  byId("pmr-live").textContent = `Prepared PMR stage ${selected + 1}: ${pmrStages[selected][0]}; engine remains blocked.`;
}

const contracts = {
  removed: { title: "GET /orders removed", category: "OPERATION_REMOVED", before: "GET /orders\n200 -> Order[]", after: "No /orders path", report: "Hand-authored illustration: GET /orders is removed." },
  parameter: { title: "Required query parameter added", category: "REQUIRED_PARAMETER_ADDED", before: "GET /invoices\nquery: none", after: "GET /invoices\nquery: region (required)", report: "Hand-authored illustration: required parameter region is added." },
  response: { title: "Required response property removed", category: "REQUIRED_RESPONSE_PROPERTY_REMOVED", before: "GET /profile\n200: { id, email }", after: "GET /profile\n200: { id }", report: "Hand-authored illustration: required response property email is removed." }
};
function renderContract() {
  const item = contracts[byId("contract-select").value]; const output = byId("contract-output"); output.replaceChildren(); output.append(element("p", "demo-status fail", `${item.category}: ${item.report}`));
  const grid = element("div", "comparison"); const before = element("section", ""); before.append(element("h3", "", "Before"), element("pre", "contract", item.before)); const after = element("section", ""); after.append(element("h3", "", "After"), element("pre", "contract", item.after)); grid.append(before, after); output.append(grid);
  output.append(element("p", "quiet", "This is a hand-authored illustrative report on synthetic snippets. It does not run the TypeScript CLI or claim coverage beyond the five documented categories."));
  byId("contract-live").textContent = `${item.title}; prepared ${item.category} report shown.`;
}

const evidenceCases = {
  answer: { question: "What annual platform amount is stated?", answer: "EUR 48,000", file: "northstar_master_services_agreement.pdf, page 1", quote: "Annual platform fee: EUR 48,000, invoiced quarterly.", note: "Prepared illustration based on a synthetic document excerpt. It is not a live model answer." },
  abstain: { question: "What is the supplier's VAT registration number?", answer: "Abstain: not stated in the supplied excerpt.", file: "northstar_master_services_agreement.pdf, page 1", quote: "The excerpt names a platform fee but contains no VAT registration number.", note: "Prepared abstention example. It demonstrates the intended product behavior, not a new evaluation result." },
  limitation: { question: "Can this answer be trusted without checking the source?", answer: "No. The frozen v7 evaluation did not meet its target.", file: "Frozen v7 evaluation record", quote: "Answerable accuracy was 9/25 and extraction F1 was 45.67%.", note: "Known limitation: retrieval alone did not establish correct answers or complete supporting evidence." }
};
function renderEvidence() {
  const item = evidenceCases[byId("evidence-select").value]; const output = byId("evidence-output"); output.replaceChildren(); output.append(element("h3", "", item.question)); output.append(element("p", "demo-status", item.answer)); const source = element("section", "source-quote"); source.append(element("strong", "", item.file), element("p", "", item.quote)); output.append(source, element("p", "quiet", item.note)); byId("evidence-live").textContent = `Prepared EvidenceDesk example: ${item.answer}`;
}

function setUp(page) {
  const map = { toolcall: [renderToolCall, ["trace-select", "event-filter"]], entity: [renderEntity, ["entity-select", "threshold"]], pmr: [renderPmr, ["pmr-stage"]], contract: [renderContract, ["contract-select"]], evidence: [renderEvidence, ["evidence-select"]] };
  const entry = map[page]; if (!entry) return; const [render, ids] = entry;
  ids.forEach((id) => byId(id).addEventListener("input", render)); byId(`${page}-reset`).addEventListener("click", () => { reset(ids); render(); byId(ids[0]).focus(); }); render();
}
document.addEventListener("DOMContentLoaded", () => setUp(document.body.dataset.demo));
