// DataVigil dashboard client
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const state = { source: null, dataset: null, lastRun: null };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (isErr ? " err" : "");
  setTimeout(() => t.classList.add("hidden"), 3800);
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const scoreColor = (v) => v >= 90 ? "var(--good)" : v >= 75 ? "var(--warning)"
  : v >= 50 ? "var(--serious)" : "var(--critical)";
const sevPill = (s) => ({ info: "pill-info", warning: "pill-warn",
  error: "pill-crit", critical: "pill-crit" }[s] || "pill-info");

/* ---------- theme ---------- */
$("#theme-toggle").onclick = () => {
  const cur = document.documentElement.getAttribute("data-theme");
  document.documentElement.setAttribute("data-theme", cur === "dark" ? "light" : "dark");
  if (state.lastRun) renderScore(state.lastRun);
};

/* ---------- tabs ---------- */
$$(".tab").forEach(t => t.onclick = () => {
  $$(".tab").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  $$(".view").forEach(v => v.classList.add("hidden"));
  $("#empty-state").classList.add("hidden");
  $("#view-" + t.dataset.view).classList.remove("hidden");
  if (t.dataset.view === "report") loadHistory();
});

/* ---------- sources ---------- */
function shortName(d) {   // dataset keys may be full paths — show basename
  return String(d).split(/[\\/]/).pop() || d;
}
function selectDataset(d) {
  if (state.dataset !== d) {
    // stale views from the previous dataset must not linger
    $("#gauge").innerHTML = ""; $("#dim-scores").innerHTML = "";
    $("#run-results").innerHTML = "";
  }
  state.dataset = d;
  $("#dataset-select").value = d;
  $("#empty-state").classList.add("hidden");
  const active = $(".tab.active").dataset.view;
  $("#view-" + active).classList.remove("hidden");
  $("#prof-dataset").textContent = "· " + shortName(d);
  renderGateInfo();
  if ($(".tab.active")?.dataset.view === "report") loadHistory();
  loadRules();
}
async function refreshSources() {
  const sources = await api("/api/sources");
  const sel = $("#source-select");
  sel.innerHTML = sources.map(s => `<option value="${esc(s.name)}">${esc(s.name)} (${esc(s.type)})</option>`).join("");
  if (state.source && sources.some(s => s.name === state.source))
    sel.value = state.source;
  else if (sources.length) sel.value = sources[0].name;
  state.source = sel.value || null;
  if (state.source) await refreshDatasets();
  else { state.dataset = null; $("#dataset-select").innerHTML = ""; }
}
async function refreshDatasets() {
  if (!state.source) return;
  const { datasets } = await api(`/api/sources/${encodeURIComponent(state.source)}/datasets`);
  const sel = $("#dataset-select");
  sel.innerHTML = datasets.map(d =>
    `<option value="${esc(d)}" title="${esc(d)}">${esc(shortName(d))}</option>`).join("");
  sel.size = Math.min(Math.max(datasets.length, 3), 10);
  if (!datasets.includes(state.dataset)) state.dataset = null;
  if (state.dataset) sel.value = state.dataset;
  else if (datasets.length) selectDataset(datasets[0]);   // auto-activate first
}
$("#source-select").onchange = async (e) => { state.source = e.target.value; await refreshDatasets(); };
$("#dataset-select").onchange = (e) => selectDataset(e.target.value);

$("#src-remove").onclick = async () => {
  const name = $("#source-select").value;
  if (!name) return toast("Select a source to remove", true);
  if (!confirm(`Remove source “${name}” and all its datasets?`)) return;
  try {
    await api(`/api/sources/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (state.source === name) { state.source = null; state.dataset = null;
      $("#dataset-select").innerHTML = ""; }
    await refreshSources(); await fillMapSources();
    toast(`Removed source “${name}”`);
  } catch (err) { toast("Remove failed: " + err.message, true); }
};

$("#file-input").onchange = async (e) => {
  const file = e.target.files[0]; if (!file) return;
  e.target.value = "";  // reset so the same file can be re-selected later
  const fd = new FormData(); fd.append("file", file);
  const opts = $("#file-options").value.trim();
  if (opts) fd.append("options", opts);
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    const info = await r.json();
    if (!r.ok) throw new Error(info.detail || r.statusText);
    toast(`Uploaded ${file.name} → source “${info.name}”`);
    state.source = info.name; await refreshSources();
    await fillMapSources();
  } catch (err) { toast("Upload failed: " + err.message, true); }
};

// Aurora IAM toggle: show region field + warn that password is generated
$("#db-iam").onchange = (e) => {
  $("#db-region").classList.toggle("hidden", !e.target.checked);
  $("#db-pass").disabled = e.target.checked;
  if (e.target.checked) $("#db-pass").value = "";
};
// show the region field when IAM auth is on OR Aurora is picked
$("#db-dialect").onchange = () => {
  const aurora = $("#db-dialect").value.startsWith("aurora");
  $("#db-region").classList.toggle("hidden", !(aurora || $("#db-iam").checked));
};

$("#fs-connect").onclick = async () => {
  const name = $("#fs-name").value.trim();
  const path = $("#fs-path").value.trim();
  if (!name || !path) return toast("Give a name and a file path", true);
  const params = { path };
  const opts = $("#fs-options").value.trim();
  if (opts) { try { Object.assign(params, JSON.parse(opts)); }
              catch { return toast("Reader options is not valid JSON", true); } }
  const cpy = $("#fs-copybook").value.trim();
  const type = cpy ? "copybook" : "file";
  if (cpy) params.copybook = cpy;
  try {
    const info = await api("/api/sources", { method: "POST",
      body: JSON.stringify({ name, type, params }) });
    toast(`Registered “${name}” (${type}) — ${info.datasets.length} dataset(s)`);
    state.source = name; await refreshSources();
    await fillMapSources();
  } catch (err) { toast("Register failed: " + err.message, true); }
};

$("#db-connect").onclick = async () => {
  const name = $("#db-name").value.trim();
  if (!name) return toast("Give the source a name", true);
  const params = {};
  const url = $("#db-url").value.trim();
  if (url) { params.url = url; }
  else {
    params.dialect = $("#db-dialect").value;
    for (const [k, id] of [["host", "db-host"], ["user", "db-user"],
      ["password", "db-pass"], ["database", "db-database"], ["schema", "db-schema"],
      ["region", "db-region"]]) {
      const v = $("#" + id).value.trim(); if (v) params[k] = v;
    }
    const port = $("#db-port").value.trim(); if (port) params.port = +port;
    if ($("#db-iam").checked) params.iam_auth = true;
  }
  try {
    const info = await api("/api/sources", { method: "POST",
      body: JSON.stringify({ name, type: "database", params }) });
    toast(`Connected “${name}” — ${info.datasets.length} datasets`);
    state.source = name; await refreshSources();
    await fillMapSources();
  } catch (err) { toast("Connect failed: " + err.message, true); }
};

// pgwire needs an endpoint; Data API does not
$("#mcp-method").onchange = (e) => {
  $("#mcp-endpoint").classList.toggle("hidden", e.target.value !== "pgwire");
};

$("#mcp-connect").onclick = async () => {
  const name = $("#mcp-name").value.trim();
  if (!name) return toast("Give the source a name", true);
  const params = { method: $("#mcp-method").value };
  for (const [k, id] of [["cluster", "mcp-cluster-arn"],
    ["secret_arn", "mcp-secret-arn"], ["database", "mcp-database"],
    ["region", "mcp-region"], ["schema", "mcp-schema"],
    ["db_endpoint", "mcp-endpoint"]]) {
    const v = $("#" + id).value.trim(); if (v) params[k] = v;
  }
  const btn = $("#mcp-connect"); btn.disabled = true; btn.textContent = "Connecting…";
  try {
    const info = await api("/api/sources", { method: "POST",
      body: JSON.stringify({ name, type: "mcp", params }) });
    toast(`Connected “${name}” via AWS (${params.method}) — ${info.datasets.length} datasets`);
    state.source = name; await refreshSources();
    await fillMapSources();
  } catch (err) { toast("MCP connect failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Connect via AWS (MCP)"; }
};

/* ---------- guard ---------- */
function need() {
  if (!state.source || !state.dataset) { toast("Pick a dataset first", true); return false; }
  return true;
}

/* ---------- profiling ---------- */
$("#btn-profile").onclick = async () => {
  if (!need()) return;
  const btn = $("#btn-profile"); btn.disabled = true; btn.textContent = "Profiling…";
  try {
    const insights = $("#insights-chk").checked;
    const p = await api(`/api/profile?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}&insights=${insights}`);
    renderProfile(p);
  } catch (err) { toast("Profile failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Profile dataset"; }
};

function renderProfile(p) {
  $("#prof-summary").innerHTML = [
    ["Rows", p.row_count.toLocaleString()],
    ["Columns", p.column_count],
    ["Duplicate rows", p.duplicate_row_count.toLocaleString()],
    ["Correlated pairs", p.correlations.length],
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  const ins = $("#prof-insight");
  if (p.llm_summary) { ins.textContent = p.llm_summary; ins.classList.remove("hidden"); }
  else ins.classList.add("hidden");

  const rows = p.columns.map(c => {
    let detail = "";
    if (c.inferred_type === "numeric")
      detail = `min ${fmt(c.min)} · max ${fmt(c.max)} · μ ${fmt(c.mean)}${c.negatives ? ` · <span style="color:var(--critical)">${c.negatives} neg</span>` : ""}`;
    else if (c.inferred_type === "datetime")
      detail = `${c.min_date?.slice(0,10) ?? ""} → ${c.max_date?.slice(0,10) ?? ""}`;
    else if (c.patterns?.length)
      detail = `<span class="mono">${esc(c.patterns[0].pattern)}</span> (${c.patterns[0].pct}%)`;
    const nullClass = c.null_pct > 5 ? "pill-warn" : c.null_pct > 0 ? "pill-info" : "pill-good";
    return `<tr>
      <td><b>${esc(c.name)}</b></td>
      <td><span class="pill pill-info">${c.inferred_type}</span></td>
      <td class="num"><span class="pill ${nullClass}">${c.null_pct}%</span></td>
      <td class="num">${c.distinct_count.toLocaleString()}</td>
      <td class="num">${c.is_unique ? '<span class="pill pill-good">key</span>' : ""}</td>
      <td>${detail}</td>
    </tr>`;
  }).join("");
  $("#prof-columns").innerHTML = `<div class="sectionlabel">Columns</div><table>
    <thead><tr><th>Column</th><th>Type</th><th class="num">Null %</th><th class="num">Distinct</th><th>Unique</th><th>Detail</th></tr></thead>
    <tbody>${rows}</tbody></table>`;

  if (p.correlations.length) {
    const cr = p.correlations.map(c => `<tr><td>${esc(c.col_a)}</td><td>${esc(c.col_b)}</td>
      <td class="num">${c.coefficient}</td></tr>`).join("");
    $("#prof-corr").innerHTML = `<div class="sectionlabel">Correlations (|r| ≥ threshold)</div><table>
      <thead><tr><th>Column A</th><th>Column B</th><th class="num">Pearson r</th></tr></thead><tbody>${cr}</tbody></table>`;
  } else $("#prof-corr").innerHTML = "";
}
const fmt = (v) => v == null ? "–" : (Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 1 }) : (+v).toFixed(2));

/* ---------- rules & score ---------- */
async function loadRules() {
  if (!state.source || !state.dataset) { $("#rules-table").innerHTML = ""; return; }
  try {
    const rules = await api(`/api/rules?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}`);
    renderRules(rules);
  } catch { /* no saved ruleset yet — leave table as-is */ }
}
$("#btn-suggest").onclick = async () => {
  if (!need()) return;
  const btn = $("#btn-suggest"); btn.disabled = true; btn.textContent = "Suggesting…";
  try {
    const rules = await api(`/api/rules/suggest?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}`, { method: "POST" });
    renderRules(rules); toast(`Suggested ${rules.length} rules`);
  } catch (err) { toast("Suggest failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Suggest rules"; }
};
$("#btn-nl").onclick = async () => {
  if (!need()) return;
  const request = $("#nl-input").value.trim(); if (!request) return;
  const btn = $("#btn-nl"); btn.disabled = true; btn.textContent = "…";
  try {
    const { rules } = await api("/api/rules/nl", { method: "POST",
      body: JSON.stringify({ source: state.source, dataset: state.dataset, request }) });
    renderRules(rules); $("#nl-input").value = ""; toast("Rule(s) added");
  } catch (err) { toast(err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Add via AI"; }
};
$("#btn-run").onclick = async () => {
  if (!need()) return;
  const btn = $("#btn-run"); btn.disabled = true; btn.textContent = "Running…";
  try {
    const run = await api(`/api/run?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}`, { method: "POST" });
    state.lastRun = run; renderScore(run); renderRunResults(run);
  } catch (err) { toast("Run failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Run & score"; }
};

function renderRules(rules) {
  const rows = rules.map(r => `<tr>
    <td><b>${esc(r.name)}</b></td>
    <td><span class="pill pill-info">${r.dimension}</span></td>
    <td class="mono">${r.rule_type}</td>
    <td>${esc(r.column ?? "—")}</td>
    <td><span class="pill ${sevPill(r.severity)}">${r.severity}</span></td>
    <td class="num">${Math.round(r.threshold * 100)}%</td>
    <td class="mono">${r.source === "nl" ? "AI" : r.source}</td>
  </tr>`).join("");
  $("#rules-table").innerHTML = `<div class="sectionlabel">Rules (${rules.length})</div><table>
    <thead><tr><th>Name</th><th>Dimension</th><th>Type</th><th>Column</th><th>Severity</th><th class="num">Pass thr.</th><th>Src</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderScore(run) {
  const v = run.score, col = scoreColor(v);
  const r = 60, c = 2 * Math.PI * r, off = c * (1 - v / 100);
  $("#gauge").innerHTML = `<svg viewBox="0 0 150 150" width="150" height="150">
    <circle cx="75" cy="75" r="${r}" fill="none" stroke="var(--surface-3)" stroke-width="12"/>
    <circle cx="75" cy="75" r="${r}" fill="none" stroke="${col}" stroke-width="12"
      stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${off}"
      transform="rotate(-90 75 75)"/>
    <text x="75" y="72" text-anchor="middle" font-size="30" font-weight="700" fill="var(--text-primary)">${v}</text>
    <text x="75" y="94" text-anchor="middle" font-size="12" fill="var(--text-muted)">${run.passed_rules}/${run.total_rules} rules</text>
  </svg>`;
  const dims = Object.entries(run.dimension_scores);
  $("#dim-scores").innerHTML = dims.map(([k, val]) => `<div class="dim">
    <div class="dk">${k}</div><div class="dv" style="color:${scoreColor(val)}">${val}</div>
    <div class="track"><span style="width:${val}%;background:${scoreColor(val)}"></span></div>
  </div>`).join("");
}

function renderRunResults(run) {
  const rows = run.results.map(r => `<tr>
    <td>${r.passed ? '<span class="pill pill-good">pass</span>' : '<span class="pill pill-crit">fail</span>'}</td>
    <td><b>${esc(r.rule_name)}</b></td>
    <td>${esc(r.column ?? "—")}</td>
    <td class="num">${(r.pass_rate * 100).toFixed(1)}%</td>
    <td class="num">${r.rows_failed.toLocaleString()}</td>
    <td class="mono">${r.failed_examples.slice(0, 3).map(e => esc(JSON.stringify(e))).join(", ")}</td>
  </tr>`).join("");
  $("#run-results").innerHTML = `<div class="sectionlabel">Run results</div><table>
    <thead><tr><th>Status</th><th>Rule</th><th>Column</th><th class="num">Pass rate</th><th class="num">Failed</th><th>Examples</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

/* ---------- anomalies ---------- */
$("#btn-anomaly").onclick = async () => {
  if (!need()) return;
  const btn = $("#btn-anomaly"); btn.disabled = true; btn.textContent = "Detecting…";
  try {
    const a = await api(`/api/anomalies?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}`);
    $("#anomaly-summary").innerHTML = [
      ["Total anomalies", a.anomaly_count.toLocaleString()],
      ["Rows scanned", a.total_rows.toLocaleString()],
      ["Methods", a.methods_used.length],
    ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
    const rows = a.anomalies.slice(0, 200).map(x => `<tr>
      <td class="mono">${x.method}</td>
      <td>${esc(x.column ?? "(row)")}</td>
      <td class="num">${x.score}</td>
      <td class="mono">${esc(JSON.stringify(x.value)).slice(0, 60)}</td>
      <td>${esc(x.reason)}</td>
    </tr>`).join("");
    $("#anomaly-table").innerHTML = `<div class="sectionlabel">Anomalies (showing up to 200)</div><table>
      <thead><tr><th>Method</th><th>Column</th><th class="num">Score</th><th>Value</th><th>Reason</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  } catch (err) { toast("Detection failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Detect anomalies"; }
};

/* ---------- reconcile (source -> target mappings) ---------- */
async function fillMapSources() {
  const sources = await api("/api/sources");
  for (const id of ["#map-src-a", "#map-src-b"]) {
    const sel = $(id);
    const cur = sel.value;
    sel.innerHTML = sources.map(s =>
      `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
    if (cur) sel.value = cur;
  }
}
async function fillMapDatasets(srcSel, dsSel) {
  const src = $(srcSel).value; if (!src) return;
  const { datasets } = await api(`/api/sources/${encodeURIComponent(src)}/datasets`);
  $(dsSel).innerHTML = datasets.map(d =>
    `<option value="${esc(d)}" title="${esc(d)}">${esc(shortName(d))}</option>`).join("");
}
$("#map-src-a").onchange = () => fillMapDatasets("#map-src-a", "#map-ds-a");
$("#map-src-b").onchange = () => fillMapDatasets("#map-src-b", "#map-ds-b");

async function refreshMappings() {
  const list = await api("/api/mappings");
  const sel = $("#map-saved");
  sel.innerHTML = `<option value="">saved mappings…</option>` +
    list.map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join("");
}
$("#map-saved").onchange = (e) => {
  const name = e.target.value; if (!name) return;
  api("/api/mappings").then(list => {
    const m = list.find(x => x.name === name);
    if (m) {
      $("#map-spec").value = JSON.stringify(m, null, 2);
      $("#map-src-a").value = m.source_a; fillMapDatasets("#map-src-a", "#map-ds-a").then(() => $("#map-ds-a").value = m.dataset_a);
      $("#map-src-b").value = m.source_b; fillMapDatasets("#map-src-b", "#map-ds-b").then(() => $("#map-ds-b").value = m.dataset_b);
    }
  });
};

function mapSpec() {
  const txt = $("#map-spec").value.trim();
  if (!txt) return null;
  try { return JSON.parse(txt); }
  catch (e) { toast("Mapping spec is not valid JSON: " + e.message, true); return undefined; }
}

$("#btn-map-suggest").onclick = async () => {
  const sa = $("#map-src-a").value, da = $("#map-ds-a").value;
  const sb = $("#map-src-b").value, db = $("#map-ds-b").value;
  if (!sa || !da || !sb || !db) return toast("Pick a dataset on both sides", true);
  const btn = $("#btn-map-suggest"); btn.disabled = true;
  try {
    const s = await api(`/api/mappings/suggest?source_a=${encodeURIComponent(sa)}&dataset_a=${encodeURIComponent(da)}&source_b=${encodeURIComponent(sb)}&dataset_b=${encodeURIComponent(db)}`);
    $("#map-spec").value = JSON.stringify(s, null, 2);
    toast(`Suggested ${s.columns.length} column map(s)` +
      (s.unmatched_source.length || s.unmatched_target.length
        ? ` — unmatched: ${[...s.unmatched_source, ...s.unmatched_target].join(", ")}` : ""));
  } catch (err) { toast("Suggest failed: " + err.message, true); }
  finally { btn.disabled = false; }
};

$("#btn-map-save").onclick = async () => {
  const spec = mapSpec(); if (!spec) return;
  if (!spec.name) return toast('Mapping needs a "name"', true);
  try {
    await api(`/api/mappings/${encodeURIComponent(spec.name)}`,
              { method: "PUT", body: JSON.stringify(spec) });
    await refreshMappings();
    toast(`Saved mapping “${spec.name}”`);
  } catch (err) { toast("Save failed: " + err.message, true); }
};

$("#btn-map-del").onclick = async () => {
  const name = $("#map-saved").value;
  if (!name) return toast("Pick a saved mapping to delete", true);
  if (!confirm(`Delete mapping “${name}”?`)) return;
  try {
    await api(`/api/mappings/${encodeURIComponent(name)}`, { method: "DELETE" });
    $("#map-spec").value = ""; await refreshMappings();
    toast(`Deleted mapping “${name}”`);
  } catch (err) { toast("Delete failed: " + err.message, true); }
};

/* ---------- compare schemas (ad-hoc drift, no mapping) ---------- */
$("#btn-compare").onclick = async () => {
  const sa = $("#map-src-a").value, da = $("#map-ds-a").value;
  const sb = $("#map-src-b").value, db = $("#map-ds-b").value;
  if (!sa || !da || !sb || !db) return toast("Pick a dataset on both sides", true);
  const btn = $("#btn-compare"); btn.disabled = true; btn.textContent = "Comparing…";
  try {
    const c = await api(`/api/compare?source_a=${encodeURIComponent(sa)}&dataset_a=${encodeURIComponent(da)}&source_b=${encodeURIComponent(sb)}&dataset_b=${encodeURIComponent(db)}`);
    renderCompare(c);
  } catch (err) { toast("Compare failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Compare schemas"; }
};

function renderCompare(c) {
  const chips = (list, cls) => (list || []).map(x =>
    `<span class="pill ${cls}" style="margin-right:6px">${esc(x)}</span>`).join("");
  const cards = [
    ["Rows A", c.row_count_a], ["Rows B", c.row_count_b],
    ["Row delta", c.row_count_delta],
    ["Only in A", (c.only_in_a || []).length],
    ["Only in B", (c.only_in_b || []).length],
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  const rows = (c.column_diffs || []).map(d => `<tr>
    <td>${esc(d.column)}</td><td>${esc(d.type_a)}</td><td>${esc(d.type_b)}</td>
    <td>${d.type_changed ? '<span class="pill pill-crit">changed</span>'
                          : '<span class="pill pill-good">same</span>'}</td>
    <td class="num">${d.null_pct_a}% → ${d.null_pct_b}%</td>
    <td class="num">${d.distinct_a} → ${d.distinct_b}</td></tr>`).join("");
  $("#cmp-result").innerHTML = `<div class="cards">${cards}</div>` +
    ((c.only_in_a || []).length
      ? `<div class="sectionlabel">Only in A — removed on B</div>` + chips(c.only_in_a, "pill-crit") : "") +
    ((c.only_in_b || []).length
      ? `<div class="sectionlabel">Only in B — added on B</div>` + chips(c.only_in_b, "pill-good") : "") +
    `<div class="sectionlabel">Column comparison (${(c.column_diffs || []).length} shared)</div><table>
    <thead><tr><th>Column</th><th>Type A</th><th>Type B</th><th>Type</th><th class="num">Null %</th><th class="num">Distinct</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

$("#btn-map-run").onclick = async () => {
  const spec = mapSpec(); if (!spec) return;
  const btn = $("#btn-map-run"); btn.disabled = true; btn.textContent = "Reconciling…";
  try {
    const res = await api("/api/reconcile", { method: "POST",
      body: JSON.stringify({ mapping: spec }) });
    renderReconcile(res);
  } catch (err) { toast("Reconcile failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Run reconciliation"; }
};

function renderReconcile(res) {
  const head = `<div class="sectionlabel">${esc(res.mapping)} · ${esc(res.cardinality)} · ` +
    `${res.row_count_a} → ${res.row_count_b} rows · ` +
    (res.passed ? `<span class="pill pill-good">PASS</span>`
                : `<span class="pill pill-crit">FAIL</span>`) + `</div>`;
  const rows = res.checks.map(c => `<tr>
    <td>${c.passed ? '<span class="pill pill-good">pass</span>'
                   : `<span class="pill ${c.severity === "warning" ? "pill-warn" : "pill-crit"}">fail</span>`}</td>
    <td><b>${esc(c.name)}</b></td>
    <td>${esc(c.detail)}</td>
    <td class="mono">${c.examples.slice(0, 3).map(e => esc(JSON.stringify(e))).join(", ")}</td>
  </tr>`).join("");
  $("#map-result").innerHTML = head + `<table>
    <thead><tr><th>Status</th><th>Check</th><th>Detail</th><th>Examples</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

/* ---------- schema gate ---------- */
async function renderGateInfo() {
  if (!state.source || !state.dataset) return;
  try {
    const v = await api(`/api/schema/versions?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}`);
    $("#gate-versions").textContent = v.length
      ? `baselines for ${state.dataset}: ` + v.map(x => `v${x.version} (${x.column_count} cols)`).join(" · ")
      : `no baseline pinned for ${state.dataset} yet`;
  } catch {}
}
$("#btn-gate-pin").onclick = async () => {
  if (!need()) return;
  try {
    const e = await api(`/api/schema/pin?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}`, { method: "POST" });
    toast(`Pinned baseline v${e.version} (${e.columns.length} columns)`);
    renderGateInfo();
  } catch (err) { toast("Pin failed: " + err.message, true); }
};
$("#btn-gate-run").onclick = async () => {
  if (!need()) return;
  const btn = $("#btn-gate-run"); btn.disabled = true; btn.textContent = "Checking…";
  try {
    const g = await api(`/api/schema/gate?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}`, { method: "POST" });
    const cls = c => c === "breaking" ? "pill-crit" : c === "warning" ? "pill-warn" : "pill-good";
    const all = [...(g.breaking || []), ...(g.warnings || []), ...(g.compatible || [])];
    const rows = all.map(c => `<tr>
      <td><span class="pill ${cls(c.classification)}">${c.classification}</span></td>
      <td class="mono">${esc(c.kind)}</td><td><b>${esc(c.column ?? "—")}</b></td>
      <td>${esc(c.from ?? "—")} → ${esc(c.to ?? "—")}</td></tr>`).join("");
    $("#gate-result").innerHTML =
      `<div class="sectionlabel">${esc(g.message)}</div>` +
      (rows ? `<table><thead><tr><th>Class</th><th>Change</th><th>Column</th><th>From → To</th></tr></thead><tbody>${rows}</tbody></table>` : "");
  } catch (err) { toast("Gate failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Run gate"; }
};

/* ---------- batch manifest runner ---------- */
const SAMPLE_BATCH = {
  name: "ui-validation-pack", parallelism: 4, fail_fast: false,
  jobs: [
    { name: "customers-gate", type: "gate",
      source_file: "samples/customers_src.csv", pin_if_missing: true },
    { name: "customers-to-stg", type: "pair",
      source_file: "samples/customers_src.csv", target_file: "samples/stg_customers.csv",
      mapping: { cardinality: "1:1",
        key_pairs: [{ src: "customer_id", tgt: "customer_id" }],
        columns: [{ tgt: "full_name", transform: "first_name + ' ' + last_name" },
                  { src: "email", tgt: "email" },
                  { src: "segment", tgt: "segment" }] } },
    { name: "orders-to-stg", type: "pair",
      source_file: "samples/orders_src.csv", target_file: "samples/stg_orders.csv",
      mapping: { cardinality: "1:1",
        key_pairs: [{ src: "order_id", tgt: "order_id" }],
        columns: [{ tgt: "order_total", transform: "qty * unit_price", tolerance: 0.01 },
                  { src: "status", tgt: "status" }],
        aggregates: [{ src: "order_total", tgt: "order_total", op: "sum", tolerance: 0.5 }] } },
    { name: "orders-to-lines", type: "pair",
      source_file: "samples/orders_src.csv", target_file: "samples/order_lines.csv",
      mapping: { cardinality: "1:N",
        key_pairs: [{ src: "order_id", tgt: "order_id" }], columns: [] } },
    { name: "txn-to-daily", type: "pair",
      source_file: "samples/txn.csv", target_file: "samples/daily_summary.csv",
      mapping: { cardinality: "N:1",
        key_pairs: [{ src: "txn_date", tgt: "txn_date" }], columns: [],
        aggregates: [{ src: "amount", tgt: "total_amount", op: "sum", tolerance: 0.01 }] } },
    { name: "parts-audit", type: "audit",
      source_file: "samples/parts_inventory.csv", use_ai: false, fail_under: 50 },
  ],
};
$("#batch-spec").value = JSON.stringify(SAMPLE_BATCH, null, 2);

$("#btn-batch").onclick = async () => {
  const txt = $("#batch-spec").value.trim();
  if (!txt) return toast("Paste a JSON manifest first", true);
  let manifest;
  try { manifest = JSON.parse(txt); }
  catch (e) { return toast("Manifest must be JSON here (YAML works from the CLI): " + e.message, true); }
  const btn = $("#btn-batch"); btn.disabled = true; btn.textContent = "Running…";
  try {
    const r = await api("/api/batch", { method: "POST", body: JSON.stringify({ manifest }) });
    renderBatch(r);
    await refreshSources(); await fillMapSources();
  } catch (err) { toast("Batch failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Run batch"; }
};

function renderBatch(r) {
  const head = `<div class="sectionlabel">${esc(r.name)} · ` +
    (r.passed ? `<span class="pill pill-good">PASSED</span>`
              : `<span class="pill pill-crit">FAILED</span>`) +
    ` · ${r.jobs_failed} / ${r.jobs_total} jobs failed</div>`;
  const rows = (r.results || []).map(j => {
    const detail = j.error ||
      (j.checks ? `${j.checks.filter(c => c.passed).length}/${j.checks.length} checks` : "") ||
      (j.score !== undefined ? `score ${j.score}` : "") ||
      (j.passed ? "ok" : "");
    return `<tr>
      <td>${j.passed ? '<span class="pill pill-good">pass</span>'
                     : '<span class="pill pill-crit">fail</span>'}</td>
      <td><b>${esc(j.name)}</b></td><td>${esc(j.type)}</td>
      <td class="mono">${esc(detail)}</td></tr>`;
  }).join("");
  $("#batch-result").innerHTML = head + `<table>
    <thead><tr><th>Status</th><th>Job</th><th>Type</th><th>Detail</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

/* ---------- AI failure analysis ---------- */
function renderAnalysis(a, boxId, tableId) {
  const box = $(boxId), wrap = $(tableId);
  if (!a) { box.classList.add("hidden"); wrap.classList.add("hidden"); return; }
  box.innerHTML = `<b>${esc(a.summary || "")}</b>` +
    (a.overall_severity ? ` <span class="pill ${sevPill(a.overall_severity)}">${esc(a.overall_severity)}</span>` : "") +
    (a.generated_by === "heuristic" ? ` <span class="pill pill-info">heuristic</span>` : "");
  box.classList.remove("hidden");
  const issues = a.issues || [];
  if (!issues.length) { wrap.classList.add("hidden"); return; }
  const rows = issues.map(i => `<tr>
    <td><b>${esc(i.rule)}</b><div class="muted tiny">${esc(i.column ?? "table")}</div></td>
    <td>${esc(i.root_cause || "")}</td>
    <td>${esc(i.recommended_fix || "")}</td>
    <td><span class="pill ${i.priority === "high" ? "pill-crit" : i.priority === "medium" ? "pill-warn" : "pill-info"}">${esc(i.priority || "")}</span></td>
  </tr>`).join("");
  wrap.innerHTML = `<div class="sectionlabel">What to fix (worst first)</div><table>
    <thead><tr><th>Issue</th><th>Likely root cause</th><th>Recommended fix</th><th>Priority</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
  wrap.classList.remove("hidden");
}

$("#btn-analyze").onclick = async () => {
  if (!need()) return;
  const btn = $("#btn-analyze"); btn.disabled = true; btn.textContent = "Analysing…";
  try {
    const a = await api(`/api/analyze?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}`, { method: "POST" });
    renderAnalysis(a, "#ai-analysis", "#ai-issues");
  } catch (err) { toast("Analysis failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Explain failures (AI)"; }
};

/* ---------- audit & report ---------- */
$("#btn-audit").onclick = async () => {
  if (!need()) return;
  const btn = $("#btn-audit"); btn.disabled = true; btn.textContent = "Auditing…";
  try {
    const a = await api(`/api/audit?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}`, { method: "POST" });
    renderAudit(a);
    toast("Audit complete — export the report below");
  } catch (err) { toast("Audit failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Audit this table"; }
};

function renderAudit(a) {
  const prof = a.profile || {}, run = a.run || {}, anom = a.anomalies || {};
  $("#audit-summary").innerHTML = [
    ["Rows", (prof.row_count ?? "—").toLocaleString?.() ?? "—"],
    ["Columns", prof.column_count ?? "—"],
    ["DQ score", run.score != null ? run.score + " / 100" : "—"],
    ["Rules passed", `${run.passed_rules ?? "—"} / ${run.total_rules ?? "—"}`],
    ["Anomalies", anom.count ?? "—"],
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  renderAnalysis(a.analysis, "#audit-analysis", "#audit-issues");
  renderHistory(a.history || []);
}

async function loadHistory() {
  const wrap = $("#history-table"); if (!wrap) return;
  if (!state.source || !state.dataset) { wrap.innerHTML = ""; return; }
  try {
    const hist = await api(`/api/history?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}`);
    renderHistory(hist);
  } catch {}
}

function renderHistory(hist) {
  const wrap = $("#history-table");
  if (!hist.length) { wrap.innerHTML = ""; return; }
  const rows = hist.slice(0, 15).map(h => `<tr>
    <td>${esc(String(h.started_at || "").slice(0, 19).replace("T", " "))}</td>
    <td class="num" style="color:${scoreColor(h.score ?? 0)};font-weight:600">${h.score ?? "—"}</td>
    <td class="num">${h.passed_rules ?? "—"}</td>
    <td class="num">${h.failed_rules ?? "—"}</td>
  </tr>`).join("");
  wrap.innerHTML = `<div class="sectionlabel">Recent runs for this dataset</div><table>
    <thead><tr><th>When (UTC)</th><th class="num">DQ score</th><th class="num">Passed</th><th class="num">Failed</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

async function downloadReport(fmt) {
  if (!need()) return;
  const btn = $(`#btn-${fmt}`); btn.disabled = true;
  try {
    const url = `/api/report?source=${encodeURIComponent(state.source)}&dataset=${encodeURIComponent(state.dataset)}&format=${fmt}`;
    const r = await fetch(url);
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^";]+)"?/);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (m && m[1]) || `datavigil_report.${fmt}`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast(`${fmt.toUpperCase()} report downloaded`);
  } catch (err) { toast("Report failed: " + err.message, true); }
  finally { btn.disabled = false; }
}
$("#btn-docx").onclick = () => downloadReport("docx");
$("#btn-pdf").onclick = () => downloadReport("pdf");

/* ---------- schema audit (all tables) ---------- */
$("#btn-audit-schema").onclick = async () => {
  if (!state.source) return toast("Pick a source first", true);
  const btn = $("#btn-audit-schema");
  btn.disabled = true; btn.textContent = "Auditing schema…";
  try {
    const a = await api(`/api/audit/schema?source=${encodeURIComponent(state.source)}`,
                        { method: "POST" });
    renderSchemaAudit(a);
    toast(`Schema audit complete — ${a.summary?.tables_audited ?? 0} tables`);
  } catch (err) { toast("Schema audit failed: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Audit whole schema"; }
};

function renderSchemaAudit(a) {
  $("#schema-audit").classList.remove("hidden");
  const s = a.summary || {};
  $("#schema-summary").innerHTML = [
    ["Tables audited", s.tables_audited ?? "—"],
    ["Avg DQ score", s.avg_score != null ? s.avg_score + " / 100" : "—"],
    ["Worst table", s.min_score != null ? s.min_score + " / 100" : "—"],
    ["Rules failed", `${s.total_failed_rules ?? "—"} / ${s.total_rules ?? "—"}`],
    ["Anomalies", s.total_anomalies ?? "—"],
    ["Could not audit", s.tables_failed ?? "—"],
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  const rows = (a.tables || []).map(t => `<tr>
    <td>${esc(t.dataset)}</td>
    <td class="num" style="color:${t.score == null ? "inherit" : scoreColor(t.score)};font-weight:600">${t.score == null ? "—" : t.score}</td>
    <td class="num">${t.rows == null ? "—" : t.rows.toLocaleString()}</td>
    <td class="num">${t.rules_failed} / ${t.rules_total}</td>
    <td class="num">${t.anomalies}</td>
    <td>${esc(t.severity || "—")}</td>
  </tr>`).join("");
  const errRows = (a.errors || []).map(e =>
    `<tr><td>${esc(e.dataset)}</td><td colspan="5" class="muted">${esc(e.error)}</td></tr>`).join("");
  $("#schema-tables").innerHTML = `<div class="sectionlabel">Score by table (worst first)</div><table>
    <thead><tr><th>Table</th><th class="num">Score</th><th class="num">Rows</th><th class="num">Rules failed</th><th class="num">Anomalies</th><th>Severity</th></tr></thead>
    <tbody>${rows}${errRows}</tbody></table>`;
}

async function downloadSchemaReport(fmt) {
  if (!state.source) return toast("Pick a source first", true);
  const btn = $(`#btn-schema-${fmt}`); btn.disabled = true;
  try {
    const url = `/api/report/schema?source=${encodeURIComponent(state.source)}&format=${fmt}`;
    const r = await fetch(url);
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^";]+)"?/);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (m && m[1]) || `datavigil_schema_report.${fmt}`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast(`Schema ${fmt.toUpperCase()} report downloaded`);
  } catch (err) { toast("Schema report failed: " + err.message, true); }
  finally { btn.disabled = false; }
}
$("#btn-schema-docx").onclick = () => downloadSchemaReport("docx");
$("#btn-schema-pdf").onclick = () => downloadSchemaReport("pdf");

/* ---------- LLM settings ---------- */
const _modelHints = {
  bedrock: "us.amazon.nova-pro-v1:0",
  ollama: "llama3.1",
  openai: "gpt-4o",
  azure_openai: "",
  none: "",
};
// which fields are visible per provider + AWS auth method
function updateLlmFields() {
  const p = $("#llm-provider").value;
  const auth = $("#llm-aws-auth").value;
  const show = (id, on) => $(id).classList.toggle("hidden", !on);
  const aws = p === "bedrock";
  show("#f-llm-region", aws);
  show("#f-llm-aws-auth", aws);
  show("#f-llm-aws-profile", aws && auth === "profile");
  show("#f-llm-aws-access", aws && (auth === "keys" || auth === "keys_session"));
  show("#f-llm-aws-secret", aws && (auth === "keys" || auth === "keys_session"));
  show("#f-llm-aws-session", aws && auth === "keys_session");
  show("#f-llm-aws-apikey", aws && auth === "api_key");
  show("#f-llm-base-url", p === "ollama" || p === "openai" || p === "azure_openai");
  show("#f-llm-api-key", p === "openai" || p === "azure_openai");
}
$("#llm-provider").onchange = (e) => {
  const hint = _modelHints[e.target.value];
  if (hint !== undefined && hint) $("#llm-model").value = hint;
  updateLlmFields();
};
$("#llm-aws-auth").onchange = updateLlmFields;

$("#settings-btn").onclick = async () => {
  try {
    const c = await api("/api/llm/config");
    $("#llm-provider").value = c.provider || "ollama";
    $("#llm-model").value = c.model || "";
    $("#llm-region").value = c.region || "";
    $("#llm-aws-auth").value = c.aws_auth || "profile";
    $("#llm-aws-profile").value = c.aws_profile || "";
    $("#llm-base-url").value = c.base_url || "";
    // secrets are never sent back — show a marker when one is stored
    const mark = (id, has, ph) => {
      const el = $(id); el.value = "";
      el.placeholder = has ? "•••••• (saved — blank keeps it)" : ph;
    };
    mark("#llm-api-key", c.has_api_key, "API key");
    mark("#llm-aws-access", c.has_aws_access_key, "AKIA…");
    mark("#llm-aws-secret", c.has_aws_secret_key, "secret access key");
    mark("#llm-aws-session", c.has_aws_session_token, "session token");
    mark("#llm-aws-apikey", c.has_bedrock_api_key, "Bedrock API key");
    $("#llm-enabled").checked = !!c.enabled;
    updateLlmFields();
  } catch {}
  $("#settings-modal").classList.remove("hidden");
};
$("#llm-cancel").onclick = () => $("#settings-modal").classList.add("hidden");
$("#settings-modal").onclick = (e) => {
  if (e.target.id === "settings-modal") $("#settings-modal").classList.add("hidden");
};
$("#llm-save").onclick = async () => {
  const body = {
    provider: $("#llm-provider").value,
    model: $("#llm-model").value.trim() || null,
    region: $("#llm-region").value.trim() || null,
    aws_profile: $("#llm-aws-profile").value.trim() || null,
    aws_auth: $("#llm-aws-auth").value,
    base_url: $("#llm-base-url").value.trim() || null,
    enabled: $("#llm-enabled").checked,
  };
  // secret fields: blank = keep what's already saved
  for (const [k, id] of [["api_key", "llm-api-key"],
    ["aws_access_key_id", "llm-aws-access"],
    ["aws_secret_access_key", "llm-aws-secret"],
    ["aws_session_token", "llm-aws-session"],
    ["bedrock_api_key", "llm-aws-apikey"]]) {
    const v = $("#" + id).value.trim();
    if (v) body[k] = v;
  }
  try {
    const h = await api("/api/llm/config", { method: "POST", body: JSON.stringify(body) });
    $("#settings-modal").classList.add("hidden");
    const badge = $("#llm-badge");
    if (h.ok) { badge.textContent = `LLM: ${h.model}`; badge.className = "badge badge-ok"; }
    else { badge.textContent = "LLM: offline"; badge.className = "badge badge-off";
           toast("Saved, but the model did not respond: " + (h.error || ""), true); }
  } catch (err) { toast("Save failed: " + err.message, true); }
};

/* ---------- ask ---------- */
$("#btn-ask").onclick = ask;
$("#ask-input").onkeydown = (e) => { if (e.key === "Enter") ask(); };
async function ask() {
  if (!need()) return;
  const q = $("#ask-input").value.trim(); if (!q) return;
  const log = $("#chat-log");
  log.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(q)}</div>`);
  $("#ask-input").value = "";
  log.insertAdjacentHTML("beforeend", `<div class="msg bot" id="pending">…</div>`);
  log.scrollTop = log.scrollHeight;
  try {
    const { answer } = await api("/api/ask", { method: "POST",
      body: JSON.stringify({ source: state.source, dataset: state.dataset, question: q }) });
    $("#pending").textContent = answer; $("#pending").id = "";
  } catch (err) { $("#pending").textContent = "⚠ " + err.message; $("#pending").id = ""; }
  log.scrollTop = log.scrollHeight;
}

/* ---------- boot ---------- */
(async () => {
  try {
    const h = await api("/api/health");
    const badge = $("#llm-badge");
    if (h.llm?.ok) { badge.textContent = `LLM: ${h.llm.model}`; badge.className = "badge badge-ok"; }
    else { badge.textContent = "LLM: offline"; badge.className = "badge badge-off"; }
    if (h.mcp_available === false) $("#mcp-note").classList.remove("hidden");
  } catch {}
  await refreshSources();
  try {
    await fillMapSources();
    await Promise.all([fillMapDatasets("#map-src-a", "#map-ds-a"),
                       fillMapDatasets("#map-src-b", "#map-ds-b")]);
    await refreshMappings();
  } catch {}
})();
