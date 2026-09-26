// Private usage dashboard. The token lives in sessionStorage (this tab only) and is sent as a Bearer header,
// never in the URL.
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const KEY = "claimpilot-admin-token";
const token = () => { try { return sessionStorage.getItem(KEY); } catch { return null; } };

async function call(path, method = "GET") {
  const res = await fetch(path, { method, headers: { Authorization: `Bearer ${token()}` } });
  if (res.status === 401) throw Object.assign(new Error("Invalid admin token"), { status: 401 });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

const table = (cols, rows) => rows.length
  ? `<div class="history-wrap"><table class="history-table"><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
     <tbody>${rows.map((r) => `<tr>${r.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
  : `<p class="history-empty">Nothing yet.</p>`;

async function load() {
  let s;
  try {
    s = await call(`/admin/stats?days=${$("#days").value}`);
  } catch (e) {
    if (e.status === 401) return signOut("That token was not accepted.");
    $("#kpis").innerHTML = `<p class="history-empty">Could not load stats (${esc(e.message)}).</p>`;
    return;
  }
  $("#login").hidden = true;
  $("#dashboard").hidden = false;
  const kpis = [
    [s.unique_visitors, "Unique visitors"], [s.visitors_who_ran_an_analysis, "Visitors who ran an analysis"],
    [s.page_views, "Page views"], [s.analyses, "Analyses"], [s.shared_link_opens, "Shared decision links opened"],
    [s.replay_views, "Replays opened"],
  ];
  $("#kpis").innerHTML = kpis.map(([v, k]) => `<div class="metric"><div class="metric-v">${esc(v)}</div><div class="metric-k">${esc(k)}</div></div>`).join("");
  $("#daily").innerHTML = table(["Day", "Visitors", "Page views", "Analyses"], s.daily.map((d) => [d.day, d.visitors, d.page_views, d.analyses]));
  $("#referrers").innerHTML = table(["Source", "Visitors"], s.referrers.map((r) => [r.host, r.visitors]));
  $("#models").innerHTML = table(["Model", "Analyses"], s.analyses_by_model.map((m) => [m.model, m.count]));
  $("#bots").innerHTML = table(["Agent", "Hits"], s.automated_traffic.map((b) => [b.agent, b.hits]));
  $("#recent").innerHTML = table(["When (UTC)", "Event", "Detail", "Source", "Visitor"],
    s.recent.map((e) => [e.ts.replace("T", " ").slice(0, 19), e.kind, e.detail || "", e.referrer || "", e.visitor]));
}

function signOut(message = "") {
  try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
  $("#dashboard").hidden = true;
  $("#login").hidden = false;
  $("#login-error").textContent = message;
}

document.addEventListener("DOMContentLoaded", () => {
  $("#login").addEventListener("submit", (e) => {
    e.preventDefault();
    try { sessionStorage.setItem(KEY, $("#token").value.trim()); } catch { /* ignore */ }
    $("#token").value = "";
    $("#login-error").textContent = "";
    load();
  });
  $("#days").addEventListener("change", load);
  $("#logout").addEventListener("click", () => signOut());
  $("#notrack").addEventListener("click", async () => {
    await call("/admin/notrack", "POST");
    $("#notrack-status").textContent = "This browser is no longer counted.";
  });
  $("#track-again").addEventListener("click", async () => {
    await call("/admin/notrack", "DELETE");
    $("#notrack-status").textContent = "This browser is counted again.";
  });
  if (token()) load();
});
