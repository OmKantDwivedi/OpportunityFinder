/* Threadfinder dashboard logic */
const $ = (id) => document.getElementById(id);

const state = { sort: "score", order: "desc", pollTimer: null };

const fmt = new Intl.NumberFormat("en-US");
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

function currentFilters() {
  return {
    search: $("search").value.trim(),
    status: $("statusFilter").value,
    product_id: $("productFilter").value,
    min_traffic: $("minTraffic").value,
    min_score: $("minScore").value,
  };
}

function qs(extra = {}) {
  const params = new URLSearchParams();
  const f = { ...currentFilters(), ...extra };
  for (const [k, v] of Object.entries(f)) if (v) params.set(k, v);
  return params.toString();
}

/* ------------------------------------------------------------- rendering */
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function rowHtml(o) {
  const statuses = ["New", "Approved", "Commented", "Rejected", "On Hold"];
  const opts = statuses.map((s) =>
    `<option ${s === o.status ? "selected" : ""}>${s}</option>`).join("");
  return `<tr>
    <td class="cell-product"><strong>${escapeHtml(o.product)}</strong><span>${escapeHtml(o.brand)} · $${o.price.toFixed(2)}</span></td>
    <td class="cell-kw">${escapeHtml(o.keyword)}</td>
    <td class="cell-thread">
      <a href="${escapeHtml(o.url)}" target="_blank" rel="noopener" title="${escapeHtml(o.title)}">${escapeHtml(o.title || o.url)}</a>
      <span class="sub">r/${escapeHtml(o.subreddit)}</span>
    </td>
    <td class="num">#${o.position}</td>
    <td class="num">${fmt.format(o.traffic)}</td>
    <td class="num">$${o.commission.toFixed(2)}</td>
    <td class="num"><span class="score-cell">
      <span class="score-bar"><i style="width:${Math.min(100, o.score)}%"></i></span>
      <span class="score-val">${o.score.toFixed(1)}</span>
    </span></td>
    <td><select class="status-select status-${o.status.replace(" ", ".")}" data-id="${o.id}">${opts}</select></td>
  </tr>`;
}

async function loadOpportunities() {
  const res = await fetch(`/api/opportunities?${qs({ sort: state.sort, order: state.order, limit: 500 })}`);
  const data = await res.json();
  const body = $("oppBody");
  body.innerHTML = data.items.map(rowHtml).join("");
  $("emptyState").hidden = data.total > 0;
  $("oppTable").style.display = data.total > 0 ? "" : "none";
  $("tableFoot").textContent = data.total
    ? `${fmt.format(data.items.length)} of ${fmt.format(data.total)} opportunities shown`
    : "";
  body.querySelectorAll(".status-select").forEach((sel) =>
    sel.addEventListener("change", async (e) => {
      const id = e.target.dataset.id;
      await fetch(`/api/opportunities/${id}/status`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: e.target.value }),
      });
      e.target.className = `status-select status-${e.target.value.replace(" ", ".")}`;
      loadStats();
    }));
}

async function loadStats() {
  const s = await (await fetch("/api/stats")).json();
  $("stProducts").textContent = fmt.format(s.products);
  $("stKeywords").textContent = fmt.format(s.keywords);
  $("stThreads").textContent = fmt.format(s.threads);
  $("stOpps").textContent = fmt.format(s.opportunities);
  $("stTraffic").textContent = fmt.format(s.total_traffic);
}

async function loadProducts() {
  const products = await (await fetch("/api/products")).json();
  const sel = $("productFilter");
  const cur = sel.value;
  sel.innerHTML = `<option value="">All products</option>` + products
    .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)} ($${p.commission.toFixed(0)})</option>`)
    .join("");
  sel.value = cur;
}

/* -------------------------------------------------------------- pipeline */
let wasRunning = false;

function setNotice(html, kind) {
  const n = $("notice");
  if (!html) { n.hidden = true; return; }
  n.hidden = false;
  n.className = `notice notice-${kind}`;
  n.innerHTML = html;
}

function renderLog(lines, error) {
  const box = $("logBox");
  if (!lines || lines.length === 0) return;
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
  box.innerHTML = lines
    .map((l) => /ERROR|WARNING/.test(l) ? `<span class="err">${escapeHtml(l)}</span>` : escapeHtml(l))
    .join("\n");
  if (stick) box.scrollTop = box.scrollHeight;
  $("logHint").textContent = error ? "failed — see last lines" : (lines[lines.length - 1] || "");
}

async function pollPipeline() {
  try {
    const st = await (await fetch("/api/pipeline/status")).json();
    $("mockBadge").hidden = !st.mock_mode;

    const running = st.running;
    $("runBtn").disabled = running;
    $("runBtn").textContent = running ? "Running…" : "Run pipeline";
    const stopBtn = $("stopBtn");
    stopBtn.hidden = !running;
    if (!running) { stopBtn.disabled = false; stopBtn.textContent = "Stop"; }
    if (running && st.cancel_requested) { stopBtn.disabled = true; stopBtn.textContent = "Stopping…"; }
    $("progressWrap").hidden = !running;
    if (running) {
      $("progressFill").style.width = `${st.progress}%`;
      $("progressLabel").textContent = st.detail || st.step;
      $("logPanel").open = true;
    }
    renderLog(st.log, st.error);

    // Banners, in priority order
    if (st.credentials_error) {
      setNotice(`<strong>Configuration needed:</strong> ${escapeHtml(st.credentials_error)}`, "error");
    } else if (st.error) {
      setNotice(`<strong>Last run failed:</strong> ${escapeHtml(st.error)} — open the activity log below for details.`, "error");
    } else if (st.interrupted_run) {
      setNotice(`<strong>Run interrupted:</strong> ${escapeHtml(st.interrupted_run)}`, "warn");
    } else if (st.mock_mode) {
      setNotice(`<strong>Demo mode (mock data).</strong> Products, SERPs and traffic are simulated —
        Reddit links are fake and will show "Page not found". To use real data, put your keys in
        <code>.env</code> (<code>LEVANTA_API_KEY</code>, <code>AHREFS_API_KEY</code>), set
        <code>MOCK_MODE=false</code>, and restart the server.`, "warn");
    } else {
      setNotice(null);
    }

    if (wasRunning && !running) refreshAll();   // run just finished
    wasRunning = running;
  } catch (e) {
    // Server briefly unreachable (restart/reload) — keep polling, never freeze the UI
    $("progressLabel").textContent = "Reconnecting to server…";
  } finally {
    setTimeout(pollPipeline, wasRunning ? 1000 : 2500);
  }
}

async function runPipeline() {
  const res = await fetch("/api/pipeline/run", { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    setNotice(`<strong>Could not start:</strong> ${escapeHtml(body.detail || res.statusText)}`, "error");
    return;
  }
  wasRunning = true;
  $("logPanel").open = true;
}

function refreshAll() {
  loadStats();
  loadProducts();
  loadOpportunities();
  $("exportBtn").href = `/api/export.csv?${qs()}`;
}

/* ----------------------------------------------------------------- wiring */
$("runBtn").addEventListener("click", runPipeline);
$("stopBtn").addEventListener("click", async () => {
  $("stopBtn").disabled = true;
  $("stopBtn").textContent = "Stopping…";
  await fetch("/api/pipeline/cancel", { method: "POST" }).catch(() => {});
});
const onFilter = debounce(() => { loadOpportunities(); $("exportBtn").href = `/api/export.csv?${qs()}`; }, 250);
["search", "statusFilter", "productFilter", "minTraffic", "minScore"].forEach((id) =>
  $(id).addEventListener("input", onFilter));

document.querySelectorAll("th.sortable").forEach((th) =>
  th.addEventListener("click", () => {
    const col = th.dataset.sort;
    if (state.sort === col) state.order = state.order === "desc" ? "asc" : "desc";
    else { state.sort = col; state.order = "desc"; }
    document.querySelectorAll("th.sortable").forEach((t) => t.classList.remove("sorted-desc", "sorted-asc"));
    th.classList.add(state.order === "desc" ? "sorted-desc" : "sorted-asc");
    loadOpportunities();
  }));

/* -------------------------------------------------------------- settings */
const SETTING_INPUTS = {
  MIN_COMMISSION_USD: "setMinComm",
  LEVANTA_PAGE_RANGE: "setPages",
  LEVANTA_BRAND_IDS: "setBrandIds",
};

async function loadSettings() {
  try {
    const settings = await (await fetch("/api/settings")).json();
    for (const s of settings) {
      const input = $(SETTING_INPUTS[s.key]);
      if (input) input.value = s.value;
    }
  } catch (e) { /* server briefly unavailable - polling will recover */ }
}

async function saveSettings() {
  const body = {};
  for (const [key, id] of Object.entries(SETTING_INPUTS)) {
    const input = $(id);
    if (input) body[key] = input.value;
  }
  const status = $("settingsStatus");
  status.className = "log-hint";
  status.textContent = "Saving…";
  let res, data;
  try {
    res = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data = await res.json().catch(() => ({}));
  } catch (e) {
    status.className = "log-hint settings-err";
    status.textContent = "Could not reach server — is it running?";
    return;
  }
  if (res.ok) {
    status.className = "log-hint settings-ok";
    status.textContent = "Saved — will apply to the next run";
    for (const [key, val] of Object.entries(data.settings || {})) {
      const input = $(SETTING_INPUTS[key]);
      if (input) input.value = val;
    }
  } else {
    status.className = "log-hint settings-err";
    status.textContent = data.detail || "Save failed";
  }
}

$("saveSettings").addEventListener("click", saveSettings);
loadSettings();

refreshAll();
pollPipeline();