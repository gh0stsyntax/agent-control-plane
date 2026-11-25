"use strict";

// =====================================================================
// Basics
// =====================================================================
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtEur = (v) => (v === null || v === undefined || v === "") ? "–" : Number(v).toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " €";
const fmtTime = (iso) => iso ? new Date(iso).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
const fmtDateTime = (iso) => iso ? new Date(iso).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "medium" }) : "";
const pct = (v) => v === null || v === undefined ? "–" : (v * 100).toFixed(1).replace(".", ",") + " %";

const STATE_CLASS = {
  CLOSED: "ok", VERIFIED: "ok", APPROVED: "ok", AUTO_APPROVED: "ok", VALIDATED: "ok",
  APPROVAL_PENDING: "warn", VALIDATION_PENDING: "warn", EXECUTING: "info",
  ESCALATED: "bad", REJECTED_POLICY: "bad", FAILED_BUSINESS: "bad", FAILED_SYSTEM: "bad",
  CLOSED_DENIED: "", DENIED: "",
};
const HAPPY_PATH = ["RECEIVED", "DIGITIZED", "CLASSIFIED", "EXTRACTED", "REASONED", "DECIDED", "AUTO_APPROVED|APPROVED", "EXECUTING", "VERIFIED", "CLOSED"];
const TERMINAL = new Set(["CLOSED", "CLOSED_DENIED", "REJECTED_POLICY", "ESCALATED"]);

const statePill = (s) => `<span class="pill ${STATE_CLASS[s] ?? "info"}">${esc(s)}</span>`;
const tierPill = (t) => t ? `<span class="tier ${esc(t)}">${esc(t)}</span>` : `<span class="muted">–</span>`;
const confCell = (c, th) => c === undefined ? "" : `<span class="${c < th ? "conf-low" : "conf-ok"}">${Number(c).toFixed(2)}</span>`;

const META = { users: [], scenarios: [], tools: {}, agents: {} };
let currentUser = localGet("aeap.user") || "a.becker";

function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = msg;
  $("#toast-host").appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 7000 : 4200);
}

function jsonHtml(obj) {
  const s = JSON.stringify(obj, null, 2) ?? "";
  return esc(s).replace(/(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)/g,
    (m, str, colon, bool, num) => {
      if (str) return colon ? `<span class="j-key">${str}</span>${colon}` : `<span class="j-str">${str}</span>`;
      if (bool) return `<span class="j-bool">${bool}</span>`;
      if (num) return `<span class="j-num">${num}</span>`;
      return m;
    });
}

function userName(id) { return (META.users.find((u) => u.id === id) || {}).name || id; }

// =====================================================================
// Router
// =====================================================================
const VIEWS = {
  overview: { title: "Übersicht", render: renderOverview, refresh: true },
  intake: { title: "Eingang · Ingress & Events (L0)", render: renderIntake },
  cases: { title: "Cases · Maestro Case Lifecycle", render: renderCases, refresh: true },
  action: { title: "Action Center · Approval & Validation", render: renderAction },
  queues: { title: "Orchestrator · Queues & Robots", render: renderQueues, refresh: true },
  systems: { title: "Systems of Record (simuliert)", render: renderSystems, refresh: true },
  governance: { title: "Governance · PDP, Trust Layer, Automation Ops", render: renderGovernance },
  integration: { title: "Integration · External Agent Bridge & Tool-Verträge", render: renderIntegration },
  audit: { title: "Audit & Trace", render: renderAudit, refresh: true },
  wissen: { title: "Spickzettel · Wissen für alle", render: renderWissen },
  planer: { title: "Workflow-Planer", render: () => { location.hash = "#/wissen/planer"; } },
};
let route = { view: "overview", arg: null };
let lastSig = "";

function parseHash() {
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  return { view: VIEWS[parts[0]] ? parts[0] : "overview", arg: parts[1] ? decodeURIComponent(parts[1]) : null,
    arg2: parts[2] ? decodeURIComponent(parts[2]) : null };
}

async function navigate(force = true) {
  route = parseHash();
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === route.view &&
    (a.dataset.tab ? route.arg === a.dataset.tab : route.arg !== "planer")));
  $("#page-title").textContent = route.view === "wissen" && route.arg === "planer" ? "Workflow-Planer · Blueprint mit allen Parametern" : VIEWS[route.view].title;
  if (force) {
    lastSig = "";
    renderHelp(route.view);
  }
  try {
    await VIEWS[route.view].render(route.arg, route.arg2);
  } catch (e) {
    $("#view").innerHTML = `<div class="callout bad">Fehler: ${esc(e.message)}</div>`;
  }
}

// Rendert nur, wenn sich die Daten geaendert haben (verhindert Flackern beim Auto-Refresh)
function paint(sig, html) {
  if (sig === lastSig) return false;
  lastSig = sig;
  $("#view").innerHTML = html;
  return true;
}

async function refreshBadge() {
  try {
    const k = await api("/api/kpis");
    $("#badge-tasks").textContent = k.openTasks || "";
    const ks = k.killSwitch || {};
    const on = [["intake", "K1 Eingang"], ["agents", "K2 Agent"], ["execution", "K3 Ausführung"]].filter(([x]) => ks[x]).map(([, l]) => l);
    const b = $("#ks-banner");
    b.hidden = !on.length;
    b.innerHTML = on.length ? `⛔ Notbremse aktiv: <b>${on.join(", ")}</b> angehalten. Arbeit wird zurückgehalten, nicht verworfen. <a href="#/governance/overview">Zur Notbremse →</a>` : "";
  } catch { /* offline */ }
}

function renderHelp(view) {
  const host = $("#help");
  host.innerHTML = view !== "wissen" && window.helpBox ? window.helpBox(view) : "";
  const d = host.querySelector("details");
  if (d) d.ontoggle = () => { try { localStorage.setItem("aeap.help." + view, d.open ? "open" : "closed"); } catch { /* ignore */ } };
}

// =====================================================================
// Overview
// =====================================================================
async function renderOverview() {
  const [k, q, erp, ev, alerts] = await Promise.all([api("/api/kpis"), api("/api/queues"), api("/api/erp"), api("/api/audit?limit=14"), api("/api/alerts")]);
  const qStat = (queue, status) => q.stats.filter((s) => s.queue === queue && (!status || s.status === status)).reduce((a, s) => a + s.n, 0);
  const active = Object.entries(k.byState).filter(([s]) => !TERMINAL.has(s)).reduce((a, [, n]) => a + n, 0);
  const comp = (id, name, cnt, title = "") => `<div class="comp" title="${esc(title)}"><span><b>${id}</b> ${esc(name)}</span><span class="cnt">${cnt ?? ""}</span></div>`;
  const layer = (name, sub, cls, comps) => `<div class="layer ${cls}"><div class="layer-name">${name}<span>${sub}</span></div>${comps.join("")}</div>`;
  const arrow = (t) => `<div class="arch-arrow">▼ ${esc(t)}</div>`;

  const states = Object.entries(k.byState).sort((a, b) => b[1] - a[1]);
  const maxState = Math.max(1, ...states.map(([, n]) => n));
  const tiers = ["R0", "R1", "R2", "R3"].map((t) => [t, k.byTier[t] || 0]);
  const maxTier = Math.max(1, ...tiers.map(([, n]) => n));

  const html = `
  ${alerts.length ? `<div class="card" style="margin-bottom:16px"><div class="card-h"><h2>Governance-Alarme</h2><span class="sub">${alerts.length} aktiv · Abschnitt 2.8</span><div class="right"><a href="#/governance/overview">Alle mit Handlungsempfehlung →</a></div></div><div class="card-b">${alertsHtml(alerts, true)}</div></div>` : ""}
  <div class="grid g4">
    <div class="card kpi"><div class="label">Cases gesamt</div><div class="value">${k.total}</div><div class="hint">${active} aktiv · ${k.closed} abgeschlossen</div></div>
    <div class="card kpi"><div class="label">Straight-Through-Rate</div><div class="value">${pct(k.stpRate)}</div><div class="hint">${k.straightThrough} ohne menschliche Interaktion abgeschlossen</div></div>
    <div class="card kpi"><div class="label">Offene Tasks (Action Center)</div><div class="value">${k.openTasks}</div><div class="hint">${k.openApprovals} Freigaben · Rest Validierung/Eskalation</div></div>
    <div class="card kpi"><div class="label">Schutzmechanismen ausgelöst</div><div class="value">${k.policyRejects + k.hashBlocks + k.duplicatesDropped + k.deadLetters}</div>
      <div class="hint">${k.policyRejects} Policy · ${k.hashBlocks} Hash · ${k.duplicatesDropped} Duplikat · ${k.deadLetters} DLQ</div></div>
  </div>

  <div class="grid g-side" style="margin-top:16px">
    <div class="card">
      <div class="card-h"><h2>Zielarchitektur · Live-Zähler</h2><span class="sub">gestrichelt = probabilistisch</span></div>
      <div class="card-b arch">
        ${layer("L0 Ingress", "stochastisch", "", [comp("[1]", "Mail/Portal/EDI", k.total + k.deadLetters + k.duplicatesDropped, "Eingegangene Nachrichten"), comp("[2]", "Integration Service", qStat("AEAP_Intake"), "Event-Trigger → Intake-Queue"), comp("[3]", "Partner-/ERP-Events", "–", "Webhooks, nicht simuliert")])}
        ${arrow("Case-Event + Idempotency-Key")}
        ${layer("L1 Orchestration", "Decision & Human", "", [comp("[4]", "Maestro", active, "aktive Prozessinstanzen"), comp("[5]", "PDP", Object.values(k.byTier).reduce((a, b) => a + b, 0), "Entscheidungen"), comp("[6]", "Action Center", k.openTasks, "offene Tasks")])}
        ${arrow("Agent-Task (sanitized Case, Tool-Scope)")}
        ${layer("L2 Cognitive", "nur Vorschläge", "prob", [comp("[A]", "Agent Builder", k.llmCalls, "LLM-Calls supplier-triage@1.4.2"), comp("[B]", "Coded Agent", "–", "Option, nicht aktiv"), comp("[C]", "External Bridge", "–", "O-3 offen")])}
        ${arrow("jeder LLM-Call")}
        ${layer("L3 Governance", "einziger LLM-Egress", "gov", [comp("[10]", "AI Trust Layer", k.llmTokens.toLocaleString("de-DE"), "Tokens via Gateway"), comp("[11]", "Guardrails + PII", k.policyRejects, "durch Policy abgelehnt"), comp("[12]", "Context Grounding", "5", "SOP-/Policy-Snippets")])}
        ${arrow("genehmigte Aktion + Approval-Hash")}
        ${layer("L4 Execution", "deterministisch", "", [comp("[13]", "Orchestrator", k.execQueueDepth, "AEAP_Execution: New"), comp("[14]", "Robots", qStat("AEAP_Execution", "Successful"), "erfolgreiche Transaktionen"), comp("[15]", "Doc. Understanding", k.total, "verarbeitete Cases")])}
        ${arrow("API zuerst, UI als Fallback")}
        ${layer("L5 Systems of Record", "simuliert", "", [comp("[16]", "ERP (SAP)", erp.documents.length, "Belege"), comp("[17]", "CRM/ITSM", erp.tickets.length, "Tickets"), comp("[18]", "Legacy/UI", erp.changeLog.length, "UI-Änderungen (SAP GUI)")])}
      </div>
    </div>

    <div class="grid" style="align-content:start">
      <div class="card">
        <div class="card-h"><h3>Cases nach Zustand</h3></div>
        <div class="card-b bars">
          ${states.length ? states.map(([s, n]) => `<div class="bar-row"><span>${statePill(s)}</span><div class="bar-track"><div class="bar-fill" style="width:${(n / maxState) * 100}%"></div></div><span class="mono">${n}</span></div>`).join("") : `<div class="empty">Noch keine Cases. Unter <a href="#/intake">Eingang</a> starten.</div>`}
        </div>
      </div>
      <div class="card">
        <div class="card-h"><h3>Risk Tiers (1.7)</h3></div>
        <div class="card-b bars">
          ${tiers.map(([t, n]) => `<div class="bar-row"><span>${tierPill(t)} <span class="muted small">${{ R0: "voll automatisch", R1: "automatisch", R2: "1× Approval", R3: "4-Augen + OOB" }[t]}</span></span><div class="bar-track"><div class="bar-fill" style="width:${(n / maxTier) * 100}%;background:var(--r${t[1]})"></div></div><span class="mono">${n}</span></div>`).join("")}
        </div>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-h"><h3>Letzte Ereignisse</h3><div class="right"><a href="#/audit">Alle anzeigen →</a></div></div>
    <ul class="timeline">${ev.map(eventRow).join("") || `<li class="empty">Keine Ereignisse</li>`}</ul>
  </div>`;
  paint(JSON.stringify([k, q.stats, ev.map((e) => e.id), alerts]), html);
}

function eventRow(e, withCase = true) {
  const caseLink = withCase && e.case_id ? `<a href="#/cases/${esc(e.case_id)}" class="mono">${esc(e.case_id)}</a> · ` : "";
  const data = e.data ? `<details><summary>Details</summary><pre class="json" style="border-radius:6px;max-height:280px">${jsonHtml(e.data)}</pre></details>` : "";
  return `<li class="k-${esc(e.kind)}"><span class="ts">${fmtTime(e.ts)}</span><span class="stage">${esc(e.stage || "")}</span><span class="comp-name">${esc(e.component || "")}</span><span class="msg">${caseLink}${esc(e.message)}${data}</span></li>`;
}

// =====================================================================
// Intake
// =====================================================================
async function renderIntake() {
  const sc = META.scenarios.map((s) => `
    <div class="card scenario">
      <h3>${esc(s.title)}</h3>
      <div class="expect">Erwartet: ${esc(s.expect)}</div>
      <div class="row">
        <button class="btn primary sm" data-send="${esc(s.id)}">Senden</button>
        <button class="btn sm" data-dup="${esc(s.id)}" title="Gleiche messageId + Anhang: zweites Senden wird als Duplikat verworfen">Mit fester ID senden</button>
      </div>
    </div>`).join("");

  $("#view").innerHTML = `
  <div class="callout info">Referenzszenario A3, Supplier Inbound Case: Jede Nachricht durchläuft S0 bis S10. Der Case wird bis zum nächsten Wartezustand (Validierung, Freigabe) oder Endzustand automatisch weitergeführt.</div>
  <div class="section-title">Szenarien</div>
  <div class="grid g4">${sc}</div>

  <div class="grid g2" style="margin-top:18px">
    <div class="card">
      <div class="card-h"><h3>Lastgenerator</h3><span class="sub">gemischter Eingang (A4: 5.000/Tag, hier verkleinert)</span></div>
      <div class="card-b row">
        <label class="field">Anzahl Nachrichten<input id="sim-count" type="number" min="1" max="500" value="25" style="width:120px"></label>
        <button class="btn primary" id="btn-sim" style="align-self:flex-end">Generieren</button>
        <span id="sim-result" class="muted small" style="align-self:flex-end"></span>
      </div>
    </div>
    <div class="card">
      <div class="card-h"><h3>Eigene Nachricht</h3></div>
      <div class="card-b grid" style="gap:10px">
        <div class="form-grid">
          <label class="field">Absender<input id="m-from" value="rechnung@mueller-industrie.example"></label>
          <label class="field">Betreff<input id="m-subject" value="Rechnung RE-2026-2001"></label>
          <label class="field">Anhang (Dateiname)<input id="m-file" value="Rechnung_RE-2026-2001.pdf"></label>
          <label class="field">Scan-Qualität <span id="m-q-v">1.00</span><input id="m-quality" type="range" min="0.3" max="1" step="0.01" value="1"></label>
        </div>
        <label class="field">Mail-Text<textarea id="m-body" rows="2">Anbei unsere Rechnung.</textarea></label>
        <label class="field">Anhang-Inhalt (leer = kein Anhang)<textarea id="m-content" rows="7">RECHNUNG
Lieferantennummer: 100234
Rechnungsnummer: RE-2026-2001
Bestellnummer: 4500012345
Gesamtbetrag: 2.150,00 EUR
Zahlbar innerhalb 30 Tagen netto.</textarea></label>
        <div class="row"><button class="btn primary" id="btn-custom">Senden</button></div>
      </div>
    </div>
  </div>`;

  const showResult = (r) => {
    if (r.status === "ACCEPTED") toast(`<b>${esc(r.caseId)}</b> angelegt → ${statePill(r.state)} <a href="#/cases/${esc(r.caseId)}">öffnen</a>`, "ok");
    else if (r.status === "DUPLICATE") toast(`Duplikat verworfen (Unique Reference)${r.caseId ? ` · ursprünglich <a href="#/cases/${esc(r.caseId)}">${esc(r.caseId)}</a>` : ""}`, "warn");
    else if (r.status === "DEAD_LETTER") toast(`Poison Message → AEAP_DeadLetter: ${esc(r.reason)}`, "bad");
    refreshBadge();
  };
  $$("[data-send]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try { showResult(await api("/api/ingest", { method: "POST", body: { scenarioId: b.dataset.send } })); } catch (e) { toast(esc(e.message), "bad"); }
    b.disabled = false;
  });
  $$("[data-dup]").forEach((b) => b.onclick = async () => {
    try { showResult(await api("/api/ingest", { method: "POST", body: { scenarioId: b.dataset.dup, fixedMessageId: true } })); } catch (e) { toast(esc(e.message), "bad"); }
  });
  $("#btn-sim").onclick = async () => {
    const b = $("#btn-sim"); b.disabled = true; b.textContent = "Läuft …";
    try {
      const r = await api("/api/simulate", { method: "POST", body: { count: Number($("#sim-count").value) } });
      $("#sim-result").innerHTML = Object.entries(r.result).map(([s, n]) => `${statePill(s)} ${n}`).join(" ");
      toast(`${r.count} Nachrichten verarbeitet`, "ok"); refreshBadge();
    } catch (e) { toast(esc(e.message), "bad"); }
    b.disabled = false; b.textContent = "Generieren";
  };
  $("#m-quality").oninput = (e) => $("#m-q-v").textContent = Number(e.target.value).toFixed(2);
  $("#btn-custom").onclick = async () => {
    const content = $("#m-content").value;
    const mail = {
      from: $("#m-from").value, subject: $("#m-subject").value, body: $("#m-body").value,
      attachments: content.trim() ? [{ filename: $("#m-file").value || "anhang.pdf", content, quality: Number($("#m-quality").value) }] : [],
    };
    try { showResult(await api("/api/ingest", { method: "POST", body: { mail } })); } catch (e) { toast(esc(e.message), "bad"); }
  };
}

// =====================================================================
// Cases
// =====================================================================
let caseFilter = { state: "", tier: "", q: "" };

async function renderCases(arg) {
  if (arg) return renderCaseDetail(arg);
  const cases = await api("/api/cases" + (caseFilter.state ? `?state=${encodeURIComponent(caseFilter.state)}` : ""));
  const q = caseFilter.q.toLowerCase();
  const rows = cases.filter((c) => (!caseFilter.tier || c.riskTier === caseFilter.tier) &&
    (!q || [c.caseId, c.subject, c.from, c.intent].join(" ").toLowerCase().includes(q)));
  const states = [...new Set(cases.map((c) => c.state))];
  const html = `
  <div class="card">
    <div class="card-h">
      <h2>${rows.length} Cases</h2>
      <div class="right">
        <input id="f-q" placeholder="Suche …" value="${esc(caseFilter.q)}" style="width:180px">
        <select id="f-state"><option value="">Alle Zustände</option>${[...new Set([...states, caseFilter.state].filter(Boolean))].sort().map((s) => `<option ${s === caseFilter.state ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
        <select id="f-tier"><option value="">Alle Tiers</option>${["R0", "R1", "R2", "R3"].map((t) => `<option ${t === caseFilter.tier ? "selected" : ""}>${t}</option>`).join("")}</select>
      </div>
    </div>
    <div class="table-wrap" style="max-height:none">
      <table>
        <thead><tr><th>Case</th><th>Eingang</th><th>Absender / Betreff</th><th>Dokument</th><th>Intent</th><th>Tier</th><th>PDP</th><th class="num">Betrag</th><th>Zustand</th></tr></thead>
        <tbody>${rows.map((c) => `
          <tr class="click" data-case="${esc(c.caseId)}">
            <td class="mono">${esc(c.caseId)}</td>
            <td class="small muted">${fmtDateTime(c.createdAt)}</td>
            <td><div>${esc(c.subject)}</div><div class="small muted">${esc(c.from)}</div></td>
            <td>${esc(c.docType || "–")}</td>
            <td class="mono small">${esc(c.intent || "–")}</td>
            <td>${tierPill(c.riskTier)}</td>
            <td class="mono small">${esc(c.decision || "–")}</td>
            <td class="num">${fmtEur(c.amount)}</td>
            <td>${statePill(c.state)}</td>
          </tr>`).join("") || `<tr><td colspan="9" class="empty">Keine Cases</td></tr>`}</tbody>
      </table>
    </div>
  </div>`;
  if (paint(JSON.stringify([rows, caseFilter]), html)) {
    $$("[data-case]").forEach((tr) => tr.onclick = () => location.hash = `#/cases/${tr.dataset.case}`);
    $("#f-state").onchange = (e) => { caseFilter.state = e.target.value; navigate(); };
    $("#f-tier").onchange = (e) => { caseFilter.tier = e.target.value; navigate(); };
    const fq = $("#f-q");
    fq.oninput = () => { caseFilter.q = fq.value; clearTimeout(fq._t); fq._t = setTimeout(async () => { await navigate(); const n = $("#f-q"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }, 250); };
  }
}

function lifecycleHtml(env, state) {
  const path = (env.history || []).map((h) => h.to);
  const steps = [...path];
  if (!TERMINAL.has(state)) {
    const idx = HAPPY_PATH.findIndex((s) => s.split("|").includes(state));
    if (idx >= 0) steps.push(...HAPPY_PATH.slice(idx + 1).map((s) => "?" + s.split("|").pop()));
  }
  return `<div class="lifecycle">${steps.map((s, i) => {
    const future = s.startsWith("?");
    const name = future ? s.slice(1) : s;
    const current = !future && i === path.length - 1;
    const cls = future ? "" : current ? `current ${STATE_CLASS[name] || ""}` : "done";
    return (i ? `<span class="lc-sep">→</span>` : "") + `<span class="lc-step ${cls}">${esc(name)}</span>`;
  }).join("")}</div>`;
}

async function renderCaseDetail(caseId) {
  const v = await api(`/api/cases/${encodeURIComponent(caseId)}`);
  const c = v.case, env = c.envelope;
  const prop = env.agent?.proposal;
  const pol = env.policy;
  const openTask = v.tasks.find((t) => t.status === "PENDING");
  const doc = env.documents?.[0];
  const html = `
  <div class="row" style="margin-bottom:12px">
    <a href="#/cases">← Alle Cases</a>
    <span class="spacer"></span>
    ${openTask ? `<a class="btn primary sm" href="#/action/${esc(openTask.task_id)}" style="text-decoration:none">Task ${esc(openTask.task_id)} im Action Center öffnen</a>` : ""}
    <a class="btn sm" href="/api/cases/${esc(c.case_id)}/auditpack" download style="text-decoration:none">Audit-Pack herunterladen</a>
  </div>
  <div class="card">
    <div class="card-h">
      <h2 class="mono">${esc(c.case_id)}</h2>${statePill(c.state)} ${tierPill(c.risk_tier)}
      <span class="sub">correlationId <span class="mono">${esc(c.correlation_id)}</span></span>
    </div>
    <div class="card-b">
      ${lifecycleHtml(env, c.state)}
      ${env.escalation ? `<div class="callout bad" style="margin-top:12px"><b>Eskaliert (${esc(env.escalation.stage)}):</b> ${esc(env.escalation.reason)}</div>` : ""}
      ${pol && pol.decision === "REJECT" ? `<div class="callout bad" style="margin-top:12px"><b>REJECTED_POLICY:</b> ${esc(pol.reasons.join("; "))}</div>` : ""}
      <div class="grid evidence" style="margin-top:14px">
        <dl class="kv">
          <dt>Absender</dt><dd>${esc(env.source.from)}</dd>
          <dt>Betreff</dt><dd>${esc(env.source.subject)}</dd>
          <dt>Eingang</dt><dd>${fmtDateTime(env.source.receivedAt)}</dd>
          <dt>Idempotency-Key</dt><dd class="mono small">${esc(env.idempotencyKey.slice(0, 24))}…</dd>
        </dl>
        <dl class="kv">
          <dt>Dokument</dt><dd>${doc ? `${esc(doc.type)} (${doc.classConfidence})` : "–"}</dd>
          <dt>Agent</dt><dd>${env.agent ? `${esc(env.agent.agentId)}@${esc(env.agent.version)}` : "–"}</dd>
          <dt>Intent</dt><dd class="mono">${esc(prop?.intent || "–")}</dd>
          <dt>Vorschlag</dt><dd>${esc(prop?.summary || env.agent?.error || "–")}</dd>
        </dl>
        <dl class="kv">
          <dt>PDP-Entscheidung</dt><dd class="mono">${esc(pol?.decision || "–")}</dd>
          <dt>Regeln</dt><dd class="mono small">${esc((pol?.rulesFired || []).join(", ") || "–")}</dd>
          <dt>Freigabe</dt><dd>${env.approval ? `${esc(env.approval.mode)} · ${esc(env.approval.decision)}${env.approval.approvedBy ? " · " + esc(env.approval.approvedBy.join(", ")) : ""}` : "–"}</dd>
          <dt>Ausführung</dt><dd>${env.execution ? `${esc(env.execution.result)} (Versuche: ${env.execution.attempt})` : "–"}</dd>
        </dl>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-h"><h3>Trace (Spans je Komponente)</h3><span class="sub">${v.events.length} Ereignisse · correlationId als Trace-ID (P6)</span></div>
    <ul class="timeline" style="max-height:560px;overflow:auto">${v.events.map((e) => eventRow(e, false)).join("")}</ul>
  </div>
  <div class="grid g2" style="margin-top:16px">
    <div class="grid" style="display:contents">
      <div class="card">
        <div class="card-h"><h3>Case Envelope (1.8)</h3><span class="sub">Datenvertrag durch alle Layer</span></div>
        <pre class="json">${jsonHtml(env)}</pre>
      </div>
      <div class="card" style="align-self:start">
        <div class="card-h"><h3>Tasks &amp; Queue-Items</h3></div>
        <table class="fixed">
          <thead><tr><th>Objekt</th><th>Typ</th><th>Status</th><th>Detail</th></tr></thead>
          <tbody>
            ${v.tasks.map((t) => `<tr><td><a class="mono" href="#/action/${esc(t.task_id)}">${esc(t.task_id)}</a></td><td>${esc(t.type)}</td><td><span class="pill ${t.status === "PENDING" ? "warn" : "ok"}">${esc(t.status)}</span></td><td class="small">${esc(t.decision || t.title)}</td></tr>`).join("")}
            ${v.queueItems.map((i) => `<tr><td class="mono">${esc(i.queue)}#${i.id}</td><td>Queue-Item</td><td>${queueStatus(i.status)}</td><td class="small">${esc(i.exception_reason || i.reference.slice(0, 28))}</td></tr>`).join("")}
          </tbody>
        </table>
      </div>
    </div>
  </div>`;
  paint(JSON.stringify([c.updated_at, v.events.length, v.tasks.map((t) => t.status)]), html);
}

// =====================================================================
// Action Center
// =====================================================================
let showCompleted = false;

async function renderAction(taskId) {
  const tasks = await api("/api/tasks" + (showCompleted ? "" : "?status=PENDING"));
  let selected = taskId ? tasks.find((t) => t.task_id === taskId) : null;
  if (taskId && !selected) {
    const all = await api("/api/tasks");
    selected = all.find((t) => t.task_id === taskId) || null;
  }
  const typeLabel = { APPROVAL: "Freigabe", VALIDATION: "Validierung", ESCALATION: "Eskalation" };
  $("#view").innerHTML = `
  <div class="grid" style="grid-template-columns:minmax(280px,380px) minmax(0,1fr)">
    <div class="card task-list" style="align-self:start">
      <div class="card-h"><h3>${showCompleted ? "Alle Tasks" : "Offene Tasks"} (${tasks.length})</h3>
        <div class="right"><label class="small muted row" style="gap:4px"><input type="checkbox" id="chk-done" ${showCompleted ? "checked" : ""}> erledigte</label></div></div>
      <div style="max-height:75vh;overflow:auto">
      ${tasks.map((t) => `
        <div class="task ${selected && selected.task_id === t.task_id ? "sel" : ""}" data-task="${esc(t.task_id)}">
          <div class="row" style="gap:6px">${tierPill(t.risk_tier)} <b>${esc(typeLabel[t.type] || t.type)}</b>
            <span class="mono small muted">${esc(t.task_id)}</span><span class="spacer"></span>
            ${t.status === "PENDING" ? (t.slaOverdue ? `<span class="pill bad">SLA überschritten</span>` : `<span class="pill warn">offen</span>`) : `<span class="pill ok">${esc(t.decision)}</span>`}</div>
          <div class="small">${esc(t.title)}</div>
          <div class="small muted mono">${esc(t.case_id)} · ${fmtTime(t.created_at)}${t.required_approvals > 1 ? ` · ${t.approvals.length}/${t.required_approvals} Freigaben` : ""}</div>
        </div>`).join("") || `<div class="empty">Keine offenen Tasks 🎉<br><span class="small">Unter <a href="#/intake">Eingang</a> z. B. „Bankdatenänderung“ senden.</span></div>`}
      </div>
    </div>
    <div id="task-detail">${selected ? "" : `<div class="card"><div class="empty">Task links auswählen.</div></div>`}</div>
  </div>`;
  $("#chk-done").onchange = (e) => { showCompleted = e.target.checked; navigate(); };
  $$("[data-task]").forEach((el) => el.onclick = () => location.hash = `#/action/${el.dataset.task}`);
  if (selected) renderTaskDetail(selected);
}

function docsTable(docs, thresholds, editable) {
  return docs.map((d) => `
    <div class="card" style="margin-top:10px">
      <div class="card-h"><h3 class="mono">${esc(d.docId)}</h3><span class="sub">${esc(d.filename)} · Scan-Qualität ${d.scanQuality}</span>
        <div class="right">${editable
          ? `<select data-doc-type="${esc(d.docId)}">${["Invoice", "Complaint", "BankDetailChange", "Other"].map((t) => `<option ${t === d.type ? "selected" : ""}>${t}</option>`).join("")}</select>`
          : esc(d.type)} ${confCell(d.classConfidence, thresholds.class)}</div></div>
      <table><thead><tr><th>Feld</th><th>Wert</th><th class="num">Konfidenz</th></tr></thead><tbody>
      ${Object.entries(d.fields).map(([k, val]) => `<tr><td class="mono small">${esc(k)}</td>
        <td>${editable ? `<input data-doc="${esc(d.docId)}" data-field="${esc(k)}" value="${esc(val ?? "")}" style="width:100%">` : esc(val ?? "–")}</td>
        <td class="num">${confCell(d.fieldConfidence[k], thresholds.field)}</td></tr>`).join("")}
      </tbody></table>
    </div>`).join("");
}

function renderTaskDetail(t) {
  const p = t.payload || {};
  const host = $("#task-detail");
  const pending = t.status === "PENDING";
  const head = `<div class="card-h">${tierPill(t.risk_tier)}<h2>${esc(t.title)}</h2>
    <div class="right"><a class="mono small" href="#/cases/${esc(t.case_id)}">${esc(t.case_id)} →</a></div></div>`;
  const sla = `<span class="small ${t.slaOverdue ? "conf-low" : "muted"}">SLA fällig ${fmtDateTime(t.sla_due)}${t.escalationLevel ? ` · Eskalationskette: ${esc(t.escalationLevel)}` : ""}</span>`;

  if (t.type === "VALIDATION") {
    host.innerHTML = `<div class="card">${head}<div class="card-b">
      <div class="callout warn"><b>Validation Station (DU):</b> Konfidenz unter Schwelle (Klasse ${p.thresholds.class}, Feld ${p.thresholds.field}).<br>${p.reasons.map(esc).join("<br>")}</div>
      ${docsTable(p.documents, p.thresholds, pending)}
      <div class="row" style="margin-top:14px">${sla}<span class="spacer"></span>
        ${pending ? `<button class="btn primary" id="btn-validate">Validieren &amp; fortsetzen als ${esc(userName(currentUser))}</button>` : `<span class="pill ok">${esc(t.decision)} durch ${esc(t.approvals[0]?.user)}</span>`}</div>
      ${!pending && t.approvals[0]?.corrections?.length ? `<div class="small muted" style="margin-top:8px">Korrekturen: ${t.approvals[0].corrections.map(esc).join("; ")}</div>` : ""}
    </div></div>`;
    if (pending) $("#btn-validate").onclick = async () => {
      const docs = p.documents.map((d) => ({
        docId: d.docId,
        type: $(`[data-doc-type="${d.docId}"]`).value,
        fields: Object.fromEntries($$(`[data-doc="${d.docId}"]`).map((i) => [i.dataset.field, i.value])),
      }));
      try {
        await api(`/api/tasks/${t.task_id}/validate`, { method: "POST", body: { user: currentUser, documents: docs } });
        toast(`Validiert. Case ${esc(t.case_id)} läuft weiter.`, "ok"); refreshBadge(); navigate();
      } catch (e) { toast(esc(e.message), "bad"); }
    };
    return;
  }

  if (t.type === "ESCALATION") {
    host.innerHTML = `<div class="card">${head}<div class="card-b">
      <div class="callout bad"><b>Eskalation aus ${esc(p.stage)}:</b> ${esc(p.reason)}</div>
      ${p.summary ? `<p><b>Agent-Vorschlag:</b> ${esc(p.summary)}</p>` : ""}
      <p class="muted small">Der Case bleibt im Zustand ESCALATED. Die weitere Bearbeitung erfolgt manuell außerhalb des automatisierten Pfads. Der vollständige Trace steht im Audit-Pack.</p>
      <div class="row">${sla}<span class="spacer"></span>
      ${pending ? `<button class="btn primary" id="btn-ack">Übernehmen als ${esc(userName(currentUser))}</button>` : `<span class="pill ok">übernommen von ${esc(t.approvals[0]?.user)}</span>`}</div>
    </div></div>`;
    if (pending) $("#btn-ack").onclick = async () => {
      try { await api(`/api/tasks/${t.task_id}/acknowledge`, { method: "POST", body: { user: currentUser } }); toast("Eskalation übernommen", "ok"); refreshBadge(); navigate(); }
      catch (e) { toast(esc(e.message), "bad"); }
    };
    return;
  }

  // APPROVAL: Evidence Pack
  const sig = p.signals || {};
  const fraud = (p.reasons || []).some((r) => r.includes("Betrugsindikator"));
  const alreadyApproved = t.approvals.some((a) => a.user === currentUser);
  host.innerHTML = `<div class="card">${head}<div class="card-b">
    <p style="margin-top:0;font-size:15px">${esc(p.summary)}</p>
    <div class="callout ${fraud ? "bad" : t.risk_tier === "R3" ? "warn" : "info"}"><b>PDP-Begründung:</b><br>${(p.reasons || []).map(esc).join("<br>")}
      <div class="small mono" style="margin-top:6px;opacity:.8">${esc((p.rulesFired || []).join(" · "))}</div></div>

    <div class="section-title">Evidence Pack</div>
    <div class="grid evidence">
      <dl class="kv">
        <dt>Intent</dt><dd class="mono">${esc(p.intent)}</dd>
        <dt>Agent-Risiken</dt><dd>${(p.agentRisks || []).map((r) => `<span class="pill warn">${esc(r)}</span>`).join(" ") || "–"}</dd>
        <dt>Grounding-Quellen</dt><dd class="small">${(p.citations || []).map(esc).join(", ") || "–"}</dd>
        <dt>Antwortentwurf</dt><dd class="small">${esc(p.replyDraft || "–")}</dd>
      </dl>
      <dl class="kv">
        ${Object.entries(sig).map(([k, v]) => `<dt class="mono small">${esc(k)}</dt><dd class="small">${typeof v === "boolean" ? (v ? "✔ ja" : "✘ nein") : esc(v ?? "–")}</dd>`).join("")}
      </dl>
    </div>
    ${docsTable(p.documents || [], { class: 0.85, field: 0.8 }, false)}

    <div class="section-title">Geplante Aktion (T-WRITE, Deferred Execution)</div>
    <pre class="json" style="border-radius:8px">${jsonHtml(p.plannedActions)}</pre>
    <div class="section-title">Approval-Hash (TOCTOU-Schutz)</div>
    <div class="hash">${esc(t.evidence_hash)}</div>
    <div class="small muted" style="margin-top:4px">SHA-256 über das kanonisierte JSON. Der Robot berechnet den Hash vor der Ausführung neu und führt bei Abweichung nicht aus.</div>

    <div class="section-title">Freigaben ${t.approvals.length}/${t.required_approvals}${t.require_oob ? " · Out-of-Band-Verifikation erforderlich" : ""}</div>
    ${t.approvals.map((a) => `<div class="small">${a.denied ? "✘" : "✔"} <b>${esc(userName(a.user))}</b> · ${fmtDateTime(a.ts)}${a.oobVerified ? " · <span class='pill ok'>OOB verifiziert</span>" : ""}${a.comment ? ` · „${esc(a.comment)}“` : ""}</div>`).join("") || `<div class="small muted">noch keine</div>`}

    ${pending ? `
    <div class="card" style="margin-top:14px;background:var(--panel-2)"><div class="card-b grid" style="gap:10px">
      ${t.require_oob ? `<label class="toggle-row"><input type="checkbox" id="chk-oob"><span>Rückruf unter der <b>Stammdaten-Telefonnummer</b> durchgeführt und Änderung bestätigt (nicht die Nummer aus der Mail)</span></label>` : ""}
      <label class="field">Kommentar<input id="appr-comment" placeholder="optional"></label>
      ${alreadyApproved ? `<div class="callout warn">Sie haben bereits freigegeben. 4-Augen-Prinzip: oben einen anderen Benutzer wählen.</div>` : ""}
      ${t.risk_tier === "R3" ? `<div class="small muted">R3-Regeln: zwei verschiedene Personen, mindestens eine Teamlead, Rückruf vor der letzten Freigabe.</div>` : ""}
      ${!canDo("task.approve") ? permHint("task.approve") : ""}
      <div class="row">${sla}<span class="spacer"></span>
        <button class="btn danger" id="btn-deny">Ablehnen</button>
        <button class="btn ok solid" id="btn-approve" ${alreadyApproved ? "disabled" : ""}>Freigeben als ${esc(userName(currentUser))}</button>
      </div>
    </div></div>` : `<div class="callout ${t.decision === "APPROVED" ? "ok" : "bad"}" style="margin-top:12px">Entscheidung: <b>${esc(t.decision)}</b></div>`}
  </div></div>`;

  if (!pending) return;
  $("#btn-approve").onclick = async () => {
    try {
      const r = await api(`/api/tasks/${t.task_id}/approve`, { method: "POST", body: {
        user: currentUser, oobVerified: $("#chk-oob")?.checked || false, comment: $("#appr-comment").value, confirmHash: t.evidence_hash } });
      toast(r.status === "COMPLETED" ? `Freigegeben. Robot führt ${esc(t.case_id)} aus.` : `Freigabe ${r.approvals.length}/${r.required_approvals} erfasst. Zweite Person nötig.`, "ok");
      refreshBadge(); navigate();
    } catch (e) { toast(esc(e.message), "bad"); }
  };
  $("#btn-deny").onclick = async () => {
    try { await api(`/api/tasks/${t.task_id}/deny`, { method: "POST", body: { user: currentUser, comment: $("#appr-comment").value } }); toast("Abgelehnt, Case geschlossen (CLOSED_DENIED)", "warn"); refreshBadge(); navigate(); }
    catch (e) { toast(esc(e.message), "bad"); }
  };
}

// =====================================================================
// Queues
// =====================================================================
function queueStatus(s) {
  return `<span class="pill ${{ Successful: "ok", Failed: "bad", New: "warn", InProgress: "info" }[s] || ""}">${esc(s)}</span>`;
}

let queueTab = "AEAP_Execution";

async function renderQueues() {
  const [q, s] = await Promise.all([api("/api/queues"), api("/api/settings")]);
  const queues = ["AEAP_Intake", "AEAP_Execution", "AEAP_DeadLetter"];
  const count = (queue, status) => q.stats.filter((x) => x.queue === queue && (!status || x.status === status)).reduce((a, x) => a + x.n, 0);
  const items = q.items.filter((i) => i.queue === queueTab);
  const autoRun = s.robot.autoRun;
  const html = `
  <div class="grid g3">
    ${queues.map((n) => `<div class="card kpi"><div class="label mono">${n}</div><div class="value">${count(n)}</div>
      <div class="hint">${["New", "InProgress", "Successful", "Failed"].map((st) => `${st}: ${count(n, st)}`).join(" · ")}</div></div>`).join("")}
  </div>
  <div class="card" style="margin-top:16px">
    <div class="card-h">
      <div class="tabs" style="margin:0;border:0">${queues.map((n) => `<button data-qtab="${n}" class="${n === queueTab ? "active" : ""}">${n}</button>`).join("")}</div>
      <div class="right">
        <label class="small row" style="gap:6px"><input type="checkbox" id="chk-auto" ${autoRun ? "checked" : ""}> Robot läuft automatisch</label>
        <button class="btn primary sm" id="btn-robot" ${count("AEAP_Execution", "New") ? "" : "disabled"}>Robot ausführen (${count("AEAP_Execution", "New")} neu)</button>
      </div>
    </div>
    ${queueTab === "AEAP_Execution" ? `<div class="card-b"><div class="callout info small">TOCTOU-Demo: „Robot läuft automatisch“ ausschalten, einen R2- oder R3-Case freigeben und hier den Payload <b>manipulieren</b>. Danach den Robot ausführen. Der Robot berechnet den Approval-Hash neu und blockiert die Ausführung.</div></div>` : ""}
    <div class="table-wrap">
      <table><thead><tr><th>#</th><th>Reference</th><th>Case</th><th>Status</th><th class="num">Versuche</th><th>Exception</th><th>Payload</th><th></th></tr></thead>
      <tbody>${items.map((i) => `<tr>
        <td class="mono">${i.id}</td>
        <td class="mono small">${esc(i.reference.length > 30 ? i.reference.slice(0, 30) + "…" : i.reference)}</td>
        <td>${i.case_id ? `<a class="mono" href="#/cases/${esc(i.case_id)}">${esc(i.case_id)}</a>` : "–"}</td>
        <td>${queueStatus(i.status)}</td>
        <td class="num">${i.attempt || 0}</td>
        <td class="small">${i.exception_type ? `<b>${esc(i.exception_type)}</b>: ${esc(i.exception_reason)}` : ""}</td>
        <td><details><summary class="small muted">anzeigen</summary><pre class="json" style="border-radius:6px;max-width:460px">${jsonHtml(i.payload)}</pre></details></td>
        <td>${i.queue === "AEAP_Execution" && i.status === "New" ? `<button class="btn danger sm" data-tamper="${i.id}">Payload manipulieren</button>` : ""}</td>
      </tr>`).join("") || `<tr><td colspan="8" class="empty">Leer</td></tr>`}</tbody></table>
    </div>
  </div>`;
  if (paint(JSON.stringify([q.items.map((i) => [i.id, i.status, i.payload]), autoRun, queueTab]), html)) {
    $$("[data-qtab]").forEach((b) => b.onclick = () => { queueTab = b.dataset.qtab; navigate(); });
    $("#chk-auto").onchange = async (e) => { await api("/api/settings", { method: "PUT", body: { robot: { autoRun: e.target.checked } } }); toast(`Robot-Autostart ${e.target.checked ? "an" : "aus"}`); navigate(); };
    $("#btn-robot").onclick = async () => {
      const r = await api("/api/robot/run", { method: "POST" });
      r.forEach((x) => toast(`Item #${x.itemId}: <b>${esc(x.result)}</b>`, x.result === "VERIFIED" ? "ok" : "bad"));
      refreshBadge(); navigate();
    };
    $$("[data-tamper]").forEach((b) => b.onclick = async () => {
      try { const r = await api(`/api/queues/${b.dataset.tamper}/tamper`, { method: "POST" }); toast(`Manipuliert: ${esc(r.change)}`, "warn"); navigate(); }
      catch (e) { toast(esc(e.message), "bad"); }
    });
  }
}

// =====================================================================
// Systems of Record
// =====================================================================
let sysTab = "vendors";

async function renderSystems() {
  const d = await api("/api/erp");
  const tabs = { vendors: "Lieferanten [16]", purchaseOrders: "Bestellungen [16]", documents: "Belege [16]", changeLog: "Stammdaten-Änderungen (UI) [18]", tickets: "Tickets [17]", outbox: "Outbox [2]" };
  const tbl = (cols, rows, fn) => `<div class="table-wrap"><table><thead><tr>${cols.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${rows.map(fn).join("") || `<tr><td colspan="${cols.length}" class="empty">Leer</td></tr>`}</tbody></table></div>`;
  const body = {
    vendors: () => tbl(["Nr.", "Name", "IBAN", "Status", "Ø Rechnung", "E-Mail (Stamm)", "Telefon (Stamm)"], d.vendors, (v) => `<tr><td class="mono">${esc(v.supplier_id)}</td><td>${esc(v.name)}</td><td class="mono">${esc(v.iban)}</td><td><span class="pill ${v.status === "ACTIVE" ? "ok" : "bad"}">${esc(v.status)}</span></td><td class="num">${fmtEur(v.avg_invoice)}</td><td class="small">${esc(v.email)}</td><td class="small">${esc(v.phone)}</td></tr>`),
    purchaseOrders: () => tbl(["Bestellung", "Lieferant", "Betrag"], d.purchaseOrders, (p) => `<tr><td class="mono">${esc(p.po_number)}</td><td class="mono">${esc(p.supplier_id)}</td><td class="num">${fmtEur(p.amount)}</td></tr>`),
    documents: () => tbl(["Beleg", "Typ", "Lieferant", "Referenz", "Bestellung", "Betrag", "Case", "Gebucht"], d.documents, (x) => `<tr><td class="mono">${esc(x.doc_no)}</td><td>${esc(x.doc_type)}</td><td class="mono">${esc(x.supplier_id)}</td><td class="mono">${esc(x.reference)}</td><td class="mono">${esc(x.po_number || "–")}</td><td class="num">${fmtEur(x.amount)}</td><td><a class="mono" href="#/cases/${esc(x.case_id)}">${esc(x.case_id)}</a></td><td class="small muted">${fmtDateTime(x.created_at)}</td></tr>`),
    changeLog: () => tbl(["#", "Zeit", "Lieferant", "Feld", "Alt", "Neu", "Kanal", "Case"], d.changeLog, (x) => `<tr><td>${x.id}</td><td class="small">${fmtDateTime(x.ts)}</td><td class="mono">${esc(x.supplier_id)}</td><td>${esc(x.field)}</td><td class="mono small">${esc(x.old_value)}</td><td class="mono small">${esc(x.new_value)}</td><td class="small">${esc(x.channel)}</td><td><a class="mono" href="#/cases/${esc(x.case_id)}">${esc(x.case_id)}</a></td></tr>`),
    tickets: () => tbl(["#", "Zeit", "Tags", "Zusammenfassung", "Case"], d.tickets, (x) => `<tr><td>${x.id}</td><td class="small">${fmtDateTime(x.ts)}</td><td>${JSON.parse(x.tags || "[]").map((t) => `<span class="pill info">${esc(t)}</span>`).join(" ")}</td><td>${esc(x.summary)}</td><td><a class="mono" href="#/cases/${esc(x.case_id)}">${esc(x.case_id)}</a></td></tr>`),
    outbox: () => tbl(["#", "Zeit", "Empfänger", "Betreff", "Text", "Kanal"], d.outbox, (x) => `<tr><td>${x.id}</td><td class="small">${fmtDateTime(x.ts)}</td><td class="small">${esc(x.recipient)}</td><td>${esc(x.subject)}</td><td class="small">${esc(x.body)}</td><td class="small muted">${esc(x.channel)}</td></tr>`),
  };
  const html = `<div class="card"><div class="card-h"><div class="tabs" style="margin:0;border:0">${Object.entries(tabs).map(([k, v]) => `<button data-stab="${k}" class="${k === sysTab ? "active" : ""}">${v} <span class="muted">(${d[k].length})</span></button>`).join("")}</div></div>${body[sysTab]()}</div>`;
  if (paint(JSON.stringify([d, sysTab]), html)) {
    $$("[data-stab]").forEach((b) => b.onclick = () => { sysTab = b.dataset.stab; navigate(); });
  }
}

// =====================================================================
// Governance (SDD Abschnitt 2)
// =====================================================================
const GOV_TABS = {
  overview: "Alarme & Notbremse",
  rules: "Regeln (PDP)",
  changes: "Änderungsanträge",
  guardrails: "Guardrails",
  roles: "Rollen & Rechte",
  agents: "Agenten & Evaluation",
  demo: "Demo-Steuerung",
};
let govTab = "overview";
let openCr = null;

const userRole = (uid = currentUser) => (META.users.find((u) => u.id === uid) || {}).role;
const canDo = (perm, uid = currentUser) => (META.permissions?.[perm]?.roles || []).includes(userRole(uid));
const roleName = (r) => META.roles?.[r]?.name || r;
const sevClass = { crit: "bad", warn: "warn", info: "info" };
const gatePill = (g) => `<span class="pill ${{ PASSED: "ok", REVIEW: "warn", BLOCKED: "bad" }[g] || ""}">Gate ${esc(g)}</span>`;
const crStatus = (s) => `<span class="pill ${{ PENDING: "warn", APPLIED: "ok", REJECTED: "" }[s] || ""}">${esc({ PENDING: "offen", APPLIED: "aktiv", REJECTED: "abgelehnt" }[s] || s)}</span>`;
const permHint = (perm) => canDo(perm) ? "" : `<div class="small muted">Als ${esc(userName(currentUser))} (${esc(roleName(userRole()))}) nicht erlaubt: ${esc(META.permissions[perm].label)}. Oben rechts Benutzer wechseln.</div>`;

function alertsHtml(alerts, compact = false) {
  if (!alerts.length) return `<div class="callout ok">✔ Keine Alarme. Alles im grünen Bereich.</div>`;
  return `<div class="grid" style="gap:8px">${alerts.map((a) => `
    <div class="callout ${sevClass[a.severity]}" style="display:grid;gap:2px">
      <div class="row" style="gap:8px"><b>${esc(a.title)}</b><span class="spacer"></span>${a.link ? `<a href="${a.link}" class="small">ansehen →</a>` : ""}</div>
      <div class="small">${esc(a.detail)}</div>
      ${compact ? "" : `<div class="small"><b>Was tun:</b> ${esc(a.action)}</div>`}
    </div>`).join("")}</div>`;
}

function evalTable(ev) {
  return `<div class="row" style="margin:6px 0">${gatePill(ev.gate)} <span class="small muted">${ev.passed}/${ev.total} Kontrollfälle wie erwartet${ev.criticalFailed.length ? ` · <b class="conf-low">kritisch verletzt: ${esc(ev.criticalFailed.join(", "))}</b>` : ""}</span></div>
  <div class="table-wrap" style="max-height:340px"><table class="fixed"><thead><tr><th style="width:28%">Kontrollfall</th><th style="width:12%">kritisch</th><th>erwartet</th><th>tatsächlich</th><th style="width:8%"></th></tr></thead><tbody>
  ${ev.results.map((r) => `<tr><td class="mono small">${esc(r.scenario)}</td><td>${r.critical ? `<span class="pill bad">kritisch</span>` : ""}</td>
    <td class="small mono">${esc(Object.entries(r.expect).map(([k, v]) => `${k}=${v}`).join(" "))}</td>
    <td class="small mono">${esc(Object.entries(r.actual).filter(([, v]) => v).map(([k, v]) => `${k}=${v}`).join(" "))}</td>
    <td>${r.passed ? "✔" : "<b class='conf-low'>✘</b>"}</td></tr>`).join("")}</tbody></table></div>`;
}

async function renderGovernance(arg) {
  if (arg && GOV_TABS[arg]) govTab = arg;
  const [g, s] = await Promise.all([api("/api/governance"), api("/api/settings")]);
  const pending = g.changes.filter((c) => c.status === "PENDING").length;
  const tabLabel = (k) => k === "changes" && pending ? `${GOV_TABS[k]} <b class="badge" style="margin-left:4px">${pending}</b>` : k === "overview" && g.alerts.length ? `${GOV_TABS[k]} <b class="badge" style="margin-left:4px">${g.alerts.length}</b>` : GOV_TABS[k];
  const body = { overview: govOverview, rules: govRules, changes: govChanges, guardrails: govGuardrails, roles: govRoles, agents: govAgents, demo: govDemo }[govTab](g, s);
  $("#view").innerHTML = `<div class="tabs">${Object.keys(GOV_TABS).map((k) => `<button data-gtab="${k}" class="${k === govTab ? "active" : ""}">${tabLabel(k)}</button>`).join("")}</div>${body}`;
  $$("[data-gtab]").forEach((b) => b.onclick = () => { location.hash = `#/governance/${b.dataset.gtab}`; });
  ({ overview: bindOverview, rules: bindRules, changes: bindChanges, guardrails: bindGuardrails, roles: () => {}, agents: bindAgents, demo: bindDemo }[govTab])(g, s);
}

// ---- Alarme & Notbremse ----------------------------------------------------
function govOverview(g) {
  const ks = g.killSwitch || {};
  const desc = {
    intake: "Neue Mails werden zurückgehalten und nach dem Lösen verarbeitet.",
    agents: "Die KI wird nicht mehr gefragt. Fälle warten vor dem KI-Schritt.",
    execution: "Keine Buchungen ins ERP. Freigegebene Aufträge bleiben in der Queue.",
  };
  return `<div class="grid g2">
    <div class="card"><div class="card-h"><h2>Governance-Alarme</h2><span class="sub">Abschnitt 2.8</span></div><div class="card-b">${alertsHtml(g.alerts)}</div></div>
    <div class="card"><div class="card-h"><h2>Notbremse (Kill-Switch)</h2><span class="sub">Abschnitt 2.9 · hält an, verwirft nichts</span></div>
      <div class="card-b grid" style="gap:10px">
        ${Object.entries(g.killLabels).map(([k, label]) => `
          <div class="ks-row ${ks[k] ? "on" : ""}">
            <div><b>${esc(label)}</b> ${ks[k] ? `<span class="pill bad">GEZOGEN</span>` : `<span class="pill ok">läuft</span>`}
              <div class="small muted">${esc(desc[k])}</div>
              <div class="small">${k === "intake" ? `${g.held.intake} Mail(s) zurückgehalten` : k === "agents" ? `${g.held.agents} Fall/Fälle vor dem KI-Schritt` : `${g.held.execution} Auftrag/Aufträge in der Queue`}</div></div>
            <button class="btn ${ks[k] ? "ok solid" : "danger solid"}" data-ks="${k}" data-val="${ks[k] ? "0" : "1"}" ${canDo("killswitch") ? "" : "disabled"}>${ks[k] ? "Lösen" : "Ziehen"}</button>
          </div>`).join("")}
        ${permHint("killswitch")}
      </div></div>
  </div>`;
}
function bindOverview() {
  $$("[data-ks]").forEach((b) => b.onclick = async () => {
    try {
      const r = await api("/api/governance/killswitch", { method: "POST", body: { user: currentUser, flags: { [b.dataset.ks]: b.dataset.val === "1" } } });
      const n = Object.values(r.resumed).reduce((a, x) => a + x, 0);
      toast(b.dataset.val === "1" ? "Notbremse gezogen. Arbeit wird zurückgehalten." : `Notbremse gelöst${n ? `, ${n} zurückgehaltene Vorgänge fortgesetzt` : ""}.`, b.dataset.val === "1" ? "warn" : "ok");
      refreshBadge(); navigate();
    } catch (e) { toast(esc(e.message), "bad"); }
  });
}

// ---- Regeln (PDP) → Change Request ------------------------------------------
function govRules(g, s) {
  const pol = s.policy;
  const allowKey = Object.keys(pol.allowlist)[0];
  const allow = new Set(pol.allowlist[allowKey]);
  const num = (path, label, val, step = "0.01", hint = "") => `<label class="field">${label}<input data-num="${path}" type="number" step="${step}" value="${val}">${hint ? `<span class="small">${hint}</span>` : ""}</label>`;
  return `
  <div class="callout info">Regeln werden <b>nie direkt</b> geändert. „Änderung beantragen“ erzeugt einen ${kbTermLink("Change Request")}. Das System testet ihn sofort gegen das ${kbTermLink("Golden Set")}, und erst nach Genehmigung durch Compliance (nicht durch den Antragsteller) wird er aktiv.</div>
  <div class="card" style="margin-top:14px">
    <div class="card-h"><h2>[5] Policy Decision Point</h2><span class="sub">aktiv: Regeln <span class="mono">${esc(pol.rulesVersion)}</span></span></div>
    <div class="card-b grid" style="gap:16px">
      <div>
        <div class="section-title" style="margin-top:0">Schwellen (Platzhalter, siehe O-5)</div>
        <div class="form-grid">
          ${num("thresholds.duClassConfidence", "DU Klassenkonfidenz", pol.thresholds.duClassConfidence, "0.01", "darunter → Validation Station")}
          ${num("thresholds.duFieldConfidence", "DU Feldkonfidenz", pol.thresholds.duFieldConfidence, "0.01", "darunter → Validation Station")}
          ${num("thresholds.r1MinConfidence", "R1 min. Konfidenz", pol.thresholds.r1MinConfidence, "0.01", "darunter → R2")}
          ${num("thresholds.invoiceAutoLimit", "R1 Betragsgrenze (EUR)", pol.thresholds.invoiceAutoLimit, "100", "darüber → R2")}
          ${num("thresholds.anomalyFactor", "Anomalie-Faktor (× Ø)", pol.thresholds.anomalyFactor, "0.5", "darüber → R2")}
        </div>
      </div>
      <div>
        <div class="section-title">Stichproben &amp; Fristen</div>
        <div class="form-grid">
          ${num("sampling.R0", "Stichprobe R0 (Anteil)", pol.sampling.R0)}
          ${num("sampling.R1", "Stichprobe R1 (Anteil)", pol.sampling.R1)}
          ${num("slaHours.R1", "SLA R1 (Stunden)", pol.slaHours.R1, "1")}
          ${num("slaHours.R2", "SLA R2 (Stunden)", pol.slaHours.R2, "1")}
          ${num("slaHours.R3", "SLA R3 (Stunden)", pol.slaHours.R3, "1")}
        </div>
      </div>
      <div>
        <div class="section-title">Tool-Allowlist · <span class="mono">${esc(allowKey)}</span> (${kbTermLink("Least Privilege", "P5")})</div>
        <div class="allow-grid">${Object.entries(META.tools).map(([name, t]) => `<label><input type="checkbox" data-tool="${esc(name)}" ${allow.has(name) ? "checked" : ""}>
          <span><span class="mono">${esc(name)}</span> <span class="pill ${t.class === "T-READ" ? "info" : "warn"}">${t.class}</span><br><span class="small muted">${esc(t.desc)}</span></span></label>`).join("")}</div>
      </div>
      <label class="field">Begründung des Antrags<input id="cr-comment" placeholder="z. B. Betragsgrenze laut Compliance-Beschluss vom …"></label>
      <div class="row"><span class="spacer"></span>
        <button class="btn" id="btn-pol-reset" ${canDo("policy.propose") ? "" : "disabled"}>Standard als Antrag</button>
        <button class="btn primary" id="btn-propose" ${canDo("policy.propose") ? "" : "disabled"}>Änderung beantragen</button></div>
      ${permHint("policy.propose")}
    </div>
  </div>`;
}
function bindRules(g, s) {
  const pol = s.policy;
  const allowKey = Object.keys(pol.allowlist)[0];
  const done = (r) => { const cr = r.changeRequest; toast(`${esc(cr.id)} beantragt · Gate <b>${esc(cr.evaluation.gate)}</b>. Genehmigung durch Compliance nötig.`, cr.evaluation.gate === "BLOCKED" ? "bad" : "ok"); openCr = cr.id; location.hash = "#/governance/changes"; };
  $("#btn-propose").onclick = async () => {
    const policy = JSON.parse(JSON.stringify(pol));
    $$("[data-num]").forEach((i) => { const [a, b] = i.dataset.num.split("."); policy[a][b] = Number(i.value); });
    policy.allowlist[allowKey] = $$("[data-tool]").filter((c) => c.checked).map((c) => c.dataset.tool);
    try { done(await api("/api/settings", { method: "PUT", body: { user: currentUser, policy, comment: $("#cr-comment").value } })); }
    catch (e) { toast(esc(e.message), "bad"); }
  };
  $("#btn-pol-reset").onclick = async () => {
    try { done(await api("/api/settings/policy/reset", { method: "POST", body: { user: currentUser } })); } catch (e) { toast(esc(e.message), "bad"); }
  };
}

// ---- Änderungsanträge ---------------------------------------------------------
function govChanges(g) {
  if (!g.changes.length) return `<div class="card"><div class="empty">Noch keine Änderungsanträge.<br><span class="small">Unter <a href="#/governance/rules">Regeln</a> oder <a href="#/governance/guardrails">Guardrails</a> eine Änderung beantragen.</span></div></div>`;
  const fmtVal = (v) => v === undefined || v === null ? "–" : typeof v === "object" ? JSON.stringify(v) : String(v);
  return `<div class="grid" style="gap:12px">${g.changes.map((c) => `
    <details class="card cr" ${openCr === c.id || (openCr === null && c.status === "PENDING") ? "open" : ""} data-cr="${esc(c.id)}">
      <summary class="card-h" style="cursor:pointer"><b class="mono">${esc(c.id)}</b> ${crStatus(c.status)} ${gatePill(c.evaluation.gate)}
        <span class="sub">von ${esc(userName(c.proposed_by))} · ${fmtDateTime(c.ts)} · ${c.diff.length} Änderung(en)</span>
        <span class="right small muted">${c.decided_by ? `${c.status === "APPLIED" ? "genehmigt" : "abgelehnt"} von ${esc(userName(c.decided_by))}` : ""}</span></summary>
      <div class="card-b grid" style="gap:12px">
        ${c.comment ? `<div><b>Begründung:</b> ${esc(c.comment)}</div>` : ""}
        <div><div class="section-title" style="margin-top:0">Was ändert sich (Diff)</div>
          <table class="fixed"><thead><tr><th style="width:34%">Einstellung</th><th>bisher</th><th>neu</th></tr></thead><tbody>
          ${c.diff.map((d) => `<tr><td class="mono small">${esc(d.path)}${d.auto ? ` <span class="pill info">automatisch</span>` : ""}</td>
            <td class="small mono">${d.removed ? (d.removed.length ? "entfernt: " + esc(d.removed.join(", ")) : "–") : esc(fmtVal(d.old))}</td>
            <td class="small mono">${d.added ? (d.added.length ? "hinzu: " + esc(d.added.join(", ")) : "–") : esc(fmtVal(d.new))}</td></tr>`).join("")}</tbody></table></div>
        <div><div class="section-title">Automatische Wirkungsanalyse (Golden Set in Sandbox)</div>
          <div class="small muted" style="margin-bottom:4px">PASSED = Verhalten unverändert · REVIEW = Verhalten ändert sich, bewusst prüfen · BLOCKED = Sicherheitsfall verletzt, Genehmigung unmöglich</div>
          ${evalTable(c.evaluation)}</div>
        ${c.status === "PENDING" ? `
        <div class="row">
          <input data-cr-comment="${esc(c.id)}" placeholder="Kommentar zur Entscheidung" style="flex:1">
          <button class="btn danger" data-cr-act="reject" data-id="${esc(c.id)}">${currentUser === c.proposed_by ? "Zurückziehen" : "Ablehnen"}</button>
          <button class="btn ok solid" data-cr-act="approve" data-id="${esc(c.id)}" ${canDo("policy.approve") && currentUser !== c.proposed_by && c.evaluation.gate !== "BLOCKED" ? "" : "disabled"}>Genehmigen &amp; aktivieren</button>
        </div>
        ${currentUser === c.proposed_by ? `<div class="small muted">Funktionstrennung: Eigene Anträge können nicht selbst genehmigt werden.</div>` : permHint("policy.approve")}
        ${c.evaluation.gate === "BLOCKED" ? `<div class="callout bad small">Release-Gate blockiert: Ein kritischer Kontrollfall würde nicht mehr wie gefordert behandelt. Antrag ablehnen und korrigieren.</div>` : ""}` :
        c.decision_comment ? `<div class="small"><b>Kommentar:</b> ${esc(c.decision_comment)}</div>` : ""}
      </div>
    </details>`).join("")}
  </div>
  ${g.history.length ? `<div class="card" style="margin-top:16px"><div class="card-h"><h3>Aktivierungshistorie</h3><span class="sub">jede Aktivierung mit CR-Nummer, Person und Zeit</span></div>
    <table><thead><tr><th>Zeit</th><th>Bereich</th><th>Change Request</th><th>Aktiviert von</th></tr></thead><tbody>
    ${g.history.map((h) => `<tr><td class="small">${fmtDateTime(h.ts)}</td><td class="mono">${esc(h.key)}</td><td class="mono">${esc(h.change_id)}</td><td>${esc(userName(h.applied_by))}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
}
function bindChanges() {
  $$("details.cr").forEach((d) => d.ontoggle = () => { if (d.open) openCr = d.dataset.cr; });
  $$("[data-cr-act]").forEach((b) => b.onclick = async () => {
    const id = b.dataset.id;
    try {
      const r = await api(`/api/governance/changes/${id}/${b.dataset.crAct}`, { method: "POST", body: { user: currentUser, comment: $(`[data-cr-comment="${id}"]`)?.value || "" } });
      toast(r.status === "APPLIED" ? `${esc(id)} genehmigt. Neue Konfiguration ist aktiv.` : `${esc(id)} abgelehnt.`, r.status === "APPLIED" ? "ok" : "warn");
      openCr = id; navigate();
    } catch (e) { toast(esc(e.message), "bad"); }
  });
}

// ---- Guardrails --------------------------------------------------------------
function govGuardrails(g, s) {
  return `
  <div class="callout info">Mehrere unabhängige Schutzschichten (${kbTermLink("Guardrail", "Guardrails")}). Schaltbare Guardrails ändern sich nur per Änderungsantrag. „Treffer“ zählt, wie oft die Schicht bisher eingegriffen oder gearbeitet hat.</div>
  <div class="card" style="margin-top:14px">
    <div class="table-wrap" style="max-height:none"><table>
      <thead><tr><th>ID</th><th>Schicht</th><th>Guardrail</th><th>In Klartext</th><th>Bei Treffer</th><th class="num">Treffer</th><th>Status</th></tr></thead>
      <tbody>${g.guardrails.map((x) => `<tr>
        <td class="mono"><b>${esc(x.id)}</b></td><td class="small">${esc(x.layer)}<br><span class="muted">${esc(x.where)}</span></td>
        <td>${esc(x.name)}</td><td class="small">${esc(x.plain)}</td><td class="small">${esc(x.onHit)}</td>
        <td class="num mono">${x.hits}</td>
        <td>${x.setting ? `<label class="row small" style="gap:6px;flex-wrap:nowrap"><input type="checkbox" data-guard="${esc(x.setting)}" ${x.enabled ? "checked" : ""}> ${x.enabled ? "an" : "<b class='conf-low'>aus</b>"}</label>` : `<span class="pill ok">${esc(x.status)}</span>`}</td>
      </tr>`).join("")}</tbody></table></div>
    <div class="card-b row">
      <input id="gr-comment" placeholder="Begründung (Pflicht bei Abschaltung)" style="flex:1">
      <button class="btn primary" id="btn-gr" ${canDo("policy.propose") ? "" : "disabled"}>Guardrail-Änderung beantragen</button>
    </div>
    <div class="card-b" style="padding-top:0">${permHint("policy.propose")}</div>
  </div>`;
}
function bindGuardrails(g, s) {
  $("#btn-gr").onclick = async () => {
    const guardrails = { ...s.guardrails }, automationOps = { ...s.automationOps };
    $$("[data-guard]").forEach((c) => { if (c.dataset.guard === "storePromptIO") automationOps.storePromptIO = c.checked; else guardrails[c.dataset.guard] = c.checked; });
    try {
      const r = await api("/api/settings", { method: "PUT", body: { user: currentUser, guardrails, automationOps, comment: $("#gr-comment").value } });
      toast(`${esc(r.changeRequest.id)} beantragt · Gate ${esc(r.changeRequest.evaluation.gate)}`, "ok"); openCr = r.changeRequest.id; location.hash = "#/governance/changes";
    } catch (e) { toast(esc(e.message), "bad"); }
  };
}

// ---- Rollen & Rechte ---------------------------------------------------------
function govRoles(g) {
  const roles = Object.keys(g.roles);
  const mine = userRole();
  return `
  <div class="callout info">Rechte hängen an Rollen (${kbTermLink("RBAC")}). Das System erzwingt die ${kbTermLink("Funktionstrennung")}: Wer beantragt, genehmigt nicht. Wer Regeln genehmigt, gibt keine Zahlungen frei. Du arbeitest gerade als <b>${esc(userName(currentUser))}</b> (${esc(roleName(mine))}).</div>
  <div class="grid g2" style="margin-top:14px">
    <div class="card"><div class="card-h"><h3>Rollen und Personen</h3></div>
      <table><thead><tr><th>Rolle</th><th>Personen</th><th>Aufgabe</th></tr></thead><tbody>
      ${roles.map((r) => `<tr class="${r === mine ? "row-hl" : ""}"><td><b>${esc(g.roles[r].name)}</b></td><td class="small">${g.users.filter((u) => u.role === r).map((u) => esc(u.name)).join(", ")}</td><td class="small">${esc(g.roles[r].desc)}</td></tr>`).join("")}
      </tbody></table></div>
    <div class="card"><div class="card-h"><h3>Rechte-Matrix</h3><span class="sub">✔ = erlaubt</span></div>
      <table><thead><tr><th>Recht</th>${roles.map((r) => `<th class="${r === mine ? "col-hl" : ""}" style="text-align:center">${esc(g.roles[r].name.split(" ")[0])}</th>`).join("")}</tr></thead><tbody>
      ${Object.entries(g.permissions).map(([, p]) => `<tr><td class="small">${esc(p.label)}</td>${roles.map((r) => `<td style="text-align:center" class="${r === mine ? "col-hl" : ""}">${p.roles.includes(r) ? "✔" : "<span class='muted'>–</span>"}</td>`).join("")}</tr>`).join("")}
      </tbody></table></div>
  </div>
  <div class="card" style="margin-top:16px"><div class="card-h"><h3>Zuständigkeiten (${kbTermLink("RACI")})</h3><span class="sub">R = führt aus · A = verantwortlich · C = wird gefragt · I = wird informiert</span></div>
    <div class="table-wrap" style="max-height:none"><table><thead><tr><th>Tätigkeit</th>${g.raci.roles.map((r) => `<th style="text-align:center">${esc(r)}</th>`).join("")}</tr></thead><tbody>
    ${g.raci.rows.map(([t, v]) => `<tr><td>${esc(t)}</td>${v.map((x) => `<td style="text-align:center"><span class="raci raci-${x}">${x}</span></td>`).join("")}</tr>`).join("")}
    </tbody></table></div></div>`;
}

// ---- Agenten & Evaluation ----------------------------------------------------
function govAgents(g, s) {
  const [id, a] = Object.entries(g.agents)[0];
  const key = `${id}@${a.version}`;
  const allow = s.policy.allowlist[key] || [];
  const ev = g.agentEval;
  return `
  <div class="card">
    <div class="card-h"><h2 class="mono">${esc(key)}</h2><span class="pill ok">${esc(a.lifecycle)}</span><span class="sub">${esc(a.kind)}</span></div>
    <div class="card-b grid" style="gap:14px">
      <div class="lifecycle">${g.lifecycle.map((st, i) => `${i ? `<span class="lc-sep">→</span>` : ""}<span class="lc-step ${st === a.lifecycle ? "current" : g.lifecycle.indexOf(a.lifecycle) > i ? "done" : ""}">${st}</span>`).join("")}</div>
      <dl class="kv">
        <dt>Zweck</dt><dd>${esc(a.description)}</dd>
        <dt>Fachlich verantwortlich</dt><dd>${esc(a.owner)}</dd>
        <dt>Betrieb</dt><dd>${esc(a.operator)}</dd>
        <dt>Risikoklasse</dt><dd>${esc(a.riskClass)}</dd>
        <dt>Sprachmodell</dt><dd>${esc(a.llm)}</dd>
        <dt>Freigegeben</dt><dd>${esc(a.approvedBy)} am ${esc(a.approvedAt)}</dd>
        <dt>Erlaubte Werkzeuge</dt><dd>${allow.map((t) => `<span class="pill ${META.tools[t]?.class === "T-READ" ? "info" : "warn"}">${esc(t)}</span>`).join(" ")}</dd>
      </dl>
      <div class="callout info small">Neuer Prompt, neues Modell, neues Werkzeug oder neuer Wissensindex = <b>neue Version</b>. Rechte gelten nur für genau diese Version und werden nicht vererbt (Abschnitt 2.5).</div>
    </div>
  </div>
  <div class="card" style="margin-top:16px">
    <div class="card-h"><h3>Evaluation gegen das ${kbTermLink("Golden Set")}</h3><span class="sub">${g.golden.length} Kontrollfälle, davon ${g.golden.filter((x) => x.critical).length} kritisch</span>
      <div class="right"><button class="btn primary sm" id="btn-eval" ${canDo("agent.evaluate") ? "" : "disabled"}>Evaluation starten</button></div></div>
    <div class="card-b">
      ${ev ? `<div class="small muted">Letzter Lauf: ${fmtDateTime(ev.ts)} durch ${esc(userName(ev.user))}</div>${evalTable(ev.result)}` : `<div class="muted">Noch keine Evaluation gelaufen.</div>`}
      ${permHint("agent.evaluate")}
    </div>
  </div>`;
}
function bindAgents() {
  $("#btn-eval").onclick = async () => {
    try { const r = await api("/api/governance/evaluate", { method: "POST", body: { user: currentUser } }); toast(`Evaluation: ${r.result.passed}/${r.result.total} · Gate ${esc(r.result.gate)}`, r.result.gate === "PASSED" ? "ok" : "warn"); navigate(); }
    catch (e) { toast(esc(e.message), "bad"); }
  };
}

// ---- Demo-Steuerung ----------------------------------------------------------
function govDemo(g, s) {
  const chaosLabels = {
    llmTimeoutRate: ["LLM-Timeout", "Agent-Wrapper, max. 2 Retries"], llm429Rate: ["LLM 429", "Agent-Wrapper, max. 2 Retries"],
    llmSchemaErrorRate: ["Schema-Fehler", "1 Repair-Prompt, dann ESCALATED"], erp5xxRate: ["ERP 5xx", "Robot, max. 3 Retries mit Idempotency-Key"],
    erpTimeoutAfterCommitRate: ["Timeout nach Commit", "Retry trifft Idempotenz, keine Doppelbuchung"], selectorDriftRate: ["Selektor-Drift (UI)", "Healing Agent, danach REFramework-Retry"],
  };
  return `
  <div class="callout warn">Nur für die Vorführung: Diese Regler simulieren Störungen und wirken sofort, ohne Änderungsantrag. In einer echten Umgebung gibt es sie nicht.</div>
  <div class="card" style="margin-top:14px">
    <div class="card-h"><h2>Fault Injection</h2><span class="sub">prüft die Retry-Zuständigkeiten aus 1.9</span></div>
    <div class="card-b">
      ${Object.entries(chaosLabels).map(([k, [label, hint]]) => `<div class="slider-row"><span>${label}<br><span class="small muted">${hint}</span></span>
        <input type="range" min="0" max="1" step="0.05" data-chaos="${k}" value="${s.chaos[k] || 0}"><span class="mono" data-chaos-v="${k}">${Math.round((s.chaos[k] || 0) * 100)}%</span></div>`).join("")}
      <label class="toggle-row"><input type="checkbox" id="demo-auto" ${s.robot.autoRun ? "checked" : ""}> Robot führt freigegebene Aufträge automatisch aus</label>
      <div class="row"><span class="spacer"></span><button class="btn primary" id="btn-demo">Übernehmen</button></div>
    </div>
  </div>`;
}
function bindDemo() {
  $$("[data-chaos]").forEach((r) => r.oninput = () => $(`[data-chaos-v="${r.dataset.chaos}"]`).textContent = Math.round(r.value * 100) + "%");
  $("#btn-demo").onclick = async () => {
    const chaos = Object.fromEntries($$("[data-chaos]").map((r) => [r.dataset.chaos, Number(r.value)]));
    try { await api("/api/settings", { method: "PUT", body: { chaos, robot: { autoRun: $("#demo-auto").checked } } }); toast("Demo-Steuerung übernommen", "ok"); }
    catch (e) { toast(esc(e.message), "bad"); }
  };
}

// =====================================================================
// Spickzettel
// =====================================================================
function renderWissen(tab, anchor) {
  const host = $("#view");
  host.innerHTML = "";
  window.renderKnowledge(host, { tab: tab || "start", anchor });
}

// =====================================================================
// Integration (SDD Abschnitt 3)
// =====================================================================
let intTab = "bridge";
let lastBridgeKey = null;
let lastBridgeBody = null;

async function hmacHex(secret, text) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const SAMPLE_BRIDGE_BODY = JSON.stringify({ type: "supplier.message", data: {
  from: "billing@nordlicht-logistik.example", subject: "Partner-API: Rechnung NL-90001",
  body: "Rechnung über die Partner-Schnittstelle.",
  attachments: [{ filename: "NL-90001.pdf", quality: 1,
    content: "RECHNUNG\nLieferantennummer: 100417\nRechnungsnummer: NL-90001\nBestellnummer: 4500020001\nGesamtbetrag: 2.400,00 EUR" }] } }, null, 2);

async function renderIntegration(arg) {
  if (arg && ["bridge", "contracts", "log"].includes(arg)) intTab = arg;
  const info = await api("/api/bridge");
  const tabs = { bridge: "Bridge-Testkonsole", contracts: "Tool-Verträge", log: `Bridge-Protokoll (${info.accepted.length + info.rejected.length})` };
  let body = "";
  if (intTab === "bridge") {
    body = `
    <div class="callout info">Externe Agenten und Partner schicken Nachrichten an <code>POST ${esc(info.endpoint)}</code>. Die Plattform prüft <b>Zeitfenster (±${info.replayWindowS} s)</b>, <b>HMAC-Signatur</b> und <b>Idempotency-Key</b>, bevor irgendetwas passiert. Danach läuft die Nachricht durch denselben Weg wie eine Mail.</div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><div class="card-h"><h3>Nachricht</h3><span class="sub">Body (JSON)</span></div>
        <div class="card-b grid" style="gap:10px">
          <textarea id="br-body" rows="14">${esc(lastBridgeBody || SAMPLE_BRIDGE_BODY)}</textarea>
          <label class="field">Gemeinsames Secret (Demo, im Betrieb im Credential Store)<input id="br-secret" class="mono" value="${esc(info.secret)}" readonly></label>
          <div class="row">
            <button class="btn primary" data-br="valid">✔ Gültig senden</button>
            <button class="btn" data-br="replay" ${lastBridgeKey ? "" : "disabled"}>↻ Gleiche Nachricht erneut</button>
            <button class="btn danger" data-br="badsig">✘ Falsche Signatur</button>
            <button class="btn danger" data-br="old">✘ Alter Zeitstempel</button>
            <button class="btn danger" data-br="conflict" ${lastBridgeKey ? "" : "disabled"}>✘ Gleicher Key, anderer Inhalt</button>
          </div>
          <div class="row"><span class="spacer"></span><button class="btn sm ghost" id="br-rotate">Secret rotieren</button></div>
        </div></div>
      <div class="card"><div class="card-h"><h3>Gesendet und Antwort</h3></div>
        <div class="card-b" id="br-out"><div class="muted">Noch nichts gesendet. Links eine Variante wählen.</div></div></div>
    </div>`;
  } else if (intTab === "contracts") {
    body = `<div class="callout info">Jeder KI-Vorschlag wird gegen diese Verträge geprüft (Regel ${kbTermLink("Tool-Vertrag", "R-007")}). Unbekannte Felder oder falsche Typen führen zur Eskalation statt zur Ausführung.</div>
    <div class="card" style="margin-top:14px"><div class="table-wrap" style="max-height:none"><table>
      <thead><tr><th>Werkzeug</th><th>Klasse</th><th>Version</th><th>Felder</th><th>Idempotent</th><th>Zeitlimit</th><th>Liefert</th></tr></thead>
      <tbody>${Object.entries(META.toolContracts || {}).map(([name, c]) => `<tr>
        <td class="mono"><b>${esc(name)}</b><div class="small muted">${esc(META.tools[name]?.desc || "")}</div></td>
        <td><span class="pill ${META.tools[name]?.class === "T-READ" ? "info" : "warn"}">${esc(META.tools[name]?.class || "")}</span></td>
        <td class="mono">${esc(c.version)}</td>
        <td class="small mono">${Object.entries(c.args).map(([f, t]) => `${esc(f)}: ${esc(t)}`).join("<br>")}</td>
        <td>${c.idempotent ? "ja" : "nein"}</td><td class="num">${c.timeoutS} s</td>
        <td class="small">${esc(c.returns || "")}${c.errors ? `<div class="muted">Fachfehler: ${esc(c.errors.join(", "))}</div>` : ""}</td>
      </tr>`).join("")}</tbody></table></div></div>`;
  } else {
    body = `<div class="grid g2">
      <div class="card"><div class="card-h"><h3>Angenommen (${info.accepted.length})</h3></div>
        <table><thead><tr><th>Zeit</th><th>Idempotency-Key</th><th>HTTP</th><th>Ergebnis</th></tr></thead><tbody>
        ${info.accepted.map((r) => `<tr><td class="small">${fmtDateTime(r.ts)}</td><td class="mono small">${esc(r.idem_key)}</td><td>${r.status_code}</td><td class="small">${r.response.caseId ? `<a class="mono" href="#/cases/${esc(r.response.caseId)}">${esc(r.response.caseId)}</a> ` : ""}${esc(r.response.state || r.response.status || "")}</td></tr>`).join("") || `<tr><td colspan="4" class="empty">Noch keine</td></tr>`}
        </tbody></table></div>
      <div class="card"><div class="card-h"><h3>Abgewiesen (${info.rejected.length})</h3></div>
        <table><thead><tr><th>Zeit</th><th>Grund</th></tr></thead><tbody>
        ${info.rejected.map((r) => `<tr><td class="small">${fmtDateTime(r.ts)}</td><td class="small">${esc(r.message)}</td></tr>`).join("") || `<tr><td colspan="2" class="empty">Noch keine</td></tr>`}
        </tbody></table></div></div>`;
  }
  $("#view").innerHTML = `<div class="tabs">${Object.entries(tabs).map(([k, l]) => `<button data-itab="${k}" class="${k === intTab ? "active" : ""}">${esc(l)}</button>`).join("")}</div>${body}`;
  $$("[data-itab]").forEach((b) => b.onclick = () => { location.hash = `#/integration/${b.dataset.itab}`; });
  if (intTab !== "bridge") return;

  $("#br-rotate").onclick = async () => {
    try { await api("/api/bridge/rotate", { method: "POST", body: { user: currentUser } }); toast("Secret rotiert. Alte Signaturen sind ab sofort ungültig.", "ok"); navigate(); }
    catch (e) { toast(esc(e.message), "bad"); }
  };
  $$("[data-br]").forEach((b) => b.onclick = async () => {
    const mode = b.dataset.br;
    let raw = $("#br-body").value;
    try { JSON.parse(raw); } catch { toast("Body ist kein gültiges JSON", "bad"); return; }
    const key = (mode === "replay" || mode === "conflict") ? lastBridgeKey : "partner-" + Math.random().toString(16).slice(2, 10);
    if (mode === "replay") raw = lastBridgeBody;
    if (mode === "conflict") { const o = JSON.parse(lastBridgeBody); o.data.subject = (o.data.subject || "") + " (geändert)"; raw = JSON.stringify(o, null, 2); }
    let ts = Math.floor(Date.now() / 1000);
    if (mode === "old") ts -= 600;
    let sig = "sha256=" + await hmacHex(info.secret, `${ts}.${raw}`);
    if (mode === "badsig") sig = "sha256=" + await hmacHex("falsches-secret", `${ts}.${raw}`);
    const hex = (n) => [...crypto.getRandomValues(new Uint8Array(n))].map((x) => x.toString(16).padStart(2, "0")).join("");
    const headers = { "Content-Type": "application/json", "X-AEAP-Timestamp": String(ts), "X-AEAP-Signature": sig,
      "Idempotency-Key": key, traceparent: `00-${hex(16)}-${hex(8)}-01` };
    const res = await fetch(info.endpoint, { method: "POST", headers, body: raw });
    const data = await res.json().catch(() => ({}));
    if (mode === "valid") { lastBridgeKey = key; lastBridgeBody = raw; }
    const replayed = res.headers.get("Idempotent-Replay") === "true";
    const expl = {
      valid: "Signatur und Zeitfenster stimmen: Nachricht angenommen und als Case verarbeitet.",
      replay: "Gleicher Key und gleicher Inhalt: gespeicherte Antwort, kein zweiter Case.",
      badsig: "Signatur passt nicht zum Secret: abgewiesen, bevor irgendetwas verarbeitet wird.",
      old: "Zeitstempel 10 Minuten alt: außerhalb des Replay-Fensters, abgewiesen.",
      conflict: "Gleicher Key mit anderem Inhalt: Konflikt (409), nichts verarbeitet.",
    }[mode];
    $("#br-out").innerHTML = `
      <div class="callout ${res.status < 300 ? "ok" : "bad"}"><b>HTTP ${res.status}</b>${replayed ? " · Idempotent-Replay" : ""} · ${esc(expl)}</div>
      ${data.caseId ? `<div style="margin-top:8px">Case: <a class="mono" href="#/cases/${esc(data.caseId)}">${esc(data.caseId)}</a> ${statePill(data.state || data.status)}</div>` : ""}
      <div class="section-title">Gesendete Header</div><pre class="json" style="border-radius:8px">${jsonHtml(headers)}</pre>
      <div class="section-title">Antwort</div><pre class="json" style="border-radius:8px">${jsonHtml(data)}</pre>
      <div class="small muted">Signatur = HMAC-SHA256(Secret, "${ts}." + Body)</div>`;
    $$("[data-br='replay'], [data-br='conflict']").forEach((x) => { x.disabled = !lastBridgeKey; });
    refreshBadge();
  });
}

// =====================================================================
// Audit
// =====================================================================
let auditFilter = { q: "", kind: "" };

async function renderAudit() {
  const isCorr = /^[0-9a-f-]{36}$/i.test(auditFilter.q);
  const isCase = /^AEAP-/i.test(auditFilter.q);
  const qs = isCorr ? `correlationId=${encodeURIComponent(auditFilter.q)}` : isCase ? `caseId=${encodeURIComponent(auditFilter.q.toUpperCase())}` : "";
  const ev = await api(`/api/audit?limit=500&${qs}`);
  const kinds = [...new Set(ev.map((e) => e.kind))].sort();
  const rows = ev.filter((e) => (!auditFilter.kind || e.kind === auditFilter.kind) &&
    (isCorr || isCase || !auditFilter.q || (e.message || "").toLowerCase().includes(auditFilter.q.toLowerCase())));
  const html = `<div class="card">
    <div class="card-h"><h2>Unified Audit (${rows.length})</h2><span class="sub">Export ins SIEM über Audit-Pack bzw. API <span class="mono">/api/audit</span></span>
      <div class="right">
        <input id="a-q" placeholder="caseId, correlationId oder Text" value="${esc(auditFilter.q)}" style="width:280px">
        <select id="a-kind"><option value="">Alle Arten</option>${[...new Set([...kinds, auditFilter.kind].filter(Boolean))].map((k) => `<option ${k === auditFilter.kind ? "selected" : ""}>${esc(k)}</option>`).join("")}</select>
      </div></div>
    <ul class="timeline">${rows.map((e) => eventRow(e)).join("") || `<li class="empty">Keine Ereignisse</li>`}</ul>
  </div>`;
  if (paint(JSON.stringify([rows.map((e) => e.id), auditFilter]), html)) {
    const q = $("#a-q");
    q.oninput = () => { auditFilter.q = q.value.trim(); clearTimeout(q._t); q._t = setTimeout(async () => { await navigate(); const n = $("#a-q"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }, 300); };
    $("#a-kind").onchange = (e) => { auditFilter.kind = e.target.value; navigate(); };
  }
}

// =====================================================================
// Boot
// =====================================================================
async function boot() {
  const meta = await api("/api/meta");
  Object.assign(META, meta);
  const sel = $("#user-select");
  sel.innerHTML = META.users.map((u) => `<option value="${esc(u.id)}" ${u.id === currentUser ? "selected" : ""}>${esc(u.name)} · ${esc(META.roles[u.role]?.name || u.role)}</option>`).join("");
  sel.onchange = () => { currentUser = sel.value; localSet("aeap.user", currentUser); if (["action", "governance"].includes(route.view)) navigate(); };
  $("#btn-reset").onclick = async () => {
    if (!confirm("Alle Cases, Tasks, Queues, ERP-Daten und Einstellungen zurücksetzen?")) return;
    await api("/api/reset", { method: "POST" }); toast("Zurückgesetzt", "ok"); refreshBadge(); navigate();
  };
  window.addEventListener("hashchange", () => navigate());
  if (!location.hash) location.hash = "#/overview";
  await navigate();
  refreshBadge();
  setInterval(() => {
    refreshBadge();
    const v = VIEWS[route.view];
    const typing = document.activeElement && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
    const openDetails = $$("#view details[open]").length > 0;
    if (v.refresh && !typing && !openDetails) navigate(false);
  }, 3000);
}

boot().catch((e) => { $("#view").innerHTML = `<div class="callout bad">Server nicht erreichbar: ${esc(e.message)}</div>`; });
