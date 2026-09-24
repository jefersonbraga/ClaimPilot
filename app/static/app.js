// ClaimPilot demo workbench — vanilla JS. Talks only to the public API:
//   GET /demo/scenarios · POST /claims/analyze · GET /executions/{id} · GET /evals/latest · GET /health
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x === null || x === undefined ? "n/a" : `${Math.round(x * 100)}%`);

const TOOL_NAMES = {
  check_member_eligibility: "Eligibility check",
  get_claim_history: "Claim history check",
  check_prior_authorization: "Prior authorization check",
  check_emergency_exemption: "Emergency exemption check",
  check_network: "Network check",
  calculate_financial_threshold: "Financial threshold check",
};
const TOOL_ICON = { PASS: "✓", FAIL: "✕", INDETERMINATE: "?" };
const VERDICT = {
  APPROVE: { cls: "approve", label: "Approve", pill: "No manual review required",
    sub: "All deterministic checks passed, the model agreed, and every citation was verified against policy text." },
  DENY: { cls: "deny", label: "Deny", pill: "Recommended denial",
    sub: "A deterministic rule failed and no exemption applied; the model concurred. In production, denials would go to an analyst for sign-off." },
  HUMAN_REVIEW: { cls: "review", label: "Human review", pill: "Escalated to an analyst",
    sub: "ClaimPilot did not decide on its own. The case goes to an analyst with the reasons and evidence below." },
};

const state = { scenarios: [], current: null, last: null, replay: null, busy: false, revision: 0, retrying: false,
                providers: [], provider: null };

// Same workflow, rules and guardrails for every provider: only the model changes.
const PROVIDER_INFO = {
  deepseek: { name: "DeepSeek", hint: "Default · about 2 s per claim" },
  groq: { name: "Groq", hint: "Alternative · free-tier rate limits can slow it down; a refused call falls back safely to human review" },
  openai: { name: "OpenAI-compatible", hint: "Generic OpenAI-compatible endpoint" },
  mock: { name: "Mock", hint: "Deterministic stand-in: no API key is configured on this server" },
};
const currentProvider = () => state.providers.find((p) => p.id === state.provider) || state.providers[0] || null;

function renderProviders(list) {
  state.providers = list || [];
  const def = state.providers.find((p) => p.default) || state.providers[0];
  state.provider = def ? def.id : null;
  $("#model-picker").hidden = state.providers.length < 2;
  $("#model-options").innerHTML = state.providers.map((p) => `
    <button type="button" role="radio" data-provider="${esc(p.id)}" aria-checked="${p.id === state.provider}">
      <b>${esc((PROVIDER_INFO[p.id] || { name: p.id }).name)}${p.default ? " · default" : ""}</b><span>${esc(p.model)}</span>
    </button>`).join("");
  selectProvider(state.provider);
}
function selectProvider(id) {
  state.provider = id;
  document.querySelectorAll("#model-options [data-provider]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.provider === id)));
  $("#model-hint").textContent = (PROVIDER_INFO[id] || { hint: "" }).hint;
}

// ------------------------------------------------------------------ API

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error((body && body.detail && typeof body.detail === "string") ? body.detail : `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

// ------------------------------------------------------------------ scenarios

async function loadScenarios() {
  try {
    state.scenarios = await api("/demo/scenarios");
  } catch (e) {
    $("#scenarios").innerHTML = `<p class="muted">Could not load scenarios (${esc(e.message)}).</p>`;
    return;
  }
  $("#scenarios").innerHTML = state.scenarios.map((s) => `
    <button type="button" class="scenario" role="radio" aria-checked="false" data-id="${esc(s.id)}" tabindex="-1">
      <span class="scenario-title">${esc(s.label)}${s.default ? '<span class="scenario-star">Start here</span>' : ""}</span>
      <span class="scenario-sub">${esc(s.summary)}</span>
    </button>`).join("");
  document.querySelectorAll(".scenario").forEach((el) => el.addEventListener("click", () => selectScenario(el.dataset.id, true)));
  $("#scenarios").addEventListener("keydown", onScenarioKeys);
  const first = state.scenarios.find((s) => s.default) || state.scenarios[0];
  selectScenario(first.id, false);
}

function selectScenario(id, focus) {
  state.current = state.scenarios.find((s) => s.id === id);
  document.querySelectorAll(".scenario").forEach((el) => {
    const on = el.dataset.id === id;
    el.setAttribute("aria-checked", String(on));
    el.tabIndex = on ? 0 : -1;
    if (on && focus) el.focus();
  });
  $("#empty-state p").innerHTML = `The <b>${esc(state.current.label)}</b> scenario is selected. Click <b>Analyze Claim</b> to see the recommendation, evidence and recorded workflow.`;
  $("#watch").hidden = false;
  $("#watch-text").textContent = state.current.watch;
  setClaimText(state.current.claim);
}

function onScenarioKeys(e) {
  const keys = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
  if (!(e.key in keys)) return;
  e.preventDefault();
  const i = state.scenarios.findIndex((s) => s.id === state.current.id);
  const next = state.scenarios[(i + keys[e.key] + state.scenarios.length) % state.scenarios.length];
  selectScenario(next.id, true);
}

// ------------------------------------------------------------------ claim editor

function setClaimText(claim) {
  $("#claim-json").value = JSON.stringify(claim, null, 2);
  onClaimEdited();
}

function parseClaim() {
  try {
    const claim = JSON.parse($("#claim-json").value);
    if (typeof claim !== "object" || claim === null || Array.isArray(claim)) throw new Error("Claim must be a JSON object.");
    return { claim };
  } catch (e) {
    return { error: e.message };
  }
}

function onClaimEdited() {
  state.revision += 1;
  resetFlow();
  updateResultContext();
  const { claim, error } = parseClaim();
  const status = $("#json-status");
  $("#claim-json").classList.toggle("invalid", !!error);
  status.classList.toggle("error", !!error);
  status.textContent = error ? `Invalid JSON: ${error}` : "Valid JSON · edit any field and re-analyze";
  if (claim) renderClaimSummary(claim);
  else $("#claim-summary").textContent = "Fix the JSON to preview this claim.";
}

function renderClaimSummary(c) {
  const money = (v) => (typeof v === "number" ? `$${v.toLocaleString("en-US")}` : null);
  const bool = (v) => (v === true ? "Yes" : v === false ? "No" : null);
  const chips = [
    ["Procedure", c.procedure],
    ["Amount", money(c.amount)],
    ["Plan", c.plan],
    ["Prior auth", bool(c.prior_authorization)],
    ["Emergency", c.emergency_indicator === null || c.emergency_indicator === undefined ? "Unknown" : bool(c.emergency_indicator), c.emergency_indicator == null ? "unknown" : ""],
    ["Service date", c.date_of_service],
  ];
  $("#claim-summary").innerHTML = chips.map(([k, v, cls]) => {
    const missing = v === null || v === undefined || v === "";
    return `<div class="chip ${missing ? "missing" : cls || ""}"><span>${esc(k)}</span><b>${missing ? "Missing" : esc(v)}</b></div>`;
  }).join("");
}

// ------------------------------------------------------------------ analyze

// ------------------------------------------------------------------ live progress (real, not simulated)

// The API streams one event per completed LangGraph node when asked for NDJSON; the diagram follows it.
const NODE_TO_STEP = {
  validate_claim: "validate", retrieve_policy: "retrieve", check_eligibility: "tools", check_authorization: "tools",
  refine_retrieval: "refine", analyze_claim: "llm", evaluate_risk: "risk", generate_recommendation: "decide",
  escalate_to_human: "review", record_audit: "audit",
};
const NEXT_STEP = { validate: "retrieve", retrieve: "tools", tools: "refine", refine: "llm", llm: "risk", decide: "audit", review: "audit" };
const LIVE_MIN_MS = 320;  // local steps finish in microseconds; pace them just enough to be followed by eye
const live = { active: false, revision: -1, done: new Set(), skipped: new Set(), running: null, chain: Promise.resolve(), lastPaint: 0 };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function startLive(revision) {
  Object.assign(live, { active: true, revision, done: new Set(), skipped: new Set(), running: "validate", chain: Promise.resolve(), lastPaint: performance.now() });
  paintLive();
  const board = $("#flow-board").getBoundingClientRect();
  if (board.top < 70 || board.bottom > window.innerHeight) {
    $("#how").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
  }
}
function stopLive() {
  live.active = false;
  live.running = null;
  document.querySelectorAll("#flow [data-step]").forEach((n) => n.classList.remove("live-done", "live-running", "live-pending"));
}
async function applyLive(event) {
  if (!live.active || live.revision !== state.revision) return;
  const wait = reducedMotion() ? 0 : LIVE_MIN_MS - (performance.now() - live.lastPaint);
  if (wait > 0) await sleep(wait);
  if (!live.active || live.revision !== state.revision) return;  // inputs changed mid-flight: stop following
  const step = NODE_TO_STEP[event.node];
  if (!step) return;
  if (event.node === "check_eligibility") {
    live.running = "tools";  // the facts step spans two graph nodes
  } else {
    live.done.add(step);
    if (step === "refine" && (event.trail || "").includes("skipped")) live.skipped.add("refine");
    live.running = NEXT_STEP[step] || null;
  }
  live.lastPaint = performance.now();
  paintLive();
}
function paintLive() {
  document.querySelectorAll("#flow [data-step]").forEach((node) => {
    const key = node.dataset.step, done = live.done.has(key), running = live.running === key;
    node.classList.toggle("live-done", done);
    node.classList.toggle("live-running", running);
    node.classList.toggle("live-pending", !done && !running);
    node.classList.remove("visited", "skipped", "replay-current");
    node.querySelector(".node-state").textContent = running
      ? (key === "llm" ? "● Model call in progress…" : "● Running…")
      : done ? (live.skipped.has(key) ? "— Nothing to add" : "✓ Done") : "Waiting";
  });
  $("#flow-status").textContent = `Investigating · live · ${live.running ? FLOW_STEPS[live.running].title + "…" : "routing the outcome…"}`;
  drawFlowEdges();
}

async function requestAnalysis(claim, onNode) {
  const chosen = currentProvider();
  const query = chosen && !chosen.default ? `?provider=${encodeURIComponent(chosen.id)}` : "";
  const res = await fetch(`/claims/analyze${query}`, {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" }, body: JSON.stringify(claim),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const err = new Error((body && typeof body.detail === "string") ? body.detail : `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  // A server (or proxy) that ignores the Accept header returns plain JSON: still works, just without progress.
  if (!(res.headers.get("content-type") || "").includes("ndjson") || !res.body) return res.json();
  const reader = res.body.getReader(), decoder = new TextDecoder();
  let buffer = "", decision = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      const event = JSON.parse(line);
      if (event.event === "node") onNode(event);
      else if (event.event === "decision") decision = event.decision;
      else if (event.event === "error") throw new Error(event.detail);
    }
  }
  if (!decision) throw new Error("The analysis stream ended without a decision.");
  return decision;
}

async function analyze() {
  if (state.busy) return;
  const { claim, error } = parseClaim();
  if (error) {
    $("#claim-editor").open = true;
    $("#claim-json").focus();
    showError("The claim JSON is invalid. Fix it before analyzing.", error);
    return;
  }
  const revision = state.revision;
  const scenario = state.current && state.current.label;
  const btn = $("#analyze");
  state.busy = true;
  btn.disabled = true;
  btn.classList.add("loading");
  btn.querySelector(".btn-label").textContent = "Investigating…";
  resetFlow();
  hideError();
  $("#retry-replay").disabled = true;
  startLive(revision);
  try {
    const decision = await requestAnalysis(claim, (event) => { live.chain = live.chain.then(() => applyLive(event)); });
    await live.chain;  // let the diagram finish the path before the result appears
    const followed = live.active;
    stopLive();
    const previous = state.last;
    const chosen = currentProvider();
    const run = { claim, decision, replay: null, scenario, revision, provider: chosen && { id: chosen.id, model: chosen.model } };
    state.last = run;
    state.replay = null;
    render(decision, null, previous);
    markCurrentExecution(decision.execution_id);
    loadHistory();
    if (followed) window.setTimeout(() => $("#results").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" }), 450);
    setReplayNotice("Loading the recorded execution…", true);
    try {
      const replay = await api(`/executions/${encodeURIComponent(decision.execution_id)}`);
      run.replay = replay;
      state.replay = replay;
      renderRecord(decision, replay);
    } catch (e) {
      setReplayNotice(`The recommendation is available, but its replay could not be loaded (${e.message}). Retry the lookup without running another analysis.`);
    }
  } catch (e) {
    stopLive();
    if (e.status === 422 && e.body && Array.isArray(e.body.detail)) {
      showError("The API rejected the claim (422 validation error).",
        e.body.detail.map((d) => `${(d.loc || []).join(".")}: ${d.msg}`).join("\n"));
    } else if (e.status === 429) {
      showError("Demo usage limit reached.", e.message);
    } else {
      showError("Analysis failed.", e.message);
    }
  } finally {
    state.busy = false;
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.querySelector(".btn-label").textContent = "Analyze Claim";
    $("#retry-replay").disabled = state.retrying;
    if (state.last && state.last.revision === state.revision && state.replay) renderFlow(state.replay);
    else resetFlow();
    updateResultContext();
  }
}

function setReplayNotice(message, loading = false) {
  $("#replay-unavailable").hidden = false;
  $("#replay-message").textContent = message;
  $("#retry-replay").disabled = loading || state.busy;
}

async function retryReplay() {
  if (!state.last || state.busy || state.retrying) return;
  const run = state.last;
  state.retrying = true;
  setReplayNotice("Loading the recorded execution…", true);
  try {
    const replay = await api(`/executions/${encodeURIComponent(run.decision.execution_id)}`);
    if (state.last !== run) return;
    run.replay = replay;
    state.replay = replay;
    renderRecord(run.decision, replay);
    if (!state.busy && run.revision === state.revision) renderFlow(replay);
  } catch (e) {
    if (state.last === run) setReplayNotice(`Replay is still unavailable (${e.message}). The recommendation below is retained.`);
  } finally {
    state.retrying = false;
    $("#retry-replay").disabled = state.busy;
  }
}

function updateResultContext() {
  if (!state.last) return;
  const stale = state.last.revision !== state.revision;
  const el = $("#result-context");
  el.classList.toggle("stale", stale);
  el.textContent = `${stale ? "Previous execution · inputs have changed. Analyze again to update these results." : "Latest execution"} · ${state.last.decision.claim_id} · ${state.last.scenario || "Edited claim"}`;
}

function renderRecord(d, r) {
  renderEvidence(d, r);
  renderExecution(d, r);
  $("#replay-unavailable").hidden = !!r;
  $("#replay-btn").disabled = !r;
}

function showError(title, detail) {
  const el = $("#error");
  el.innerHTML = `<b>${esc(title)}</b>${detail ? `<pre>${esc(detail)}</pre>` : ""}`;
  el.hidden = false;
}
function hideError() { $("#error").hidden = true; }

// ------------------------------------------------------------------ render

function render(d, r, previous) {
  $("#empty-state").hidden = true;
  $("#results").hidden = false;
  renderCompare(d, previous);
  renderDecision(d);
  renderTools(d);
  renderRecord(d, r);
  updateResultContext();
  closeReplay();
  // Keep the workflow in view; results remain immediately below the claim controls.
}

function renderCompare(d, prev) {
  const el = $("#compare");
  if (!prev || prev.decision.claim_id !== d.claim_id) { el.hidden = true; return; }
  const now = state.last.claim;
  const changed = Object.keys({ ...prev.claim, ...now })
    .filter((k) => JSON.stringify(prev.claim[k]) !== JSON.stringify(now[k]))
    .map((k) => `<code>${esc(k)}</code>: ${esc(JSON.stringify(prev.claim[k] ?? null))} → ${esc(JSON.stringify(now[k] ?? null))}`);
  const tag = (rec) => `<span class="tag ${rec === "APPROVE" ? "PASS" : rec === "DENY" ? "FAIL" : "INDETERMINATE"}">${esc(rec)}</span>`;
  const before = prev.provider && prev.provider.model, after = state.last.provider && state.last.provider.model;
  const modelChanged = before && after && before !== after;
  if (modelChanged) changed.push(`<code>model</code>: ${esc(before)} → ${esc(after)}`);
  const secs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`);
  const metrics = modelChanged
    ? `<span class="compare-metrics">latency ${secs(prev.decision.latency_ms)} → ${secs(d.latency_ms)} · cost $${prev.decision.estimated_cost.toFixed(5)} → $${d.estimated_cost.toFixed(5)}</span>`
    : "";
  el.innerHTML = `
    <span class="compare-k">${modelChanged && changed.length === 1 ? "Same claim, different model" : "Same claim, re-analyzed"}</span>
    <span class="compare-flow">${tag(prev.decision.recommendation)}<span class="compare-arrow">→</span>${tag(d.recommendation)}</span>
    <span class="compare-change">${changed.length ? `Changed: ${changed.join(" · ")}` : "No input changes"}</span>${metrics}`;
  el.hidden = false;
}

function renderDecision(d) {
  const v = VERDICT[d.recommendation];
  const card = $("#decision");
  card.className = `card decision ${v.cls}`;
  $("#verdict").innerHTML = `${esc(v.label)} <span class="pill">${esc(v.pill)}</span>`;
  $("#verdict-sub").textContent = v.sub;

  const conf = Math.round(d.confidence * 100);
  $("#confidence").innerHTML = `${conf}%<span class="conf-bar ${d.confidence < 0.8 ? "low" : ""}"><i style="width:${conf}%"></i></span>`;
  $("#risk").innerHTML = `<span class="tag ${esc(d.risk_level)}">${esc(d.risk_level)}</span>`;
  // A PASS can follow a failed check when an exemption rescued it (e.g. PA missing, emergency exemption applies).
  const rescued = d.deterministic_outcome === "PASS" && d.tool_results.some((t) => t.outcome === "FAIL");
  $("#rules").innerHTML = `<span class="tag ${esc(d.deterministic_outcome)}">${esc(d.deterministic_outcome)}${rescued ? " · exemption applied" : ""}</span>`;
  $("#review").innerHTML = `<span class="tag ${d.human_review_required ? "yes" : "no"}">${d.human_review_required ? "Yes" : "No"}</span>`;

  // The story in one glance: every check that did not simply pass, plus any exemption that was evaluated.
  const notable = d.tool_results.filter((t) => t.outcome !== "PASS" || t.tool === "check_emergency_exemption");
  $("#findings").hidden = !notable.length;
  $("#finding-list").innerHTML = notable.map((t) => `
    <li><span class="tool-ic ${esc(t.outcome)}">${TOOL_ICON[t.outcome] || "·"}</span>
      <span><b>${esc(TOOL_NAMES[t.tool] || t.tool)}</b> <span class="d">— ${esc(t.detail)}</span></span></li>`).join("");

  const missing = $("#missing");
  missing.hidden = !d.missing_information.length;
  missing.innerHTML = `<b>Missing information</b> ${d.missing_information.map((m) => `<code>${esc(m)}</code>`).join(" ")}
    <span class="muted">— supply it and re-analyze.</span>`;

  $("#reasons").hidden = !d.human_review_reasons.length;
  $("#reason-list").innerHTML = d.human_review_reasons.map((r) => `<li>${esc(r)}</li>`).join("");
  $("#summary").textContent = d.reasoning_summary;
}

function renderEvidence(d, r) {
  const cited = new Set(d.policy_evidence.map((e) => e.policy_id));
  const supplemental = new Set(r?.supplemental_policy_ids || []);
  $("#evidence-hint").textContent = `${r ? r.retrieved_policy_ids.length + " retrieved · " : ""}${d.policy_evidence.length} cited`;
  $("#retrieved").innerHTML = (r?.retrieved_policy_ids || []).map((id) => `
    <span class="pol ${cited.has(id) ? "cited" : ""}" title="${supplemental.has(id) ? "Added by the second, findings-driven retrieval pass" : "Retrieved from the claim"}">
      ${esc(id)}@${esc(r.retrieved_policy_versions[id])}${supplemental.has(id) ? '<span class="pass2">pass 2</span>' : ""}
    </span>`).join("");
  $("#evidence").innerHTML = d.policy_evidence.length
    ? d.policy_evidence.map((e) => `
        <div class="quote">
          <div class="quote-src"><span class="quote-id">${esc(e.policy_id)} @ v${esc(e.version)}</span>
            <span class="verified" title="Excerpt matched verbatim against the retrieved policy text">✓ verified verbatim</span></div>
          <blockquote>${esc(e.excerpt)}</blockquote>
        </div>`).join("")
    : `<p class="muted">No verified citations. Unverified model citations are never shown as evidence${d.human_review_required ? ", which is one reason this case was escalated" : ""}.</p>`;
}

function renderTools(d) {
  $("#tools").innerHTML = d.tool_results.length
    ? d.tool_results.map((t) => `
        <li class="tool">
          <span class="tool-ic ${esc(t.outcome)}" aria-label="${esc(t.outcome)}">${TOOL_ICON[t.outcome] || "·"}</span>
          <div>
            <div class="tool-name">${esc(TOOL_NAMES[t.tool] || t.tool)}</div>
            <div class="tool-detail">${esc(t.detail)}</div>
            ${t.policy_refs.length ? `<div class="tool-refs">${t.policy_refs.map((p) => `<span>${esc(p)}</span>`).join("")}</div>` : ""}
          </div>
          <span class="tag ${esc(t.outcome)}">${esc(t.outcome)}</span>
        </li>`).join("")
    : `<li class="muted">No tools ran: the claim could not be identified, so it went straight to a human.</li>`;
}

function renderExecution(d, r) {
  const rows = r ? [
    ["Execution ID", r.execution_id],
    ["Workflow", r.workflow_version],
    ["Prompt", r.prompt_version],
    ["Model", `${r.model} (${r.llm_provider})`],
    ["Latency", `${r.latency_ms.toFixed(1)} ms`],
    ["Input tokens", r.input_tokens.toLocaleString("en-US")],
    ["Output tokens", r.output_tokens.toLocaleString("en-US")],
    ["Est. cost", `$${r.estimated_cost.toFixed(6)}`],
  ] : [["Execution ID", d.execution_id], ["Latency", `${d.latency_ms.toFixed(1)} ms`], ["Est. cost", `$${d.estimated_cost.toFixed(6)}`]];
  $("#meta").innerHTML = rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");
}

// ------------------------------------------------------------------ interactive workflow

const FLOW_STEPS = {
  validate: { title: "Validate claim", category: "01 · Input", purpose: "Establish what is known before investigating the exception.", input: "The submitted claim and its identifying fields.", output: "A validated claim, or direct escalation when identifiers are missing.", prefixes: ["validate_claim:"] },
  retrieve: { title: "Retrieve policies", category: "02 · Versioned policy", purpose: "Find policies that apply to this claim and its service date.", input: "Procedure, plan, region and date of service.", output: "Relevant policy text with policy IDs and versions.", prefixes: ["retrieve_policy:"] },
  tools: { title: "Check the facts", category: "03 · Deterministic rules", purpose: "Establish authoritative facts in code, independently of the model.", input: "Claim, enrollment, authorizations, claim history and policy rules.", output: "Tool findings and a PASS, FAIL or INDETERMINATE rule verdict.", prefixes: ["check_eligibility:", "check_authorization:"] },
  refine: { title: "Refine retrieval", category: "04 · Findings-driven policy", purpose: "Look for policy context that only becomes relevant after the checks run.", input: "Non-passing checks and deterministic risk signals.", output: "Additional relevant policies; skipped when there are no findings.", prefixes: ["refine_retrieval:"] },
  llm: { title: "Interpret with AI", category: "05 · One structured model call", purpose: "Read policy text against the facts and explain the exception in context.", input: "Retrieved policy passages, claim and deterministic tool results.", output: "A structured recommendation, citations and a short operational summary. The model cannot override the rules.", prefixes: ["analyze_claim:"] },
  risk: { title: "Evaluate risk", category: "06 · Guardrails", purpose: "Check grounding, missing information, risk and agreement with the rules.", input: "Rule verdict, model output and verified policy evidence.", output: "Review reasons that block an autonomous outcome when any control fails.", prefixes: ["evaluate_risk:"] },
  decide: { title: "Recommend", category: "07A · Recommendation branch", purpose: "Generate an approval or denial from the deterministic rules when all controls agree.", input: "Conclusive rules, model agreement and no review triggers.", output: "APPROVE or DENY, supported by evidence. This is a synthetic demonstration.", prefixes: ["generate_recommendation:"] },
  review: { title: "Human review", category: "07B · Escalation branch", purpose: "Make uncertainty explicit so an analyst knows what needs resolving.", input: "Missing identifiers, missing evidence, conflicting policies or other review triggers.", output: "HUMAN_REVIEW with reasons and missing facts. An analyst can supply facts and re-analyze; no human decision is recorded by this demo.", prefixes: ["escalate_to_human:"] },
  audit: { title: "Record & replay", category: "08 · Audit", purpose: "Keep the recommendation, evidence and execution context together.", input: "Either outcome, its policy evidence and the path through the workflow.", output: "A stored execution with model, prompt and workflow versions, accessible by execution ID.", prefixes: [] },
};
const flow = { record: null, selected: "validate", path: [], index: -1, timer: null, paused: false };
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function stepTrail(key, record = flow.record) {
  return (record?.routing_trail || []).filter((line) => FLOW_STEPS[key].prefixes.some((p) => line.startsWith(p)));
}
function stepStatus(key) {
  if (!flow.record) return "Explore step";
  // record_audit does not append a trail entry; a fetched record confirms persistence.
  if (key === "audit") return "✓ Recorded";
  const lines = stepTrail(key);
  if (!lines.length) return "— Not taken";
  if (key === "refine" && lines.some((s) => s.startsWith("refine_retrieval: skipped"))) return "— Skipped";
  if (key === "llm" && flow.record.llm_error) return "! Attempted · error";
  return "✓ Executed";
}
function isVisited(key) { return /^(✓|!)/.test(stepStatus(key)); }

function selectFlowStep(key) {
  flow.selected = key;
  document.querySelectorAll("#flow .iso-card").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.closest("[data-step]").dataset.step === key));
  });
  const step = FLOW_STEPS[key];
  let evidence = [];
  const r = flow.record;
  if (r) {
    evidence = stepTrail(key);
    if (isVisited(key)) {
      if (key === "retrieve") evidence.push(...r.retrieved_policy_ids.filter((id) => !r.supplemental_policy_ids.includes(id)).map((id) => `${id} @ ${r.retrieved_policy_versions[id]}`));
      if (key === "tools") evidence.push(...r.tool_summary);
      if (key === "refine") evidence.push(`Supplemental policies: ${r.supplemental_policy_ids.join(", ") || "none added"}`);
      if (key === "llm") evidence.push(r.llm_error || r.reasoning_summary);
      if (key === "risk") evidence.push(...r.human_review_reason);
      if (key === "review") evidence.push(...r.human_review_reason, ...r.missing_information.map((v) => `Missing: ${v}`));
      if (key === "audit") evidence.push(`Stored execution: ${r.execution_id}`, `${r.workflow_version} · ${r.prompt_version} · ${r.model}`, "Persistence confirmed by the execution lookup; audit has no separate routing-trail entry.");
    }
  }
  $("#flow-detail").innerHTML = `<div><p class="eyebrow">${esc(step.category)}</p><h3>${esc(step.title)}</h3><p>${esc(step.purpose)}</p></div>
    <dl><dt>Input</dt><dd>${esc(step.input)}</dd><dt>Output</dt><dd>${esc(step.output)}</dd></dl>
    ${r ? `<div class="step-evidence"><p><b>${esc(stepStatus(key))}</b> · Recorded execution</p>${evidence.length ? `<ul>${evidence.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : "<p>This branch was not taken in this execution.</p>"}</div>` : ""}`;
}

function clearFlowTimer() {
  window.clearTimeout(flow.timer);
  flow.timer = null;
}
function resetFlow() {
  clearFlowTimer();
  stopLive();
  Object.assign(flow, { record: null, path: [], index: -1, paused: false });
  $("#flow-status").textContent = state.busy ? "Investigating…" : "Conceptual workflow · select a step to explore";
  $("#flow-play").textContent = "Replay execution";
  $("#flow-play").disabled = true;
  $("#flow-restart").disabled = true;
  paintFlow();
}
function renderFlow(r) {
  clearFlowTimer();
  flow.record = r;
  flow.index = -1;
  flow.paused = false;
  flow.path = [];
  for (const line of r.routing_trail) {
    const key = Object.keys(FLOW_STEPS).find((k) => FLOW_STEPS[k].prefixes.some((p) => line.startsWith(p)));
    if (key && isVisited(key) && !flow.path.includes(key)) flow.path.push(key);
  }
  flow.path.push("audit");
  $("#flow-status").textContent = `Recorded execution · ${r.claim_id} · ${r.recommendation}`;
  $("#flow-play").disabled = false;
  $("#flow-play").textContent = "Replay execution";
  $("#flow-restart").disabled = false;
  paintFlow();
}
function paintFlow() {
  document.querySelectorAll("#flow [data-step]").forEach((node) => {
    const key = node.dataset.step;
    node.classList.toggle("visited", !!flow.record && isVisited(key));
    node.classList.toggle("skipped", !!flow.record && !isVisited(key));
    node.classList.toggle("replay-current", flow.path[flow.index] === key);
    node.querySelector(".node-state").textContent = stepStatus(key);
  });
  selectFlowStep(flow.selected);
  drawFlowEdges();
}
function replayTick() {
  if (!flow.record) return;
  flow.index += 1;
  if (flow.index >= flow.path.length) {
    flow.index = -1;
    flow.timer = null;
    $("#flow-play").textContent = "Replay execution";
    $("#flow-status").textContent = `Replay complete · recorded execution · ${flow.record.claim_id}`;
    paintFlow();
    return;
  }
  flow.selected = flow.path[flow.index];
  $("#flow-status").textContent = `Replay · ${flow.index + 1}/${flow.path.length} · ${FLOW_STEPS[flow.selected].title} · recorded, not live`;
  paintFlow();
  flow.timer = window.setTimeout(replayTick, 1100);
}
function playFlow() {
  if (!flow.record) return;
  if (flow.timer) {
    clearFlowTimer();
    flow.paused = true;
    $("#flow-play").textContent = "Resume replay";
    $("#flow-status").textContent = `Replay paused · ${FLOW_STEPS[flow.path[flow.index]].title} · recorded, not live`;
    return;
  }
  // Reduced-motion mode advances only on request; there is no automatic animation.
  if (reducedMotion()) {
    flow.index = (flow.index + 1) % flow.path.length;
    flow.selected = flow.path[flow.index];
    $("#flow-play").textContent = "Next replay step";
    $("#flow-status").textContent = `Replay · step ${flow.index + 1}/${flow.path.length} · manual, reduced motion`;
    paintFlow();
    return;
  }
  flow.paused = false;
  $("#flow-play").textContent = "Pause replay";
  replayTick();
}
function restartFlow() {
  if (!flow.record) return;
  clearFlowTimer();
  flow.index = -1;
  flow.paused = false;
  playFlow();
}

// Orthogonal connectors for the layout: pipeline row → lane → guardrails → two outcomes → audit.
function drawFlowEdges() {
  const board = $("#flow-board");
  if (window.matchMedia("(max-width: 760px)").matches) return;
  const b = board.getBoundingClientRect();
  const box = (key) => {
    const r = $(`#flow [data-step="${key}"] .iso-card`).getBoundingClientRect();
    return { l: r.left - b.left, r: r.right - b.left, t: r.top - b.top, bo: r.bottom - b.top,
             cx: (r.left + r.right) / 2 - b.left, cy: (r.top + r.bottom) / 2 - b.top };
  };
  // "Passed through": refine runs even when it has nothing to add, so the path continues through it.
  const passed = (key) => live.active ? (live.done.has(key) || live.running === key)
    : !!flow.record && (isVisited(key) || stepStatus(key) === "— Skipped");
  const off = [], on_ = [];  // taken path is drawn last so it stays on top where branches share a segment
  const edge = (points, on, { early = false, label = "", at = null } = {}) => {
    const out = on ? on_ : off;
    const d = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    out.push(`<path class="flow-edge${on ? " visited" : ""}${early ? " early" : ""}" d="${d}" marker-end="url(#arrow-${on ? "on" : "off"})"/>`);
    if (label) out.push(`<text class="edge-label${on ? " visited" : ""}" x="${at[0]}" y="${at[1]}" text-anchor="middle">${esc(label)}</text>`);
  };
  const gap = 3;  // keep arrow tips just off the card border

  // 1. Pipeline row.
  for (const [from, to] of [["validate", "retrieve"], ["retrieve", "tools"], ["tools", "refine"], ["refine", "llm"]]) {
    const a = box(from), c = box(to);
    edge([[a.r + gap, a.cy], [c.l - gap, c.cy]], passed(from) && passed(to));
  }
  // 2. Wrap from the model down the connector lane to the guardrails.
  const llm = box("llm"), risk = box("risk");
  const lane = llm.bo + ($("#flow [data-step='risk']").getBoundingClientRect().top - b.top - llm.bo) / 2 - 20;
  edge([[llm.cx, llm.bo + gap], [llm.cx, lane], [risk.cx, lane], [risk.cx, risk.t - gap]], passed("llm") && passed("risk"));

  // 3. Fork to the two outcomes, then merge into the audit record.
  const decide = box("decide"), review = box("review"), audit = box("audit");
  const forkX = (risk.r + decide.l) / 2, mergeX = (decide.r + audit.l) / 2;
  edge([[risk.r + gap, risk.cy], [forkX, risk.cy], [forkX, decide.cy], [decide.l - gap, decide.cy]], passed("risk") && passed("decide"),
       { label: "Controls agree", at: [(forkX + decide.l) / 2, decide.cy - 8] });
  edge([[risk.r + gap, risk.cy], [forkX, risk.cy], [forkX, review.cy], [review.l - gap, review.cy]], passed("risk") && passed("review"),
       { label: "Review required", at: [(forkX + review.l) / 2, review.cy - 8] });
  edge([[decide.r + gap, decide.cy], [mergeX, decide.cy], [mergeX, audit.cy], [audit.l - gap, audit.cy]], passed("decide"));
  edge([[review.r + gap, review.cy], [mergeX, review.cy], [mergeX, audit.cy], [audit.l - gap, audit.cy]], passed("review"));

  // 4. Early exit: unidentifiable claims skip the investigation entirely (dashed, below the diagram).
  const validate = box("validate"), low = review.bo + 22;
  const early = !!flow.record && passed("review") && !passed("retrieve");
  edge([[validate.l - gap, validate.cy], [validate.l - 16, validate.cy], [validate.l - 16, low], [review.cx, low], [review.cx, review.bo + gap]],
       early, { early: true, label: "Missing identifiers → direct escalation", at: [(validate.l + review.cx) / 2, low - 7] });

  const marker = (id, color) => `<marker id="arrow-${id}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M1 1 L9 5 L1 9z" fill="${color}"/></marker>`;
  $("#flow-edges").innerHTML = `<defs>${marker("on", "#4338ca")}${marker("off", "#c3cad8")}</defs>${off.join("")}${on_.join("")}`;
}

// ------------------------------------------------------------------ replay

function toggleReplay() {
  if (!state.replay) return;
  const open = $("#replay").hidden;
  if (open) renderReplay(state.replay);
  $("#replay").hidden = !open;
  $("#replay-btn").setAttribute("aria-expanded", String(open));
  $("#replay-btn").textContent = open ? "Hide Decision Replay" : "View Full Decision Replay";
}
function closeReplay() {
  $("#replay").hidden = true;
  $("#replay-btn").setAttribute("aria-expanded", "false");
  $("#replay-btn").textContent = "View Full Decision Replay";
}

function renderReplay(r) {
  const kv = (pairs) => `<dl class="rp-kv">${pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
  const list = (xs) => (xs && xs.length ? xs.map(esc).join("<br>") : '<span class="muted">—</span>');
  const trail = r.routing_trail.map((s) => {
    const i = s.indexOf(":");
    return `<li><b>${esc(s.slice(0, i))}</b>${esc(s.slice(i))}</li>`;
  }).join("");
  $("#replay-formatted").innerHTML = `
    <div class="rp-group"><h4>Identity & versions</h4>${kv([
      ["execution_id", esc(r.execution_id)], ["claim_id", esc(r.claim_id)], ["timestamp", esc(r.timestamp)],
      ["workflow_version", esc(r.workflow_version)], ["prompt_version", esc(r.prompt_version)],
      ["model", `${esc(r.model)} · ${esc(r.llm_provider)}`],
    ])}</div>
    <div class="rp-group"><h4>Outcome</h4>${kv([
      ["recommendation", esc(r.recommendation)], ["deterministic_outcome", esc(r.deterministic_outcome)],
      ["llm_recommendation", esc(r.llm_recommendation ?? "—")], ["confidence", esc(r.confidence)],
      ["risk_level", esc(r.risk_level)], ["risk_factors", list(r.risk_factors)],
      ["human_review_reason", list(r.human_review_reason)], ["missing_information", list(r.missing_information)],
      ["llm_error", esc(r.llm_error ?? "—")],
    ])}</div>
    <div class="rp-group"><h4>Retrieval & grounding</h4>${kv([
      ["retrieved_policies", list(r.retrieved_policy_ids.map((id) => `${id}@${r.retrieved_policy_versions[id]}`))],
      ["supplemental (pass 2)", list(r.supplemental_policy_ids)],
      ["retrieval_queries", list(r.retrieval_queries)],
      ["evidence_grounded", esc(r.evidence_grounded)], ["grounding_failures", list(r.grounding_failures)],
    ])}</div>
    <div class="rp-group"><h4>Deterministic tools</h4>${kv([["tool_summary", list(r.tool_summary)]])}</div>
    <div class="rp-group"><h4>Routing trail</h4><ol class="trail">${trail}</ol></div>
    <div class="rp-group"><h4>Cost & latency</h4>${kv([
      ["latency_ms", esc(r.latency_ms)], ["llm_latency_ms", esc(r.llm_latency_ms)],
      ["input_tokens", esc(r.input_tokens)], ["output_tokens", esc(r.output_tokens)], ["estimated_cost", esc(r.estimated_cost)],
    ])}</div>`;
  $("#replay-raw").textContent = JSON.stringify(r, null, 2);
}

function switchReplayView(view) {
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.view === view;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", String(on));
  });
  $("#replay-formatted").hidden = view !== "formatted";
  $("#replay-raw").hidden = view !== "raw";
}

// ------------------------------------------------------------------ execution history (this browser only)

const history_ = { rows: [], currentId: null };
const recTag = (rec) => `<span class="tag ${rec === "APPROVE" ? "PASS" : rec === "DENY" ? "FAIL" : "INDETERMINATE"}">${esc(rec)}</span>`;

async function loadHistory() {
  const filter = $("#history-filter").value;
  let rows;
  try {
    rows = await api(`/executions?limit=50${filter ? `&claim_id=${encodeURIComponent(filter)}` : ""}`);
  } catch (e) {
    $("#history-list").innerHTML = `<p class="history-empty">History is unavailable (${esc(e.message)}).</p>`;
    return;
  }
  if (!filter) {
    history_.rows = rows;
    const claims = [...new Set(rows.map((r) => r.claim_id))];
    $("#history-filter").innerHTML = `<option value="">All claims</option>` + claims.map((c) => `<option>${esc(c)}</option>`).join("");
  }
  if (!rows.length) {
    $("#history-list").innerHTML = `<p class="history-empty">No analyses from this browser yet. Run one above; every execution is recorded and replayable.</p>`;
    return;
  }
  $("#history-list").innerHTML = `<div class="history-wrap"><table class="history-table">
    <thead><tr><th>When</th><th>Claim</th><th>Recommendation</th><th>Risk</th><th>Rules</th><th>Model</th><th>Latency</th><th>Cost</th><th></th></tr></thead>
    <tbody>${rows.map((r) => `<tr class="${r.execution_id === history_.currentId ? "current" : ""}">
      <td class="num" title="${esc(r.timestamp)}">${esc(new Date(r.timestamp).toLocaleString())}</td>
      <td><code>${esc(r.claim_id)}</code></td>
      <td>${recTag(r.recommendation)}</td>
      <td><span class="tag ${esc(r.risk_level)}">${esc(r.risk_level)}</span></td>
      <td><span class="tag ${esc(r.deterministic_outcome)}">${esc(r.deterministic_outcome)}</span></td>
      <td class="num">${esc(r.model)}</td>
      <td class="num">${r.latency_ms >= 1000 ? (r.latency_ms / 1000).toFixed(1) + " s" : Math.round(r.latency_ms) + " ms"}</td>
      <td class="num">${r.llm_provider === "mock" ? "—" : "$" + Number(r.estimated_cost).toFixed(5)}</td>
      <td><div class="row-actions">
        <button type="button" class="link-btn" data-open="${esc(r.execution_id)}">Open</button>
        <button type="button" class="link-btn" data-copy="${esc(r.execution_id)}">Copy link</button>
      </div></td></tr>`).join("")}</tbody></table></div>`;
}

function executionLink(id) { return `${location.origin}/?execution=${encodeURIComponent(id)}`; }
function markCurrentExecution(id) {
  history_.currentId = id;
  try { window.history.replaceState(null, "", `?execution=${encodeURIComponent(id)}`); } catch { /* file:// or sandbox */ }
}

// Rebuild the workbench from a stored execution record (the replay carries everything the decision view needs).
function decisionFromRecord(r) {
  return {
    execution_id: r.execution_id, claim_id: r.claim_id, recommendation: r.recommendation, confidence: r.confidence,
    risk_level: r.risk_level, reasoning_summary: r.reasoning_summary, policy_evidence: r.policy_evidence,
    tool_results: r.tool_results, deterministic_outcome: r.deterministic_outcome,
    human_review_required: r.human_review_required, human_review_reasons: r.human_review_reason,
    missing_information: r.missing_information, estimated_tokens: r.input_tokens + r.output_tokens,
    estimated_cost: r.estimated_cost, latency_ms: r.latency_ms,
  };
}

async function openExecution(id, { scroll = true } = {}) {
  if (state.busy) return;
  let r;
  try {
    r = await api(`/executions/${encodeURIComponent(id)}`);
  } catch (e) {
    showError(`Execution ${id} could not be opened.`, e.message);
    return;
  }
  hideError();
  // Stored claims include explicit nulls that scenario files omit; treat missing as null when matching.
  const sameClaim = (a, b) => Object.keys({ ...a, ...b }).every((k) => (a[k] ?? null) === (b[k] ?? null));
  const match = state.scenarios.find((s) => sameClaim(s.claim, r.claim));
  if (match) selectScenario(match.id, false);
  else {
    state.current = { id: null, label: `Recorded execution ${r.execution_id}`, claim: r.claim, watch: "A recorded execution from your history." };
    document.querySelectorAll(".scenario").forEach((el) => el.setAttribute("aria-checked", "false"));
    $("#watch-text").textContent = state.current.watch;
    setClaimText(r.claim);
  }
  const decision = decisionFromRecord(r);
  state.last = { claim: r.claim, decision, replay: r, scenario: `Recorded · ${r.execution_id}`, revision: state.revision,
                 provider: { id: r.llm_provider, model: r.model } };
  state.replay = r;
  render(decision, r, null);
  renderFlow(r);
  updateResultContext();
  markCurrentExecution(r.execution_id);
  loadHistory();
  if (scroll) $("#results").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
}

async function copyExecutionLink(id, button) {
  try {
    await navigator.clipboard.writeText(executionLink(id));
    button.textContent = "Copied ✓";
  } catch {
    window.prompt("Copy this link:", executionLink(id));
  }
  setTimeout(() => { button.textContent = "Copy link"; }, 1600);
}

// ------------------------------------------------------------------ evaluation snapshot

async function loadEvaluation() {
  let s;
  try {
    s = await api("/evals/latest");
  } catch (e) {
    $("#eval").innerHTML = `<div class="eval-empty">No evaluation results are available on this server yet.
      Run <code>python -m evals.run</code> to generate them; nothing is shown until real results exist.</div>`;
    return;
  }
  // Headline: a real-model run if one is committed; otherwise the local run. The table compares all runs.
  const runs = [...(s.reference_runs || []), s];
  const head = runs.find((r) => r.provider !== "mock") || s;
  const unsafe = head.unsafe_autonomous_actions;
  const metrics = [
    [head.cases, "Cases evaluated"],
    [pct(head.policy_retrieval_rate), "Correct policy retrieval"],
    [pct(head.grounded_response_rate), "Grounded responses"],
    [pct(head.correct_escalation_rate), "Correct escalation"],
    [pct(head.recommendation_accuracy), "Recommendation accuracy"],
  ];
  const row = (r) => `<tr>
      <td><code>${esc(r.model)}</code>${r.provider === "mock" ? ' <span class="muted">(mock · CI gate)</span>' : ' <span class="muted">(real model)</span>'}</td>
      <td>${esc(r.cases)}</td><td>${pct(r.recommendation_accuracy)}</td><td>${pct(r.policy_retrieval_rate)}</td>
      <td>${pct(r.grounded_response_rate)}</td><td>${pct(r.correct_escalation_rate)}</td>
      <td class="${r.unsafe_autonomous_actions ? "bad" : "ok"}">${esc(r.unsafe_autonomous_actions)}</td>
      <td>${esc(r.invalid_outputs)}</td><td>${esc(Math.round(r.p50_latency_ms))} / ${esc(Math.round(r.p95_latency_ms))} ms</td>
      <td>${r.provider === "mock" ? '<span class="muted">—</span>' : `$${Number(r.estimated_average_cost).toFixed(5)}`}</td></tr>`;
  $("#eval-sub").innerHTML = `Metrics computed from the audit records of real runs of <code>python -m evals.run</code>, not hardcoded.
    Headline: <code>${esc(head.model)}</code>.`;
  $("#eval").innerHTML = metrics.map(([v, k]) =>
    `<div class="metric"><div class="metric-v">${esc(v)}</div><div class="metric-k">${esc(k)}</div></div>`).join("")
    + `<div class="metric gate ${unsafe ? "bad" : ""}"><div class="metric-v">${esc(unsafe)}</div><div class="metric-k">Unsafe autonomous actions</div></div>`
    + `<div class="eval-table-wrap"><table class="eval-table">
        <thead><tr><th>Run</th><th>Cases</th><th>Accuracy</th><th>Retrieval</th><th>Grounded</th><th>Escalation</th>
          <th>Unsafe</th><th>Invalid</th><th>p50 / p95</th><th>Cost/claim</th></tr></thead>
        <tbody>${runs.map(row).join("")}</tbody></table></div>`
    + `<p class="eval-note">The mock model validates the controls (routing, grounding, escalation) and gates CI; the real-model
       run measures interpretation quality, latency and cost. Same cases, same workflow.</p>`;
}

async function loadVersions() {
  try {
    const h = await api("/health");
    $("#versions").textContent = `${h.workflow_version} · ${h.prompt_version} · ${h.model}`;
    renderProviders(h.available_providers || []);
  } catch { /* footer only */ }
}

// ------------------------------------------------------------------ wiring

document.addEventListener("DOMContentLoaded", () => {
  $("#analyze").addEventListener("click", analyze);
  $("#retry-replay").addEventListener("click", retryReplay);
  $("#flow-play").addEventListener("click", playFlow);
  $("#flow-restart").addEventListener("click", restartFlow);
  document.querySelectorAll("#flow [data-step]").forEach((node) => node.querySelector("button").addEventListener("click", () => {
    if (flow.timer) playFlow();
    selectFlowStep(node.dataset.step);
  }));
  new ResizeObserver(drawFlowEdges).observe($("#flow-board"));
  window.matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", () => {
    if (flow.timer) playFlow();
  });
  resetFlow();
  $("#claim-json").addEventListener("input", onClaimEdited);
  $("#reset-claim").addEventListener("click", () => state.current && setClaimText(state.current.claim));
  $("#replay-btn").addEventListener("click", toggleReplay);
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchReplayView(t.dataset.view)));
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); analyze(); }
  });
  $("#model-options").addEventListener("click", (e) => {
    const b = e.target.closest("[data-provider]");
    if (b && !state.busy) selectProvider(b.dataset.provider);
  });
  $("#history-refresh").addEventListener("click", loadHistory);
  $("#history-filter").addEventListener("change", loadHistory);
  $("#history-list").addEventListener("click", (e) => {
    const open = e.target.closest("[data-open]"), copy = e.target.closest("[data-copy]");
    if (open) openExecution(open.dataset.open);
    if (copy) copyExecutionLink(copy.dataset.copy, copy);
  });
  // A shared link (/?execution=EXE-…) opens that decision once the scenarios have loaded.
  const shared = new URLSearchParams(location.search).get("execution");
  loadScenarios().then(() => (shared ? openExecution(shared, { scroll: true }) : null));
  loadHistory();
  loadEvaluation();
  loadVersions();
});
