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
  APPROVE: { cls: "approve", label: "Approve", pill: "Autonomous",
    sub: "All deterministic checks passed, the model agreed, and every citation was verified against policy text." },
  DENY: { cls: "deny", label: "Deny", pill: "Autonomous",
    sub: "A deterministic rule failed and no exemption applied. The model concurred; the decision comes from the rules." },
  HUMAN_REVIEW: { cls: "review", label: "Human review", pill: "Escalated",
    sub: "ClaimPilot did not decide on its own. The case goes to an analyst with the reasons and evidence below." },
};

const state = { scenarios: [], current: null, last: null, replay: null };

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
  const { claim, error } = parseClaim();
  const status = $("#json-status");
  $("#claim-json").classList.toggle("invalid", !!error);
  status.classList.toggle("error", !!error);
  status.textContent = error ? `Invalid JSON: ${error}` : "Valid JSON · edit any field and re-analyze";
  if (claim) renderClaimSummary(claim);
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

async function analyze() {
  const { claim, error } = parseClaim();
  if (error) {
    showError("The claim JSON is invalid. Fix it before analyzing.", error);
    return;
  }
  const btn = $("#analyze");
  btn.disabled = true;
  btn.classList.add("loading");
  btn.querySelector(".btn-label").textContent = "Investigating…";
  hideError();
  try {
    const decision = await api("/claims/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(claim),
    });
    const replay = await api(`/executions/${encodeURIComponent(decision.execution_id)}`);
    const previous = state.last;
    state.last = { claim, decision, replay, scenario: state.current && state.current.label };
    state.replay = replay;
    render(decision, replay, previous);
  } catch (e) {
    if (e.status === 422 && e.body && Array.isArray(e.body.detail)) {
      showError("The API rejected the claim (422 validation error).",
        e.body.detail.map((d) => `${(d.loc || []).join(".")}: ${d.msg}`).join("\n"));
    } else if (e.status === 429) {
      showError("Demo usage limit reached.", e.message);
    } else {
      showError("Analysis failed.", e.message);
    }
  } finally {
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.querySelector(".btn-label").textContent = "Analyze Claim";
  }
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
  renderEvidence(d, r);
  renderTools(d);
  renderExecution(d, r);
  renderFlow(r);
  closeReplay();
  $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderCompare(d, prev) {
  const el = $("#compare");
  if (!prev || prev.decision.claim_id !== d.claim_id) { el.hidden = true; return; }
  const now = state.last.claim;
  const changed = Object.keys({ ...prev.claim, ...now })
    .filter((k) => JSON.stringify(prev.claim[k]) !== JSON.stringify(now[k]))
    .map((k) => `<code>${esc(k)}</code>: ${esc(JSON.stringify(prev.claim[k] ?? null))} → ${esc(JSON.stringify(now[k] ?? null))}`);
  const tag = (rec) => `<span class="tag ${rec === "APPROVE" ? "PASS" : rec === "DENY" ? "FAIL" : "INDETERMINATE"}">${esc(rec)}</span>`;
  el.innerHTML = `
    <span class="compare-k">Same claim, re-analyzed</span>
    <span class="compare-flow">${tag(prev.decision.recommendation)}<span class="compare-arrow">→</span>${tag(d.recommendation)}</span>
    <span class="compare-change">${changed.length ? `Changed: ${changed.join(" · ")}` : "No input changes"}</span>`;
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
  $("#rules").innerHTML = `<span class="tag ${esc(d.deterministic_outcome)}">${esc(d.deterministic_outcome)}</span>`;
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
  const supplemental = new Set(r.supplemental_policy_ids || []);
  $("#evidence-hint").textContent = `${r.retrieved_policy_ids.length} retrieved · ${d.policy_evidence.length} cited`;
  $("#retrieved").innerHTML = r.retrieved_policy_ids.map((id) => `
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
  const rows = [
    ["Execution ID", r.execution_id],
    ["Workflow", r.workflow_version],
    ["Prompt", r.prompt_version],
    ["Model", `${r.model} (${r.llm_provider})`],
    ["Latency", `${r.latency_ms.toFixed(1)} ms`],
    ["Input tokens", r.input_tokens.toLocaleString("en-US")],
    ["Output tokens", r.output_tokens.toLocaleString("en-US")],
    ["Est. cost", `$${r.estimated_cost.toFixed(6)}`],
  ];
  $("#meta").innerHTML = rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");
}

// Light up the "How it works" strip from the real routing trail of this execution.
function renderFlow(r) {
  const trail = r.routing_trail;
  const has = (prefix) => trail.some((s) => s.startsWith(prefix));
  const steps = {
    validate: has("validate_claim"),
    retrieve: has("retrieve_policy"),
    tools: has("check_authorization"),
    refine: has("refine_retrieval") && !has("refine_retrieval: skipped"),
    llm: has("analyze_claim"),
    risk: has("evaluate_risk"),
    route: true,
    audit: true,
  };
  document.querySelectorAll("#flow li").forEach((li) => {
    const s = li.dataset.step;
    li.classList.remove("visited", "skipped", "end-review", "end-decide");
    if (s === "route") li.classList.add(r.human_review_required ? "end-review" : "end-decide");
    else li.classList.add(steps[s] ? "visited" : "skipped");
  });
}

// ------------------------------------------------------------------ replay

function toggleReplay() {
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
  } catch { /* footer only */ }
}

// ------------------------------------------------------------------ wiring

document.addEventListener("DOMContentLoaded", () => {
  $("#analyze").addEventListener("click", analyze);
  $("#claim-json").addEventListener("input", onClaimEdited);
  $("#reset-claim").addEventListener("click", () => state.current && setClaimText(state.current.claim));
  $("#replay-btn").addEventListener("click", toggleReplay);
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchReplayView(t.dataset.view)));
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); analyze(); }
  });
  loadScenarios();
  loadEvaluation();
  loadVersions();
});
