"use strict";
// EpisodeID web UI — plain JS, no build step.

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const view = $("#view");
let pollTimer = null;
let state = { series: null, plan: null, tab: "plan", filter: "actions" };

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  if (r.status === 401) { location.reload(); throw new Error("Login required"); }  // the browser asks for the login
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms);
}
const fail = (e) => toast("⚠ " + e.message, 6000);

// sticky: a click outside the dialog doesn't close it (dialogs you type into and read from).
function modal(html, { sticky = false } = {}) {
  $("#modal-box").innerHTML = html;
  $("#modal").classList.remove("hidden");
  $("#modal").dataset.sticky = sticky ? "1" : "";
  return $("#modal-box");
}
function closeModal() { $("#modal").classList.add("hidden"); $("#modal-box").innerHTML = ""; $("#modal-box").style.width = ""; }
// Close on a click outside the dialog — only when the press started outside it too, so
// selecting text in a box and letting go past the edge doesn't close it.
let modalDownOutside = false;
$("#modal").addEventListener("mousedown", (e) => { modalDownOutside = e.target.id === "modal"; });
$("#modal").addEventListener("click", (e) => {
  if (e.target.id === "modal" && modalDownOutside && !$("#modal").dataset.sticky) closeModal();
});

const fmtTime = (s) => {
  s = Math.max(0, Math.round(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const fmtCut = (t) => {
  const m = Math.floor(t / 60), sec = t - m * 60;
  return `${m}:${sec < 10 ? "0" : ""}${sec.toFixed(1)}`;
};
const parseCut = (v) => {
  const p = String(v).trim().split(":").map(Number);
  return p.some(isNaN) ? NaN : p.reduce((a, x) => a * 60 + x, 0);
};
const when = (iso) => iso ? new Date(iso).toLocaleString() : "";

const STATUS_BADGE = {
  OK: "b-ok", OK_ORDER_DIFFERS: "b-warn", MISMATCH: "b-bad", LOW_CONFIDENCE: "b-warn",
  NO_TEXT: "", NO_MATCH: "b-warn", ERROR: "b-bad", MANUAL: "b-info", PARTS: "b-info",
};
const KIND_BADGE = { rename: "b-info", split: "b-warn", join: "b-warn", aside: "b-bad", review: "", ok: "b-ok" };
const ACTIONABLE = ["rename", "split", "join", "aside"];

// -------------------------------------------------------------------- theme
// Auto (follow the system) → Light → Dark. Saved in this browser only.

const THEME_KEY = "episodeid-theme";
const THEME_ICONS = {
  auto: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/></svg>',
  light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  dark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
};
const THEME_LABEL = { auto: "Theme: follow system", light: "Theme: light", dark: "Theme: dark" };

function getTheme() {
  try { const t = localStorage.getItem(THEME_KEY); return t === "light" || t === "dark" ? t : "auto"; }
  catch { return "auto"; }
}

function applyTheme(t) {
  const root = document.documentElement;
  if (t === "auto") delete root.dataset.theme; else root.dataset.theme = t;
  // The header icon adapts to the system theme on its own; a chosen theme needs the matching variant.
  const icon = $("#brand-icon");
  if (icon) icon.src = t === "light" ? "/static/icon-light.svg" : t === "dark" ? "/static/icon-dark.svg" : "/static/icon.svg";
  const btn = $("#theme-btn");
  if (btn) {
    btn.innerHTML = THEME_ICONS[t];
    btn.title = THEME_LABEL[t] + " (click to change)";
    btn.setAttribute("aria-label", THEME_LABEL[t]);
  }
}

let currentTheme = getTheme();

function cycleTheme() {
  const next = currentTheme = { auto: "light", light: "dark", dark: "auto" }[currentTheme];
  try { if (next === "auto") localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, next); }
  catch { /* storage blocked: still switch for this visit */ }
  applyTheme(next);
  toast(THEME_LABEL[next]);
}

applyTheme(currentTheme);
$("#theme-btn").onclick = cycleTheme;

// ------------------------------------------------------------------ routing

async function route() {
  clearInterval(pollTimer);
  const h = location.hash || "#/";
  $$("nav a").forEach((a) => a.classList.toggle("active",
    (h.startsWith("#/settings") && a.dataset.nav === "settings") ||
    ((h === "#/" || h.startsWith("#/series")) && a.dataset.nav === "series")));
  try {
    if (h.startsWith("#/settings")) await renderSettings();
    else if (h.startsWith("#/series/")) await renderSeries(+h.split("/")[2]);
    else await renderHome();
  } catch (e) {
    view.innerHTML = `<div class="card">⚠ ${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

async function refreshChips() {
  try {
    const s = await api("/api/status");
    const chip = (on, label) => `<span class="chip ${on ? "on" : ""}">${on ? "●" : "○"} ${label}</span>`;
    $("#chips").innerHTML = chip(s.tvdb, "TVDB") + chip(s.opensubtitles, "OpenSubtitles") +
      chip(s.whisper, "Whisper") + chip(s.titlecards, "Title cards") + chip(s.llm, "AI") + chip(s.sonarr, "Sonarr") +
      `<span class="chip">v${esc(s.version)}</span>`;
    return s;
  } catch { return {}; }
}

// --------------------------------------------------------------------- home

async function renderHome() {
  const [status, list] = await Promise.all([refreshChips(), api("/api/series")]);
  let html = `<div class="row"><div><h1>Series</h1>
    <div class="muted">Each series is a folder in your library matched to a TVDB show.</div></div>
    <div class="spacer"></div><button class="btn" id="unmon-all" title="Every series: seasons where every aired episode has a correct file">Unmonitor complete seasons…</button>
    <button class="btn primary" id="add">+ Add series</button></div>`;
  if (!status.tvdb) {
    html += `<div class="card" style="margin-top:16px">Start by adding your TVDB API key in
      <a href="#/settings">Settings</a> — EpisodeID uses TVDB episode numbering so names line up with Sonarr.</div>`;
  }
  if (!list.length) {
    html += `<div class="empty">No series yet. Click <b>Add series</b> to pick a show folder.</div>`;
  } else {
    html += `<div class="grid" style="margin-top:16px">` + list.map((s) => {
      const pct = s.episode_count ? Math.round(100 * s.reference_count / s.episode_count) : 0;
      const counts = seriesCardCounts(s);
      return `<div class="card series-card" data-id="${s.id}">
        <div class="name">${esc(s.name)} <span class="muted">${esc(s.year)}</span></div>
        <div class="path muted">${esc(s.path)}</div>
        <div class="small muted">References: ${s.reference_count} / ${s.episode_count} episodes</div>
        <div class="bar"><div style="width:${pct}%"></div></div>
        <div class="counts">${counts}</div>
        ${fileCheckLine(s.file_check)}${s.file_check && s.file_check.checked_at ? importLine(s.file_check) : ""}
        ${s.active_job ? `<div class="small"><span class="badge b-info">${esc(s.active_job.kind)} running…</span></div>` : ""}
      </div>`;
    }).join("") + `</div>`;
  }
  view.innerHTML = html;
  $("#add").onclick = () => addSeriesDialog();
  $("#unmon-all").onclick = () => unmonitorAllDialog();
  $$(".series-card").forEach((c) => c.onclick = () => location.hash = `#/series/${c.dataset.id}`);
}

function ago(iso) {
  const h = (Date.now() - new Date(iso).getTime()) / 3600e3;
  return h < 1 ? "under an hour ago" : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} days ago`;
}

function importLine(fc) {
  const bad = (fc && fc.imports) || [];
  if (!bad.length) return "";
  return `<div class="small" title="${esc(bad.map((x) => `${x.rel}: ${x.status} — ${x.note}`).join("\n"))}">
    <span class="badge b-bad">⚑ ${bad.length} new download${bad.length === 1 ? "" : "s"} ${bad.length === 1 ? "doesn't" : "don't"} match ${bad.length === 1 ? "its name" : "their names"}</span></div>`;
}

function fileCheckLine(fc) {
  if (!fc || !fc.checked_at) return importLine(fc);
  const p = fc.pending;
  if (!p) return `<div class="small muted">Files checked ${ago(fc.checked_at)} · no changes since the last scan</div>`;
  const what = [p.added && `${p.added} new`, p.changed && `${p.changed} replaced`, p.removed && `${p.removed} removed`]
    .filter(Boolean).join(", ");
  return `<div class="small" title="${esc((p.examples || []).join("\n"))}"><span class="badge b-info">⚑ ${what} since the last scan</span>
    <span class="muted">— open the series and Scan files</span></div>`;
}

function seriesCardCounts(s) {
  if (!s.last_scan) return `<span class="muted small">Not scanned yet</span>`;
  const scanTip = "Last scan: " + Object.entries(s.last_scan.counts).map(([k, v]) => `${k} ${v}`).join(", ");
  const k = (s.plan && s.plan.kinds) || null;
  if (!k) return Object.entries(s.last_scan.counts).map(([n, v]) =>
    `<span class="badge ${STATUS_BADGE[n] || ""}">${esc(n)} ${v}</span>`).join("");
  const ok = k.ok || 0, review = k.review || 0;
  const changes = (k.rename || 0) + (k.split || 0) + (k.aside || 0);
  if (!changes && !review) {
    const scanned = s.file_check && s.file_check.pending ? " scanned" : "";
    return `<span class="badge b-ok" title="${esc(scanTip)}">✓ All ${ok}${scanned} file${ok === 1 ? "" : "s"} correct</span>`;
  }
  return [ok ? `<span class="badge b-ok" title="${esc(scanTip)}">${ok} correct</span>` : "",
    changes ? `<span class="badge b-warn">${changes} to change</span>` : "",
    review ? `<span class="badge b-bad">${review} to review</span>` : ""].join("");
}

async function addSeriesDialog() {
  const box = modal(`<h2 style="margin-top:0">Add series</h2>
    <input type="search" id="q" placeholder="Search your library, e.g. gumball" class="grow-input" style="width:100%" autocomplete="off">
    <div id="results" style="margin-top:10px"></div>
    <div id="step2"></div>
    <div class="row" style="margin-top:12px"><a id="browse" style="cursor:pointer" class="small">Browse folders instead…</a>
      <div class="spacer"></div><button class="btn" id="cancel">Cancel</button></div>
    <div id="browser"></div>`);
  $("#cancel", box).onclick = closeModal;
  const q = $("#q", box);
  q.focus();
  let timer = null, seq = 0;

  async function list() {
    const my = ++seq;
    $("#step2", box).innerHTML = "";
    try {
      const r = await api(`/api/folders/search?q=${encodeURIComponent(q.value.trim())}`);
      if (my !== seq) return;  // a newer keystroke already answered
      const res = r.results;
      $("#results", box).innerHTML = `<div class="list">${res.map((f, i) => `<div class="item" data-i="${i}">
        📁 <div style="flex:1"><b>${esc(f.name)}</b> ${f.year ? `<span class="muted">${esc(f.year)}</span>` : ""}
          ${f.tvdb_id ? `<span class="badge b-info">tvdb ${f.tvdb_id}</span>` : ""}
          ${f.added_id ? `<span class="badge b-ok">added</span>` : ""}
          <div class="path muted">${esc(f.path)}</div></div></div>`).join("") ||
        `<div class="item muted">No folders in your library match “${esc(q.value)}”.</div>`}</div>
        ${r.total > res.length ? `<div class="small muted" style="margin-top:4px">${r.total - res.length} more — keep typing to narrow down.</div>` : ""}`;
      $$("#results .item[data-i]", box).forEach((el) => el.onclick = () => {
        const f = res[+el.dataset.i];
        if (f.added_id) { closeModal(); location.hash = `#/series/${f.added_id}`; return; }
        identify(f.path);
      });
    } catch (e) { $("#results", box).innerHTML = `<div class="card">⚠ ${esc(e.message)}</div>`; }
  }
  q.oninput = () => { clearTimeout(timer); timer = setTimeout(list, 150); };
  q.onkeydown = (e) => { if (e.key === "Enter") { clearTimeout(timer); list(); } };
  list();
  $("#browse", box).onclick = () => browseFolders($("#browser", box), identify);

  async function add(path, tvdbId) {
    try {
      const s = await api("/api/series", { method: "POST", body: { path, tvdb_id: tvdbId } });
      closeModal();
      location.hash = `#/series/${s.id}`;
    } catch (e) { fail(e); }
  }

  function showCandidates(step, path, results, best) {
    $("#cands", step).innerHTML = results.map((r, i) => `<div class="item" data-i="${i}" ${i === 0 && best ? 'style="background:var(--panel-2)"' : ""}>
        ${r.image ? `<img src="${esc(r.image)}" alt="" loading="lazy">` : `<img alt="">`}
        <div style="flex:1"><b>${esc(r.name)}</b> <span class="muted">${esc(r.year)}${r.network ? " · " + esc(r.network) : ""} · tvdb ${r.tvdb_id}</span>
          ${i === 0 && best ? ` <span class="badge b-ok">best match</span>` : ""}
          <div class="small muted">${esc((r.overview || "").slice(0, 160))}</div></div>
        <button class="btn small ${i === 0 && best ? "primary" : ""}">Add</button></div>`).join("") ||
      `<div class="item muted">No TVDB results — try different words above.</div>`;
    $$("#cands .item[data-i]", step).forEach((el) => el.onclick = () => add(path, results[+el.dataset.i].tvdb_id));
  }

  async function identify(path) {
    $("#results", box).innerHTML = "";
    $("#browser", box).innerHTML = "";
    const step = $("#step2", box);
    step.innerHTML = `<div class="card muted">Identifying ${esc(path)} on TVDB…</div>`;
    let r;
    try { r = await api(`/api/folders/resolve?path=${encodeURIComponent(path)}`); }
    catch (e) {
      step.innerHTML = `<div class="card">⚠ ${esc(e.message)}${/key/i.test(e.message) ?
        ` — add your TVDB key in <a href="#/settings" onclick="closeModal()">Settings</a>.` : ""}</div>`;
      return;
    }
    step.innerHTML = `<div class="card" style="margin-bottom:10px">📁 <b>${esc(r.folder)}</b>
        <span class="muted small">· ${r.videos} video files</span> <a class="small" id="change" style="cursor:pointer">change folder</a></div>
      <h2 style="margin:0 0 6px">${r.confident ? "Is this the show?" : "Which show is this?"}</h2>
      <div class="muted small" style="margin-bottom:8px">${r.tvdb_id_tag ? `The folder is tagged tvdb ${r.tvdb_id_tag}.` :
        `Matched on TVDB by the folder name${r.year ? " and year" : ""}.`} EpisodeID uses TVDB's episode list and numbering, the same as Sonarr.</div>
      <div class="list" id="cands"></div>
      <div class="row" style="margin-top:8px"><input type="search" id="tq" value="${esc(r.query)}" class="grow-input" placeholder="Search TVDB">
        <button class="btn" id="tsearch">Search TVDB</button></div>`;
    $("#change", step).onclick = () => { step.innerHTML = ""; list(); };
    showCandidates(step, r.path, r.results, r.confident);
    const tsearch = async () => {
      try { showCandidates(step, r.path, await api(`/api/tvdb/search?q=${encodeURIComponent($("#tq", step).value)}`), false); }
      catch (e) { fail(e); }
    };
    $("#tsearch", step).onclick = tsearch;
    $("#tq", step).onkeydown = (e) => { if (e.key === "Enter") tsearch(); };
  }
}

async function browseFolders(el, onPick) {
  el.innerHTML = `<div class="crumbs" id="crumbs"></div><div class="list" id="dirs"></div>
    <div class="row" style="margin-top:10px"><span class="muted small" id="folder-info"></span><div class="spacer"></div>
    <button class="btn primary" id="choose" disabled>Use this folder</button></div>`;
  let current = "";
  async function load(path) {
    try {
      const d = await api(`/api/browse?path=${encodeURIComponent(path)}`);
      current = d.path;
      const parts = d.path ? d.path.split("/") : [];
      $("#crumbs", el).innerHTML = `<a data-p="">media</a>` + parts.map((p, i) =>
        ` / <a data-p="${esc(parts.slice(0, i + 1).join("/"))}">${esc(p)}</a>`).join("");
      $$("#crumbs a", el).forEach((a) => a.onclick = () => load(a.dataset.p));
      $("#dirs", el).innerHTML = d.dirs.map((x) =>
        `<div class="item" data-p="${esc(x.path)}">📁 ${esc(x.name)}</div>`).join("") ||
        `<div class="item muted">No sub-folders</div>`;
      $$("#dirs .item[data-p]", el).forEach((i) => i.onclick = () => load(i.dataset.p));
      $("#folder-info", el).textContent = d.path ? `${d.videos} video files directly in this folder` : "";
      $("#choose", el).disabled = !d.path;
    } catch (e) { fail(e); }
  }
  $("#choose", el).onclick = () => onPick(current);
  load("");
}

// ------------------------------------------------------------------- series

async function renderSeries(id) {
  refreshChips();
  const s = await api(`/api/series/${id}`);
  state.series = s;
  view.innerHTML = `<div class="row">
      <div><a href="#/" class="small">← All series</a>
      <h1>${esc(s.name)} <span class="muted">${esc(s.year)}</span></h1>
      <div class="path muted">${esc(s.path)} · TVDB ${s.tvdb_id}${s.imdb_id ? " · " + esc(s.imdb_id) : ""}</div></div>
      <div class="spacer"></div>
      <button class="btn" id="b-refs" title="Download reference subtitles/transcripts for episodes that don't have one yet">Fetch references</button>
      <button class="btn primary" id="b-scan">Scan files</button>
    </div>
    <div class="small muted" style="margin-top:6px">References: ${s.reference_count} / ${s.episode_count} episodes
      ${s.last_scan ? ` · last scan ${esc(when(s.last_scan.created))}` : ""}</div>
    <div id="job"></div>
    <div class="tabs">
      <a data-tab="plan">Plan</a><a data-tab="episodes">Episodes &amp; references</a>
      <a data-tab="history">History</a><a data-tab="options">Series options</a>
    </div>
    <div id="tab"></div>`;
  $("#b-scan").onclick = () => startJob("scan");
  $("#b-refs").onclick = () => startJob("fetch_refs");
  $$(".tabs a").forEach((a) => a.onclick = () => { state.tab = a.dataset.tab; showTab(); });
  showTab();
  watchJobs();
}

async function startJob(kind, params = {}) {
  try {
    await api(`/api/series/${state.series.id}/jobs`, { method: "POST", body: { kind, params } });
    watchJobs();
  } catch (e) { fail(e); }
}

async function watchJobs() {
  clearInterval(pollTimer);
  const sid = state.series.id;
  let lastId = null, wasActive = false, lastSig = "", pinned = true, scrollPos = 0;
  const tick = async () => {
    if (!state.series || state.series.id !== sid) return clearInterval(pollTimer);
    const list = await api(`/api/jobs?series_id=${sid}`).catch(() => []);
    const j = list[0];
    const box = $("#job");
    if (!box) return clearInterval(pollTimer);
    if (!j) { box.innerHTML = ""; return; }
    const active = j.status === "queued" || j.status === "running";
    const full = await api(`/api/jobs/${j.id}?tail=300`);
    const sig = `${j.id}|${j.status}|${full.progress}|${full.message}|${full.log_lines}`;
    if (sig === lastSig && box.firstChild) { schedule(active); return; }  // nothing new
    lastSig = sig;
    const pct = Math.round((full.progress || 0) * 100);
    const badge = { done: "b-ok", failed: "b-bad", cancelled: "", running: "b-info", queued: "" }[j.status];
    box.innerHTML = `<div class="card job">
      <div class="row"><b>${esc(labelJob(j.kind))}</b>
        <span class="badge ${badge}">${esc(j.status)}</span>
        <span class="muted small">${esc(full.message || "")}</span><div class="spacer"></div>
        ${active ? `<button class="btn small danger" id="cancel-job">Cancel</button>` :
          `<button class="btn small" id="hide-job">Hide</button>`}</div>
      ${active ? `<div class="bar" style="margin-top:8px"><div style="width:${pct}%"></div></div>` : ""}
      <pre id="joblog">${full.log_lines > 300 ? `… ${full.log_lines - 300} earlier lines\n` : ""}${esc(full.log)}</pre></div>`;
    const pre = $("#joblog");
    pre.scrollTop = pre.scrollHeight;
    pre.onscroll = () => { pinned = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20; };
    if (!pinned) pre.scrollTop = scrollPos;
    pre.addEventListener("scroll", () => { scrollPos = pre.scrollTop; });
    const c = $("#cancel-job");
    if (c) c.onclick = () => api(`/api/jobs/${j.id}/cancel`, { method: "POST" });
    const hb = $("#hide-job");
    if (hb) hb.onclick = () => { box.innerHTML = ""; clearInterval(pollTimer); };
    if (wasActive && !active && j.id === lastId) {
      // a job just finished: re-render so counts, plan and references are fresh
      clearInterval(pollTimer);
      await renderSeries(sid);
      return;
    }
    wasActive = active; lastId = j.id;
    schedule(active);
  };
  // Poll again only after the previous poll finished: with a fixed timer, requests
  // piled up whenever the server was busy, which made the page slower still.
  const schedule = (active) => {
    clearTimeout(pollTimer);
    if (active && state.series && state.series.id === sid) pollTimer = setTimeout(tick, 1500);
  };
  await tick();
}

function labelJob(kind) {
  return { scan: "Scan", fetch_refs: "Fetch references", refresh_episodes: "Load TVDB episodes",
    apply: "Apply changes", undo: "Undo", replan: "Rebuild plan", ai_check: "Ask AI" }[kind] || kind;
}

function showTab() {
  $$(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === state.tab));
  ({ plan: tabPlan, episodes: tabEpisodes, history: tabHistory, options: tabOptions })[state.tab]()
    .catch(fail);
}

// --------------------------------------------------------------------- plan

function segHtml(sg, eps) {
  const conf = sg.override ? "manual" : sg.confidence;
  const cls = conf === "high" ? "b-ok" : conf === "manual" ? "b-info" : "b-warn";
  let h = `<span class="seg"><span class="badge ${cls}" title="score ${sg.score} · margin ${sg.margin ?? ""}">${
    esc(sg.code || "?")}</span> <span class="muted small">${fmtTime(sg.start)}–${fmtTime(sg.end)}</span>`;
  if (sg.code && eps[sg.code]) {
    const e = eps[sg.code];
    const shaky = (e.ref_note || "").includes("⚠");
    h += ` <span class="small">${esc(e.title)}</span>` + (e.ref ?
      ` <span class="small ${shaky ? "" : "muted"}" style="${shaky ? "color:var(--warn)" : ""}" title="${esc(e.ref_note || "")}">· ref: ${esc(e.ref.split(":")[0])}${shaky ? " ⚠" : ""}</span>` : "");
  }
  if (sg.why && (sg.confidence !== "high" || sg.evidence === "title+filename"))
    h += `<div class="small" style="color:var(--warn)">${esc(sg.why)}</div>`;
  else if (sg.confidence !== "high" && sg.alternatives && sg.alternatives.length)
    h += ` <span class="muted small">(or ${sg.alternatives.slice(0, 2).map((a) => esc(a.code)).join(", ")})</span>`;
  if (sg.title_card) {
    const tc = sg.title_card, conflict = sg.confidence === "conflict";
    h += `<div class="small"><span class="badge ${conflict ? "b-warn" : "b-ok"}" title="OCR score ${tc.score}">title card</span>
      <span class="muted">“${esc(tc.text)}” at ${fmtTime(tc.time)} → ${esc(tc.code)}${tc.partial ? " (partial read)" : ""}${
      sg.dialogue_code && !conflict ? ` (dialogue said ${esc(sg.dialogue_code)})` : ""}</span></div>`;
  }
  if (sg.llm) h += `<div class="small"><span class="badge b-ai">AI → ${esc(sg.llm.code)} ${Math.round(sg.llm.confidence * 100)}%</span>
    <span class="muted">${esc(sg.llm.reason)}</span></div>`;
  return h + `</span>`;
}

async function tabPlan(reload = true) {
  if (reload || state.ai === undefined) state.ai = !!(await api("/api/status").catch(() => ({}))).llm;
  const plan = reload || !state.plan ? await api(`/api/series/${state.series.id}/plan`) : state.plan;
  state.plan = plan;
  const tab = $("#tab");
  if (!plan.id) {
    tab.innerHTML = `<div class="empty">No scan yet.<br><br>1. <b>Fetch references</b> downloads what each episode should say.<br>
      2. <b>Scan files</b> reads every file's subtitles (or transcribes it) and works out which episode it really is.</div>`;
    return;
  }
  const eps = Object.fromEntries((state.series.episodes || []).map((e) => [e.code, e]));
  const counts = {};
  plan.items.forEach((it) => counts[it.kind] = (counts[it.kind] || 0) + 1);
  const actionable = plan.items.filter((it) => ACTIONABLE.includes(it.kind));
  const filters = [["actions", "Changes", actionable.length], ["review", "Needs review", counts.review || 0],
    ["ok", "Correct", counts.ok || 0], ["all", "All", plan.items.length]];
  const shown = plan.items.filter((it) => state.filter === "all" ? true :
    state.filter === "actions" ? ACTIONABLE.includes(it.kind) : it.kind === state.filter);
  const applied = plan.status !== "draft";
  const selCount = actionable.filter((it) => it.selected).length;

  tab.innerHTML = `<div class="row" style="margin-bottom:10px">
      <div class="filters">${filters.map(([k, l, n]) =>
        `<button class="btn small ${state.filter === k ? "active" : ""}" data-f="${k}">${l} (${n})</button>`).join("")}</div>
      <div class="spacer"></div>
      <button class="btn small" id="sonarr-cmp" title="Check EpisodeID's view of this series' files against Sonarr's">Compare with Sonarr</button>
      <button class="btn small" id="unmon" title="Seasons where every aired episode has a correct file: stop Sonarr searching for or upgrading them">Unmonitor complete seasons…</button>
      <span class="muted small">Plan #${plan.id} · ${esc(when(plan.created))}${applied ? " · <b>applied</b>" : ""}</span>
      ${applied ? "" : `<button class="btn primary" id="apply" ${selCount ? "" : "disabled"}>Apply ${selCount} change${selCount === 1 ? "" : "s"}…</button>`}
    </div>
    ${applied ? "" : coverageHtml(plan, state.series)}
    ${shown.length ? `<table><thead><tr>
      <th class="check">${applied ? "" : `<input type="checkbox" id="all-sel" title="Select all">`}</th>
      <th>File → result</th><th>Detected in the file</th><th style="width:150px"></th></tr></thead><tbody>
      ${shown.map((it) => rowHtml(it, eps, applied)).join("")}</tbody></table>` :
      `<div class="empty">Nothing here.</div>`}`;

  $$(".filters .btn", tab).forEach((b) => b.onclick = () => { state.filter = b.dataset.f; tabPlan(false); });
  const cov = $("#coverage", tab);
  if (cov) cov.ontoggle = () => { state.coverageOpen = cov.open; };
  $$("input.sel", tab).forEach((cb) => cb.onchange = async () => {
    await api(`/api/plans/${plan.id}/selection`, { method: "PATCH",
      body: { selected: { [cb.dataset.id]: cb.checked } } }).catch(fail);
    tabPlan();
  });
  const all = $("#all-sel", tab);
  if (all) {
    const boxes = $$("input.sel", tab);
    all.checked = boxes.length && boxes.every((b) => b.checked);
    all.onchange = async () => {
      const sel = Object.fromEntries(boxes.map((b) => [b.dataset.id, all.checked]));
      await api(`/api/plans/${plan.id}/selection`, { method: "PATCH", body: { selected: sel } }).catch(fail);
      tabPlan();
    };
  }
  $$("button.set-ep", tab).forEach((b) => b.onclick = () => setEpisodeDialog(b.dataset.src, b.dataset.cur));
  $$("button.card-check", tab).forEach((b) => b.onclick = () => cardCheckDialog(b.dataset.src));
  $$("button.ai-check", tab).forEach((b) => b.onclick = () => { startJob("ai_check", { source: b.dataset.src }); toast("Asking the AI — see the job log; the plan updates when it answers."); });
  $$("button.cut-pick", tab).forEach((b) => b.onclick = () =>
    cutDialog(plan, plan.items.find((x) => String(x.id) === b.dataset.iid)));
  $$("button.clear-ov", tab).forEach((b) => b.onclick = async () => {
    await api(`/api/series/${state.series.id}/overrides`, { method: "POST",
      body: { source: b.dataset.src, codes: null } }).catch(fail);
    tabPlan();
  });
  const ap = $("#apply", tab);
  if (ap) ap.onclick = () => confirmApply(plan, actionable.filter((it) => it.selected));
  $("#sonarr-cmp", tab).onclick = () => sonarrCompareDialog();
  $("#unmon", tab).onclick = () => unmonitorDialog();
}

// Which episodes have a file now, and which will after applying the ticked changes.
function coverage(plan, series) {
  const today = new Date().toISOString().slice(0, 10);
  const withSpecials = !!(series.options || {}).include_specials;
  const all = (series.episodes || []).filter((e) => e.season > 0 || withSpecials);
  const dated = all.some((e) => e.aired);  // no dates at all (older data): count everything
  const aired = all.filter((e) => !dated || (e.aired && e.aired <= today));
  const now = new Map(), after = new Map();
  const add = (m, code, src) => { if (!m.has(code)) m.set(code, []); m.get(code).push(src); };
  for (const it of plan.items) {
    (it.expected || []).forEach((c) => add(now, c, it.source));
    const acting = it.selected && ACTIONABLE.includes(it.kind);
    if (!acting) { (it.expected || []).forEach((c) => add(after, c, it.source)); continue; }
    for (const t of it.targets || []) {
      if (t.duplicate) continue;
      (t.codes || []).forEach((c) => add(after, c, t.path));
    }
  }
  const codes = new Set(aired.map((e) => e.code));
  return {
    aired,
    nowHave: aired.filter((e) => now.has(e.code)).length,
    afterHave: aired.filter((e) => after.has(e.code)).length,
    lost: aired.filter((e) => now.has(e.code) && !after.has(e.code)),
    gained: aired.filter((e) => !now.has(e.code) && after.has(e.code)),
    missing: aired.filter((e) => !now.has(e.code) && !after.has(e.code)),
    doubled: [...after.entries()].filter(([c, files]) => codes.has(c) && files.length > 1)
      .map(([c, files]) => ({ ep: aired.find((e) => e.code === c), files })),
  };
}

function coverageHtml(plan, series) {
  const c = coverage(plan, series);
  if (!c.aired.length) return "";
  const list = (eps) => {
    const bySeason = {};
    eps.forEach((e) => (bySeason[e.season] = bySeason[e.season] || []).push(e));
    return Object.entries(bySeason).map(([s, es]) =>
      `<div class="small" style="margin:2px 0"><b>${+s === 0 ? "Specials" : `Season ${s}`}:</b> ${es.map((e) =>
        `<span class="mono">${esc(e.code)}</span> ${esc(e.title || "")}`).join(" · ")}</div>`).join("");
  };
  const diff = c.afterHave - c.nowHave;
  const sec = (cls, title, eps, note = "") => eps.length ? `<div style="margin-top:8px">
    <span class="badge ${cls}">${title} (${eps.length})</span>${note ? ` <span class="muted small">${note}</span>` : ""}${list(eps)}</div>` : "";
  return `<details class="card" id="coverage" style="margin-bottom:12px" ${state.coverageOpen ? "open" : ""}>
    <summary style="cursor:pointer"><b>Episodes after applying:</b> ${c.afterHave} of ${c.aired.length} aired episodes have a file
      <span class="muted small">(now ${c.nowHave}${diff ? `, ${diff > 0 ? "+" : ""}${diff}` : ""})</span>
      ${c.lost.length ? ` <span class="badge b-bad">${c.lost.length} lost</span>` : ""}
      ${c.gained.length ? ` <span class="badge b-ok">${c.gained.length} gained</span>` : ""}
      ${c.missing.length ? ` <span class="badge">${c.missing.length} missing</span>` : ""}
      ${c.doubled.length ? ` <span class="badge b-warn">${c.doubled.length} doubled</span>` : ""}</summary>
    <div class="muted small" style="margin-top:6px">Worked out from the ticked changes — tick or untick rows to see the effect.
      Episodes that haven't aired yet${(series.options || {}).include_specials ? "" : " and specials"} aren't counted.</div>
    ${sec("b-bad", "Lost by this plan", c.lost, "have a file now but won't after applying — check these")}
    ${sec("b-ok", "Gained", c.gained)}
    ${sec("", "Still missing", c.missing, "no file before or after")}
    ${c.doubled.length ? `<div style="margin-top:8px"><span class="badge b-warn">Two or more files (${c.doubled.length})</span>
      ${c.doubled.map((d) => `<div class="small" style="margin:2px 0"><span class="mono">${esc(d.ep.code)}</span> ${esc(d.ep.title || "")}:
        ${d.files.map((f) => `<span class="path">${esc(f)}</span>`).join(", ")}</div>`).join("")}</div>` : ""}
  </details>`;
}

function rowHtml(it, eps, applied) {
  const actionable = ACTIONABLE.includes(it.kind);
  const targets = (it.targets || []).map((t) =>
    `<div class="arrow">↳ <span class="path">${esc(t.path.replace(/^(\.\.\/)+/, ""))}</span>${t.duplicate ? ' <span class="badge b-bad">duplicate</span>' : ""}</div>`).join("");
  const segs = (it.segments || []).map((sg) => segHtml(sg, eps)).join("<br>") ||
    `<span class="muted small">${esc(it.text_source === "none" ? "no subtitles / transcript" : "no match")}</span>`;
  const src = it.text_source && it.text_source !== "none" ?
    `<div class="muted small">text: ${esc(it.text_source)}</div>` : "";
  const ovr = it.status === "MANUAL";
  const cur = (it.codes || it.suggested || []).join(" ");
  return `<tr>
    <td class="check">${actionable && !applied ? `<input type="checkbox" class="sel" data-id="${it.id}" ${it.selected ? "checked" : ""}>` : ""}</td>
    <td><span class="badge ${KIND_BADGE[it.kind]}">${esc(it.kind)}</span>
      <span class="badge ${STATUS_BADGE[it.status] || ""}">${esc(it.status)}</span>
      ${it.ai ? '<span class="badge b-ai">AI</span>' : ""}
      ${it.ref_warning ? '<span class="badge b-warn">check reference</span>' : ""}
      ${((state.series.file_check || {}).imports || []).some((x) => (it.sources || [it.source]).includes(x.rel)) ? '<span class="badge b-bad" title="Sonarr imported this file recently and its content doesn\'t match its name">new download</span>' : ""}
      ${(it.sources || [it.source]).map((p) => `<div class="path" style="margin-top:4px">${it.sources ? "+ " : ""}${esc(p)}</div>`).join("")}${targets}
      ${it.kind === "split" && it.cuts ? `<div class="small" style="margin-top:2px"><span class="badge b-info">cut points chosen</span>
        ${it.cuts.map((c) => c == null ? "auto" : fmtCut(c)).join(", ")}</div>` : ""}
      <div class="muted small">${esc(it.reason || "")}</div></td>
    <td>${segs}${src}</td>
    <td>${applied ? "" : `<button class="btn small set-ep" data-src="${esc(it.source)}" data-cur="${esc(cur)}">Set episode…</button>
      ${ovr ? `<button class="btn small clear-ov" data-src="${esc(it.source)}">Clear</button>` : ""}`}
      <button class="btn small card-check" data-src="${esc(it.source)}" style="margin-top:4px" title="See what the title-card OCR reads in part of this file">Check title cards…</button>
      ${state.ai && !applied && it.kind !== "ok" && it.text_source && it.text_source !== "none" ? `<button class="btn small ai-check" data-src="${esc(it.source)}" style="margin-top:4px" title="Ask the AI which episode this file's dialogue is, from the TVDB summaries of the likely episodes">Ask AI</button>` : ""}
      ${it.kind === "split" && !applied ? `<button class="btn small cut-pick" data-iid="${it.id}" style="margin-top:4px" title="See where this file would be split, with pictures, and choose">Choose cut points…</button>` : ""}</td>
  </tr>`;
}

// Full-size frame, with stepping to find the exact moment one episode ends.
function frameViewer(sid, source, t) {
  document.querySelector(".lightbox")?.remove();
  const lb = document.createElement("div");
  lb.className = "lightbox";
  lb.innerHTML = `<div class="lb-box">
      <img alt="">
      <div class="row lb-bar">
        <button class="btn small" data-d="-1">−1 s</button><button class="btn small" data-d="-0.1">−0.1 s</button>
        <b class="mono lb-t"></b>
        <button class="btn small" data-d="0.1">+0.1 s</button><button class="btn small" data-d="1">+1 s</button>
        <div class="spacer"></div><span class="small lb-hint">← → step · Esc closes</span>
        <button class="btn small lb-close">Close</button></div></div>`;
  document.body.append(lb);
  const im = $("img", lb), label = $(".lb-t", lb);
  const go = (x) => {
    t = Math.max(0, Math.round(x * 10) / 10);
    label.textContent = fmtCut(t);
    im.style.opacity = ".5";
    im.onload = () => { im.style.opacity = "1"; };
    im.src = `/api/series/${sid}/frame?source=${encodeURIComponent(source)}&t=${t.toFixed(2)}&w=960`;
  };
  const close = () => { lb.remove(); document.removeEventListener("keydown", key, true); };
  const key = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
    else if (e.key === "ArrowLeft") go(t - (e.shiftKey ? 1 : 0.1));
    else if (e.key === "ArrowRight") go(t + (e.shiftKey ? 1 : 0.1));
  };
  document.addEventListener("keydown", key, true);
  lb.onclick = (e) => { if (e.target === lb) close(); };
  $(".lb-close", lb).onclick = close;
  $$("button[data-d]", lb).forEach((b) => b.onclick = () => go(t + Number(b.dataset.d)));
  go(t);
}

function cutDialog(plan, it) {
  const sid = state.series.id;
  const img = (t, w = 280) => `/api/series/${sid}/frame?source=${encodeURIComponent(it.source)}&t=${Math.max(0, t).toFixed(2)}&w=${w}`;
  const box = modal(`<h2 style="margin-top:0">Choose cut points</h2>
    <div class="path">${esc(it.source)}</div>
    <p class="muted small">Where this file will be split. EpisodeID looks for black frames and silence near the point
      where the dialogue changes episode; each place is moved to a keyframe, because a lossless split can only start on one.
      The pictures show the last moment before the cut and the first after it — click one to see it full size.</p>
    <div id="cuts-body"><span class="muted small">Looking for black frames and silence… (a few seconds per split)</span></div>
    <div class="row" style="margin-top:12px"><button class="btn" id="cut-auto" title="Forget any chosen cut points">Use automatic</button>
      <div class="spacer"></div><button class="btn" id="c">Cancel</button><button class="btn primary" id="cut-save" disabled>Save</button></div>`);
  $("#modal-box").style.width = "min(980px, 100%)";
  $("#c", box).onclick = closeModal;
  const save = async (cuts) => {
    try {
      await api(`/api/plans/${plan.id}/items/${it.id}/cuts`, { method: "PUT", body: { cuts } });
      closeModal(); tabPlan();
    } catch (e) { fail(e); }
  };
  api(`/api/plans/${plan.id}/items/${it.id}/cut-candidates`).then((r) => {
    const body = $("#cuts-body", box);
    const name = (xs) => xs.map(([c, t]) => `<span class="mono">${esc(c)}</span> ${esc(t || "")}`).join(" + ");
    const card = (n, c, chosen) => {
      const before = c.gap ? c.gap[0] - 0.6 : c.t - 1, after = c.gap ? c.gap[1] + 0.3 : c.t + 0.3;
      const what = c.kind === "estimate" ? "no gap found — estimate" :
        `${esc(c.kind)} ${(c.gap[1] - c.gap[0]).toFixed(1)} s${c.in_gap ? "" : " (nearest keyframe is outside it)"}`;
      return `<label class="card cut-card" style="padding:8px;cursor:pointer;display:block">
        <div class="row" style="gap:6px"><input type="radio" name="b${n}" value="${c.t}" ${chosen ? "checked" : ""}>
          <b class="mono">${fmtCut(c.t)}</b> <span class="small muted">${what}</span>
          ${c.auto ? '<span class="badge b-info">automatic pick</span>' : ""}</div>
        <div class="row" style="gap:4px;margin-top:6px;flex-wrap:nowrap">
          <figure style="margin:0;flex:1"><img class="zoomable" data-t="${before}" loading="lazy" src="${img(before)}" style="width:100%;border-radius:4px;background:#000;aspect-ratio:16/9;object-fit:contain">
            <figcaption class="small muted">before · ${fmtCut(before)}</figcaption></figure>
          <figure style="margin:0;flex:1"><img class="zoomable" data-t="${after}" loading="lazy" src="${img(after)}" style="width:100%;border-radius:4px;background:#000;aspect-ratio:16/9;object-fit:contain">
            <figcaption class="small muted">after · ${fmtCut(after)}</figcaption></figure></div></label>`;
    };
    body.innerHTML = r.boundaries.map((b, n) => {
      const pick = b.chosen ?? b.auto;
      const custom = b.chosen != null && !b.candidates.some((c) => Math.abs(c.t - b.chosen) < 0.05);
      return `<div style="margin-bottom:16px"><h3 style="margin:8px 0 2px;font-size:14px">${name(b.from)} → ${name(b.to)}</h3>
        <div class="small muted" style="margin-bottom:6px">The dialogue changes episode between ${fmtCut(b.prev_end)} and ${fmtCut(b.next_start)};
          searched ${fmtCut(b.window[0])}–${fmtCut(b.window[1])}.</div>
        <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px">
          ${b.candidates.map((c) => card(n, c, !custom && Math.abs(c.t - pick) < 0.05)).join("")}
          <div class="card" style="padding:8px">
            <div class="row" style="gap:6px"><input type="radio" name="b${n}" value="custom" id="cr${n}" ${custom ? "checked" : ""}>
              <b>Your own time</b></div>
            <div class="row" style="gap:4px;margin-top:6px">
              <input id="ct${n}" value="${fmtCut(custom ? b.chosen : b.auto)}" size="8" class="mono">
              ${[-5, -1, 1, 5].map((d) => `<button class="btn small nudge" data-n="${n}" data-d="${d}" type="button">${d > 0 ? "+" : ""}${d}s</button>`).join("")}
              <button class="btn small show" data-n="${n}" type="button">Show</button></div>
            <div class="small muted" id="cs${n}" style="margin-top:4px">${custom ? `keyframe ${fmtCut(b.chosen)}` : "moved to the nearest keyframe when you press Show"}</div>
            <div class="row" style="gap:4px;margin-top:6px;flex-wrap:nowrap" id="cp${n}">${custom ? `
              <img class="zoomable" data-t="${b.chosen - 1}" src="${img(b.chosen - 1)}" style="width:50%;border-radius:4px;background:#000;aspect-ratio:16/9;object-fit:contain">
              <img class="zoomable" data-t="${b.chosen + 0.3}" src="${img(b.chosen + 0.3)}" style="width:50%;border-radius:4px;background:#000;aspect-ratio:16/9;object-fit:contain">` : ""}</div>
          </div></div></div>`;
    }).join("");
    const customT = {};
    r.boundaries.forEach((b, n) => {
      const custom = b.chosen != null && !b.candidates.some((c) => Math.abs(c.t - b.chosen) < 0.05);
      if (custom) customT[n] = b.chosen;
    });
    const show = async (n) => {
      const t = parseCut($(`#ct${n}`, box).value);
      if (isNaN(t)) return toast("Enter a time like 11:42.5");
      $(`#cs${n}`, box).textContent = "Finding the nearest keyframe…";
      try {
        const s = await api(`/api/plans/${plan.id}/items/${it.id}/snap?t=${t}`);
        customT[n] = s.t;
        $(`#ct${n}`, box).value = fmtCut(s.t);
        $(`#cs${n}`, box).textContent = Math.abs(s.t - t) > 0.05 ? `moved to the nearest keyframe, ${fmtCut(s.t)}` : `keyframe ${fmtCut(s.t)}`;
        $(`#cp${n}`, box).innerHTML = [s.t - 1, s.t + 0.3].map((x) =>
          `<img class="zoomable" data-t="${x}" src="${img(x)}" style="width:50%;border-radius:4px;background:#000;aspect-ratio:16/9;object-fit:contain">`).join("");
        $(`#cr${n}`, box).checked = true;
      } catch (e) { $(`#cs${n}`, box).textContent = e.message; }
    };
    $$(".nudge", box).forEach((bt) => bt.onclick = () => {
      const n = bt.dataset.n, i = $(`#ct${n}`, box);
      const t = parseCut(i.value);
      if (!isNaN(t)) { i.value = fmtCut(Math.max(0, t + Number(bt.dataset.d))); show(n); }
    });
    $$(".show", box).forEach((bt) => bt.onclick = () => show(bt.dataset.n));
    body.addEventListener("click", (e) => {
      const im = e.target.closest("img.zoomable");
      if (!im) return;
      e.preventDefault(); e.stopPropagation();  // don't also tick the candidate
      frameViewer(sid, it.source, Number(im.dataset.t));
    });
    const sv = $("#cut-save", box);
    sv.disabled = false;
    sv.onclick = () => {
      const cuts = r.boundaries.map((b, n) => {
        const v = (box.querySelector(`input[name="b${n}"]:checked`) || {}).value;
        if (v === "custom") return customT[n] ?? null;
        return v == null ? null : Number(v);
      });
      if (cuts.some((c, n) => c != null && n > 0 && cuts[n - 1] != null && c <= cuts[n - 1]))
        return toast("Cut points must be in order");
      save(cuts);
    };
    $("#cut-auto", box).onclick = () => save(r.boundaries.map(() => null));
  }).catch((e) => { $("#cuts-body", box).innerHTML = `<span class="badge b-bad">Failed</span> ${esc(e.message)}`; });
}

// "m:ss" or "h:mm:ss"; a number on its own is minutes ("12" → 12:00, "1.5" → 1:30).
function parseTime(v) {
  const t = String(v).trim();
  if (t === "") return NaN;
  if (!t.includes(":")) { const m = Number(t); return isNaN(m) ? NaN : Math.round(m * 60); }
  const p = t.split(":").map(Number);
  if (p.some(isNaN)) return NaN;
  return p.reduce((a, x) => a * 60 + x, 0);
}

function cardCheckDialog(source) {
  const box = modal(`<div class="row" style="align-items:flex-start"><h2 style="margin:0">Check title cards</h2>
      <div class="spacer"></div><button class="btn small" id="cc-x" title="Close" aria-label="Close">✕</button></div>
    <div class="path">${esc(source)}</div>
    <p class="muted small">Reads every sampled frame in the range (up to 3 minutes) and shows the OCR text and
      what it matched — nothing is cached or skipped. Try both decoders if a card is being missed.</p>
    <div class="row">
      <label class="small" title="m:ss — or just minutes: 12 = 12:00">From <input id="cc-a" value="0:00" size="6"></label>
      <label class="small" title="Set to From + 1:30 whenever From changes">to <input id="cc-b" value="1:30" size="6"></label>
      <label class="small">Decode <select id="cc-d"><option value="auto">as the scan does</option><option value="cpu">CPU</option></select></label>
      <label class="small">Frames <select id="cc-m"><option value="full">all (1 per second)</option><option value="key">keyframes only</option></select></label>
      <div class="spacer"></div><button class="btn primary" id="cc-go">Read</button></div>
    <div id="cc-out" style="margin-top:12px"></div>
    <div class="row" style="margin-top:12px"><button class="btn" id="cc-copy" disabled>Copy as text</button>
      <div class="spacer"></div><button class="btn" id="c">Close</button></div>`, { sticky: true });
  $("#modal-box").style.width = "min(960px, 100%)";
  let text = "";
  $("#c", box).onclick = closeModal;
  $("#cc-x", box).onclick = closeModal;
  // From: tidy what was typed ("12" → "12:00") and put To 90 s after it.
  const from = $("#cc-a", box), to = $("#cc-b", box);
  let lastFrom = from.value;
  from.addEventListener("change", () => {
    const t = parseTime(from.value);
    if (isNaN(t)) return;
    from.value = fmtTime(t);
    if (from.value !== lastFrom) to.value = fmtTime(t + 90);
    lastFrom = from.value;
  });
  to.addEventListener("change", () => { const t = parseTime(to.value); if (!isNaN(t)) to.value = fmtTime(t); });
  $("#cc-copy", box).onclick = async () => {
    try { await navigator.clipboard.writeText(text); toast("Copied"); }
    catch { const t = document.createElement("textarea"); t.value = text; document.body.append(t); t.select(); document.execCommand("copy"); t.remove(); toast("Copied"); }
  };
  $("#cc-go", box).onclick = async () => {
    const start = parseTime($("#cc-a", box).value), end = parseTime($("#cc-b", box).value);
    if (isNaN(start) || isNaN(end) || end <= start) return toast("Enter times like 0:30 and 1:30");
    const out = $("#cc-out", box);
    out.innerHTML = '<span class="muted small">Reading… (about a second per frame)</span>';
    $("#cc-go", box).disabled = true;
    try {
      const r = await api(`/api/series/${state.series.id}/titlecard-check`, { method: "POST",
        body: { source, start, end, decode: $("#cc-d", box).value, mode: $("#cc-m", box).value } });
      const hitTxt = (h) => !h ? "" : h.partial ? `partial → ${(h.candidates || []).slice(0, 4).join(", ")}` : `→ ${h.code} ${h.title}`;
      const lineTxt = (l) => `${l[0]} (conf ${l[1]}, height ${l[2]})`;
      text = `EpisodeID title card check — ${source}\nrange ${fmtTime(start)}–${fmtTime(end)}, decode ${r.decode}, ${r.mode} frames, ${r.fps} fps` +
        ` (counts: conf ≥ ${r.min_conf}, line height ≥ ${r.min_line}, a title needs one line ≥ ${r.min_title_line})\n` +
        r.frames.map((f) => `${fmtTime(f.t)}  ${f.size.join("x")}  ${f.lines.map(lineTxt).join(" | ") || "—"}  ${hitTxt(f.hit)}`).join("\n");
      out.innerHTML = `<div class="small muted">Decoded on ${esc(r.decode.toUpperCase())}, ${r.frames.length} frames.</div>
        <div style="max-height:50vh;overflow:auto"><table><tr><th>Time</th><th>Text read (confidence, height)</th><th>Match</th></tr>
        ${r.frames.map((f) => `<tr><td class="mono">${fmtTime(f.t)}</td>
          <td class="small">${f.lines.map((l) => `${esc(l[0])} <span class="muted">(${l[1]}, ${l[2]})</span>`).join("<br>") || '<span class="muted">—</span>'}</td>
          <td class="small">${f.hit ? `<span class="badge ${f.hit.partial ? "b-warn" : "b-ok"}">${esc(f.hit.partial ? "partial" : f.hit.code)}</span> ${esc(f.hit.partial ? (f.hit.candidates || []).slice(0, 4).join(", ") : f.hit.title)}` : ""}</td></tr>`).join("")}
        </table></div>`;
      $("#cc-copy", box).disabled = false;
    } catch (e) { out.innerHTML = `<span class="badge b-bad">Failed</span> ${esc(e.message)}`; }
    $("#cc-go", box).disabled = false;
  };
}

function setEpisodeDialog(source, current) {
  const box = modal(`<h2 style="margin-top:0">Set episode</h2>
    <div class="path">${esc(source)}</div>
    <p class="muted small">Type the episode code(s) this file really contains, in the order they play —
      e.g. <span class="mono">S02E05</span>, or <span class="mono">S02E05E06</span> / <span class="mono">S02E05 S02E06</span> for a two-part file.
      If the scan found the same number of segments, each segment gets one code (splitting if needed).</p>
    <input type="text" id="codes" value="${esc(current)}" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg)">
    <div class="row" style="margin-top:12px"><div class="spacer"></div>
      <button class="btn" id="c">Cancel</button><button class="btn primary" id="ok">Save</button></div>`);
  $("#c", box).onclick = closeModal;
  const save = async () => {
    const codes = $("#codes", box).value.split(/[\s,+]+/).filter(Boolean);
    try {
      await api(`/api/series/${state.series.id}/overrides`, { method: "POST", body: { source, codes } });
      closeModal(); tabPlan();
    } catch (e) { fail(e); }
  };
  $("#ok", box).onclick = save;
  $("#codes", box).onkeydown = (e) => { if (e.key === "Enter") save(); };
  $("#codes", box).focus();
}

const UNMON_NOTE = `<p class="muted small">A season qualifies when every aired episode has a file EpisodeID found correct, the plan
  has nothing left to change or review in it, no file in it changed since the last scan, and Sonarr has a file for every episode.
  Unmonitoring stops Sonarr searching for and upgrading those episodes — a later "upgrade" can't bring in a wrongly labelled
  release. Turn monitoring back on in Sonarr whenever you like.</p>`;

function unmonitorDialog() {
  const box = modal(`<div class="row" style="align-items:flex-start"><h2 style="margin:0">Unmonitor complete seasons</h2>
      <div class="spacer"></div><button class="btn small" id="um-x" aria-label="Close">✕</button></div>
    ${UNMON_NOTE}<div id="um-body" class="muted">Checking…</div>`);
  $("#modal-box").style.width = "min(860px, 100%)";
  $("#um-x", box).onclick = closeModal;
  api(`/api/series/${state.series.id}/seasons`).then((r) => {
    const body = $("#um-body", box);
    body.classList.remove("muted");
    const rows = r.seasons.map((x) => `<tr>
      <td class="check">${x.eligible ? `<input type="checkbox" class="um" value="${x.season}" checked>` : ""}</td>
      <td style="white-space:nowrap">${x.season === 0 ? "Specials" : `Season ${x.season}`}</td><td class="small">${x.have} / ${x.episodes}</td>
      <td class="small">${x.monitored === false ? '<span class="badge">unmonitored</span>' : x.monitored ? '<span class="badge b-info">monitored</span>' : ""}</td>
      <td class="small">${x.complete ? '<span class="badge b-ok">complete and correct</span> ' : ""}${esc(x.why)}</td></tr>`).join("");
    body.innerHTML = (r.sonarr !== "ok" ? `<p><span class="badge b-bad">Sonarr</span> ${esc(r.sonarr)}</p>` : "") +
      `<div style="max-height:55vh;overflow:auto"><table><tr><th></th><th>Season</th><th>Files</th><th>Sonarr</th><th></th></tr>${rows}</table></div>
      <div class="row" style="margin-top:12px"><span class="small muted" id="um-out"></span><div class="spacer"></div>
        <button class="btn" id="um-c">Close</button><button class="btn primary" id="um-go">Unmonitor ticked seasons</button></div>`;
    $("#um-c", box).onclick = closeModal;
    const sync = () => { const n = $$("input.um", box).filter((c) => c.checked).length;
      $("#um-go", box).disabled = !n; $("#um-go", box).textContent = `Unmonitor ${n} season${n === 1 ? "" : "s"}`; };
    $$("input.um", box).forEach((c) => c.onchange = sync); sync();
    $("#um-go", box).onclick = async () => {
      const seasons = $$("input.um", box).filter((c) => c.checked).map((c) => +c.value);
      $("#um-go", box).disabled = true;
      try {
        const res = await api(`/api/series/${state.series.id}/unmonitor`, { method: "POST", body: { seasons } });
        toast(`Unmonitored ${res.seasons.length} season${res.seasons.length === 1 ? "" : "s"} in Sonarr` +
          (res.skipped.length ? ` (skipped ${res.skipped.join(", ")}: no longer complete)` : ""));
        closeModal();
      } catch (e) { $("#um-out", box).textContent = e.message; $("#um-go", box).disabled = false; }
    };
  }).catch((e) => { $("#um-body", box).innerHTML = `<span class="badge b-bad">Failed</span> ${esc(e.message)}`; });
}

function unmonitorAllDialog() {
  const box = modal(`<div class="row" style="align-items:flex-start"><h2 style="margin:0">Unmonitor complete seasons — all series</h2>
      <div class="spacer"></div><button class="btn small" id="um-x" aria-label="Close">✕</button></div>
    ${UNMON_NOTE}<div id="um-body" class="muted">Checking every series with Sonarr…</div>`);
  $("#modal-box").style.width = "min(860px, 100%)";
  $("#um-x", box).onclick = closeModal;
  api("/api/seasons").then((list) => {
    const body = $("#um-body", box);
    body.classList.remove("muted");
    const rows = list.map((s) => `<tr><td>${esc(s.name)}</td><td class="small">${s.complete} of ${s.seasons} complete</td>
      <td class="small">${s.sonarr !== "ok" ? `<span class="badge b-bad">Sonarr</span> ${esc(s.sonarr)}` :
        s.eligible.length ? s.eligible.map((n) => `<label style="margin-right:8px"><input type="checkbox" class="um" data-sid="${s.id}" value="${n}" checked>
          ${n === 0 ? "Specials" : `S${n}`}</label>`).join("") : '<span class="muted">nothing to unmonitor</span>'}</td></tr>`).join("");
    body.innerHTML = `<div style="max-height:55vh;overflow:auto"><table><tr><th>Series</th><th>Seasons</th><th>Still monitored in Sonarr, complete</th></tr>${rows}</table></div>
      <div class="row" style="margin-top:12px"><span class="small muted" id="um-out"></span><div class="spacer"></div>
        <button class="btn" id="um-c">Close</button><button class="btn primary" id="um-go">Unmonitor</button></div>`;
    $("#um-c", box).onclick = closeModal;
    const sync = () => { const n = $$("input.um", box).filter((c) => c.checked).length;
      $("#um-go", box).disabled = !n; $("#um-go", box).textContent = `Unmonitor ${n} season${n === 1 ? "" : "s"}`; };
    $$("input.um", box).forEach((c) => c.onchange = sync); sync();
    $("#um-go", box).onclick = async () => {
      const series = {};
      $$("input.um", box).filter((c) => c.checked).forEach((c) => (series[c.dataset.sid] = series[c.dataset.sid] || []).push(+c.value));
      $("#um-go", box).disabled = true;
      try {
        const res = await api("/api/unmonitor", { method: "POST", body: { series } });
        const n = res.reduce((a, r) => a + (r.seasons || []).length, 0);
        const errs = res.filter((r) => r.error);
        toast(`Unmonitored ${n} season${n === 1 ? "" : "s"} in Sonarr` + (errs.length ? ` — ${errs.length} series failed` : ""), 6000);
        closeModal();
      } catch (e) { $("#um-out", box).textContent = e.message; $("#um-go", box).disabled = false; }
    };
  }).catch((e) => { $("#um-body", box).innerHTML = `<span class="badge b-bad">Failed</span> ${esc(e.message)}`; });
}

function sonarrCompareDialog() {
  const box = modal(`<div class="row" style="align-items:flex-start"><h2 style="margin:0">EpisodeID vs Sonarr</h2>
      <div class="spacer"></div><button class="btn small" id="sc-x" aria-label="Close">✕</button></div>
    <div id="sc-body" class="muted" style="margin-top:10px">Asking Sonarr…</div>`);
  $("#modal-box").style.width = "min(960px, 100%)";
  $("#sc-x", box).onclick = closeModal;
  const KIND = { differs: ["b-warn", "different episodes"], not_imported: ["b-bad", "not in Sonarr"],
                 sonarr_only: ["b-bad", "only in Sonarr"] };
  api(`/api/series/${state.series.id}/sonarr-compare`).then((r) => {
    const head = `<p class="small">Sonarr has ${r.files.sonarr} file${r.files.sonarr === 1 ? "" : "s"} for
      <b>${esc(r.sonarr_title)}</b>; EpisodeID sees ${r.files.episodeid}. Files are matched by their path inside the series folder.
      ${r.sonarr_type && r.sonarr_type !== "standard" ? `<br><span class="badge b-warn">${esc(r.sonarr_type)}</span> Sonarr numbers this series differently, so differences are expected.` : ""}</p>`;
    $("#sc-body", box).classList.remove("muted");
    $("#sc-body", box).innerHTML = head + (r.rows.length ? `<div style="max-height:60vh;overflow:auto"><table>
      <tr><th>File</th><th>Difference</th></tr>${r.rows.map((x) => `<tr>
        <td class="path">${esc(x.rel)}</td>
        <td class="small"><span class="badge ${KIND[x.kind][0]}">${KIND[x.kind][1]}</span> ${esc(x.why)}</td></tr>`).join("")}
      </table></div>` : `<div class="badge b-ok">✓ They agree on every file</div>`);
  }).catch((e) => { $("#sc-body", box).innerHTML = `<span class="badge b-bad">Failed</span> ${esc(e.message)}`; });
}

function confirmApply(plan, items) {
  const n = (k) => items.filter((i) => i.kind === k).length;
  const box = modal(`<h2 style="margin-top:0">Apply ${items.length} changes?</h2>
    <ul><li>${n("rename")} renames</li><li>${n("split")} splits (lossless, original kept in the backup folder)</li>
    ${n("join") ? `<li>${n("join")} joins of multi-part files (lossless, parts kept in the backup folder)</li>` : ""}
    <li>${n("aside")} files moved to the backup folder</li></ul>
    <p class="muted small">Nothing is deleted. Every step is logged and can be undone from the History tab.</p>
    <p>Type <b>APPLY</b> to continue:</p>
    <input type="text" id="confirm" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg)">
    <div class="row" style="margin-top:12px"><div class="spacer"></div>
      <button class="btn" id="c">Cancel</button><button class="btn primary" id="go" disabled>Apply</button></div>`);
  $("#c", box).onclick = closeModal;
  $("#confirm", box).oninput = (e) => $("#go", box).disabled = e.target.value.trim() !== "APPLY";
  $("#go", box).onclick = async () => {
    try {
      await api(`/api/plans/${plan.id}/apply`, { method: "POST" });
      closeModal(); watchJobs();
    } catch (e) { fail(e); }
  };
  $("#confirm", box).focus();
}

// ----------------------------------------------------------------- episodes

async function tabEpisodes() {
  const s = state.series;
  const eps = s.episodes || [];
  const tab = $("#tab");
  const missing = eps.filter((e) => e.season > 0 && !e.ref).length;
  tab.innerHTML = `<div class="row" style="margin-bottom:10px">
      <span class="muted small">${missing} regular episodes without a reference.
        Missing ones can be uploaded by hand (.srt, .ass, .vtt or a plain-text transcript).</span>
      <div class="spacer"></div>
      <button class="btn small" id="retry">Retry not-found episodes</button>
      <button class="btn small" id="reload-eps">Reload TVDB episode list</button></div>
    <table><thead><tr><th>Episode</th><th>Title</th><th>Reference</th><th style="width:170px"></th></tr></thead><tbody>
    ${eps.map((e) => `<tr>
      <td class="mono">${esc(e.code)}</td>
      <td>${esc(e.title)}<div class="muted small">${esc(e.aired || "")}</div></td>
      <td>${e.ref ? `<span class="badge b-ok">${esc(e.ref.split(":")[0])}</span> <span class="muted small">${esc(e.ref_note)}</span>` :
        e.miss ? `<span class="badge b-warn">not found</span> <span class="muted small">${esc(e.miss)}</span>` :
        `<span class="muted small">—</span>`}</td>
      <td><label class="btn small">Upload<input type="file" hidden data-code="${esc(e.code)}" accept=".srt,.ass,.ssa,.vtt,.txt"></label>
        ${e.ref || e.miss ? `<button class="btn small refetch" data-code="${esc(e.code)}">Re-fetch</button>` : ""}</td>
    </tr>`).join("")}</tbody></table>`;
  $("#retry", tab).onclick = () => startJob("fetch_refs", { retry_misses: true });
  $("#reload-eps", tab).onclick = () => startJob("refresh_episodes");
  $$("input[type=file]", tab).forEach((inp) => inp.onchange = async () => {
    const fd = new FormData();
    fd.append("file", inp.files[0]);
    try {
      await api(`/api/series/${s.id}/references/${inp.dataset.code}`, { method: "POST", body: fd });
      toast(`Reference uploaded for ${inp.dataset.code}`);
      state.series = await api(`/api/series/${s.id}`); tabEpisodes();
    } catch (e) { fail(e); }
  });
  $$("button.refetch", tab).forEach((b) => b.onclick = async () => {
    await api(`/api/series/${s.id}/references/${b.dataset.code}`, { method: "DELETE" }).catch(fail);
    startJob("fetch_refs", { codes: [b.dataset.code] });
  });
}

// ------------------------------------------------------------------ history

async function tabHistory() {
  const list = await api(`/api/series/${state.series.id}/applies`);
  const tab = $("#tab");
  if (!list.length) { tab.innerHTML = `<div class="empty">No changes applied yet.</div>`; return; }
  const latestOpen = list.find((a) => !a.undone_at);
  tab.innerHTML = `<table><thead><tr><th>#</th><th>When</th><th>Operations</th><th>Status</th><th></th></tr></thead><tbody>
    ${list.map((a) => `<tr><td>${a.id}</td><td>${esc(when(a.created))}</td><td>${a.ops}</td>
      <td>${a.undone_at ? `<span class="badge">undone ${esc(when(a.undone_at))}</span>` : `<span class="badge b-ok">applied</span>`}</td>
      <td><button class="btn small view" data-id="${a.id}">View log</button>
        ${latestOpen && latestOpen.id === a.id ? `<button class="btn small danger undo" data-id="${a.id}">Undo</button>` : ""}</td></tr>`).join("")}
    </tbody></table>
    <p class="muted small">Undo works newest-first. Files created by splits are parked in the backup folder's <span class="mono">undone</span> folder rather than deleted.</p>`;
  $$("button.view", tab).forEach((b) => b.onclick = async () => {
    const a = await api(`/api/applies/${b.dataset.id}`);
    modal(`<h2 style="margin-top:0">Apply #${a.id}</h2><pre class="mono" style="white-space:pre-wrap">${
      esc(a.log.map((o) => o.op === "move" ? `move   ${o.src}\n    → ${o.dst}  (${o.note})` :
        `create ${o.dst}  (${o.note})`).join("\n"))}</pre>
      <div class="row"><div class="spacer"></div><button class="btn" onclick="closeModal()">Close</button></div>`);
  });
  $$("button.undo", tab).forEach((b) => b.onclick = async () => {
    if (!confirm("Undo this apply? Files go back to their previous names.")) return;
    try { await api(`/api/applies/${b.dataset.id}/undo`, { method: "POST" }); watchJobs(); }
    catch (e) { fail(e); }
  });
}

// ------------------------------------------------------------------ options

async function tabOptions() {
  const s = state.series;
  const o = s.options || {};
  const tab = $("#tab");
  tab.innerHTML = `<form class="settings" id="opts"><fieldset><legend>Series</legend>
    ${field("name_in_files", "Series name in filenames", o.name_in_files || "", "Defaults to the folder name: " + s.path.split("/").pop())}
    ${field("imdb_id", "IMDb ID", s.imdb_id || "", "From TVDB. Used to look up subtitles on OpenSubtitles (e.g. tt1942683).")}
    <div class="field"><label>Include specials (season 0)</label><input type="checkbox" name="include_specials" ${o.include_specials ? "checked" : ""}></div>
    </fieldset>
    <fieldset><legend>Title cards</legend>
    <div class="field"><label>Read on-screen titles</label>
      <select name="title_cards">${[["auto", "Auto — test a few files first"], ["on", "On"], ["off", "Off"]].map(([v, t]) =>
        `<option value="${v}" ${(o.title_cards || "auto") === v ? "selected" : ""}>${t}</option>`).join("")}</select>
      <div class="hint">${tcStatus(o)}</div></div>
    <div class="field"><label>Check</label>
      <select name="title_cards_scope">${[["unconfirmed", "Only files the dialogue match didn't confirm"], ["all", "Every file (slower)"]].map(([v, t]) =>
        `<option value="${v}" ${(o.title_cards_scope || "unconfirmed") === v ? "selected" : ""}>${t}</option>`).join("")}</select></div>
    <div class="field"><label>Trust title cards</label>
      <input type="checkbox" name="title_cards_trust" ${o.title_cards_trust === false ? "" : "checked"}>
      <div class="hint">When the dialogue can't tell, a clearly read title card (complete title, seen on 2+ frames)
        decides — even against the filename. Strong dialogue disagreeing with the card still goes to review.</div></div>
    <div class="row"><div class="small muted" style="flex:1">Title-card OCR is cached, so rescans normally reuse it. Use this after an update that changes how cards are read, or if a card was misread.</div>
      <button class="btn small" type="button" id="reread-cards">Rescan, re-reading title cards</button></div>
    </fieldset>
    <fieldset><legend>Fandom wiki transcripts</legend>
    <div class="row" style="margin-bottom:6px"><div class="small" style="flex:1">${wikiStatus(o)}</div>
      <button class="btn small" type="button" id="find-wiki">Find automatically</button></div>
    ${field("fandom_wiki", "Wiki", o.fandom_wiki || "", "Found automatically when the series is added. Subdomain, host or any URL on the wiki — e.g. theamazingworldofgumball or https://pawpatrol.fandom.com/wiki/…. Checked before OpenSubtitles; costs no download quota.")}
    ${field("fandom_page_pattern", "Transcript page", o.fandom_page_pattern || "{title}/Transcript", "Only used for episodes not matched to a page automatically. Use {title}, {season}, {episode}.")}
    <div class="field"><label>Page title overrides</label>
      <textarea name="fandom_title_overrides" rows="4" class="mono" placeholder='{"S01E01": "The DVD/Transcript"}'>${esc(o.fandom_title_overrides ? JSON.stringify(o.fandom_title_overrides, null, 1) : "")}</textarea>
      <div class="hint">JSON object: episode code → wiki page title or URL, e.g. {"S01E03": "https://pawpatrol.fandom.com/wiki/Pups_Save_the_Sea_Turtles/Transcript"}. Underscores and full URLs are fine.</div></div>
    </fieldset>
    <fieldset><legend>Backup folder</legend>
      <div class="hint" style="margin-bottom:8px">Files EpisodeID moved out of the way (duplicates, unverified files,
        originals of split files, old metadata…). Kept beside the series folder, not in it, so Sonarr doesn't see
        them. Nothing is ever deleted unless you do it here.</div>
      <div id="bk"><span class="muted small">Loading…</span></div>
    </fieldset>
    <div class="row"><button class="btn danger" type="button" id="del">Remove series from EpisodeID</button>
      <div class="spacer"></div><button class="btn primary" type="submit">Save</button></div></form>`;
  $("#opts").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    let ov = {};
    const raw = f.fandom_title_overrides.value.trim();
    if (raw) { try { ov = JSON.parse(raw); } catch { return toast("Page title overrides must be valid JSON"); } }
    try {
      state.series = await api(`/api/series/${s.id}/options`, { method: "PATCH", body: {
        name_in_files: f.name_in_files.value.trim(), imdb_id: f.imdb_id.value.trim(),
        include_specials: f.include_specials.checked, fandom_wiki: f.fandom_wiki.value.trim(),
        fandom_page_pattern: f.fandom_page_pattern.value.trim(), fandom_title_overrides: ov,
        title_cards: f.title_cards.value, title_cards_scope: f.title_cards_scope.value,
        title_cards_trust: f.title_cards_trust.checked } });
      toast("Saved");
    } catch (err) { fail(err); }
  };
  $("#reread-cards").onclick = () => {
    if (!confirm("Scan again, reading every title card from the video files instead of the cache? This takes longer than a normal scan.")) return;
    startJob("scan", { reread_cards: true }); toast("Rescanning — see the job log.");
  };
  loadBackup(s);
  $("#find-wiki").onclick = () => { startJob("fetch_refs", { find_wiki: true }); toast("Looking for the wiki — see the job log."); };
  $("#del").onclick = async () => {
    if (!confirm("Remove this series from EpisodeID? No media files are touched.")) return;
    await api(`/api/series/${s.id}`, { method: "DELETE" });
    location.hash = "#/";
  };
}

const BACKUP_INFO = {
  duplicates: "copies of episodes another file was a better match for",
  unverified: "files whose name a confirmed file needed",
  split_originals: "the original files that were split",
  metadata: "old .nfo files and thumbnails",
  conflicts: "files that were in the way of a rename",
  undone: "files created by an apply that was undone",
  undo_conflicts: "restored copies whose original place was taken",
};

function fmtBytes(n) {
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i && n < 10 ? 1 : 0)} ${u[i]}`;
}

async function loadBackup(s) {
  const el = $("#bk");
  if (!el) return;
  let b;
  try { b = await api(`/api/series/${s.id}/backup`); }
  catch (e) { el.innerHTML = `<span class="badge b-bad">Error</span> ${esc(e.message)}`; return; }
  if (!b.files) { el.innerHTML = `<span class="muted small">Empty — nothing in ${esc(b.folder)}/.</span>`; return; }
  el.innerHTML = `<table><tr><th style="width:28px"><input type="checkbox" id="bk-all" checked></th><th>Folder</th><th>Files</th><th>Size</th></tr>
    ${b.buckets.map((k) => `<tr><td><input type="checkbox" class="bk" value="${esc(k.name)}" checked></td>
      <td><span class="mono">${esc(b.folder)}/${esc(k.name === "(top level)" ? "" : k.name + "/")}</span>
        ${BACKUP_INFO[k.name] ? `<div class="muted small">${esc(BACKUP_INFO[k.name])}</div>` : ""}</td>
      <td>${k.files}</td><td>${fmtBytes(k.bytes)}</td></tr>`).join("")}</table>
    <div class="row" style="margin-top:8px"><span class="small muted">${b.files} files, ${fmtBytes(b.bytes)} in total</span>
      <div class="spacer"></div><button class="btn danger small" type="button" id="bk-del">Delete selected…</button></div>`;
  $("#bk-all").onchange = (e) => $$(".bk", el).forEach((c) => c.checked = e.target.checked);
  $("#bk-del").onclick = () => {
    const chosen = $$(".bk", el).filter((c) => c.checked).map((c) => c.value);
    if (!chosen.length) return toast("Nothing selected");
    const sel = b.buckets.filter((k) => chosen.includes(k.name));
    const n = sel.reduce((a, k) => a + k.files, 0), size = sel.reduce((a, k) => a + k.bytes, 0);
    const box = modal(`<h2 style="margin-top:0">Delete ${n} backed-up files?</h2>
      <p>This permanently deletes <b>${n} files (${fmtBytes(size)})</b> from
        ${sel.map((k) => `<span class="mono">${esc(b.folder)}/${esc(k.name === "(top level)" ? "" : k.name + "/")}</span>`).join(", ")}.</p>
      <p class="muted small">It can't be undone, and <b>Undo</b> in History won't be able to bring these files back
        for earlier applies — it will restore everything else and list them as missing.</p>
      <p>Type <b>DELETE</b> to continue:</p>
      <input type="text" id="confirm" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg)">
      <div class="row" style="margin-top:12px"><div class="spacer"></div>
        <button class="btn" id="c">Cancel</button><button class="btn danger" id="go" disabled>Delete</button></div>`);
    $("#c", box).onclick = closeModal;
    $("#confirm", box).oninput = (e) => $("#go", box).disabled = e.target.value.trim() !== "DELETE";
    $("#go", box).onclick = async () => {
      $("#go", box).disabled = true;
      try {
        const r = await api(`/api/series/${s.id}/backup/delete`, { method: "POST", body: { buckets: chosen, confirm: "DELETE" } });
        closeModal();
        toast(`Deleted ${r.deleted} files (${fmtBytes(r.bytes)})` + (r.errors.length ? ` — ${r.errors.length} couldn't be deleted` : ""));
        loadBackup(s);
      } catch (e) { fail(e); $("#go", box).disabled = false; }
    };
    $("#confirm", box).focus();
  };
}

function wikiStatus(o) {
  const d = o.fandom_discovery;
  if (!d) return o.fandom_wiki ? "Transcript pages will be matched to episodes on the next reference fetch."
    : `<span class="muted">Not searched yet — happens on the next reference fetch.</span>`;
  if (!d.wiki) return `<span class="muted">No Fandom wiki with transcripts was found for this series.</span>`;
  const host = d.wiki.includes(".") ? d.wiki : `${d.wiki}.fandom.com`;
  return `<span class="badge b-ok">${d.auto ? "found" : "set"}</span> <a href="https://${esc(host)}" target="_blank">${esc(host)}</a>` +
    (d.sitename ? ` <span class="muted">(${esc(d.sitename)})</span>` : "") +
    (d.total ? ` — ${d.mapped} of ${d.total} episodes matched to a transcript page` : "") +
    (d.found_via ? ` <span class="muted">via ${esc(d.found_via)}</span>` : "");
}

function tcStatus(o) {
  const st = o.title_cards_status;
  if (!st || st.has_cards === undefined) return "Not tested yet — the next scan checks a few files.";
  if (!st.has_cards) return `Not found on ${st.hits}/${st.probed} test files, so not used. Choose On to force it.`;
  return `Found on ${st.hits}/${st.probed} test files` + (st.window ? `, usually ${fmtTime(st.window[0])}–${fmtTime(st.window[1])} into an episode.` : ".");
}

function field(name, label, value, hint = "", type = "text", locked = false) {
  return `<div class="field"><label for="f-${name}">${esc(label)}${locked ? ' <span class="locked">(set by env)</span>' : ""}</label>
    <input type="${type}" id="f-${name}" name="${name}" value="${esc(value)}" ${locked ? "disabled" : ""} autocomplete="off">
    ${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
}

// ----------------------------------------------------------------- settings

async function renderSettings() {
  refreshChips();
  const { settings: s, locked } = await api("/api/settings");
  const L = (k) => locked.includes(k);
  const f = (k, label, hint = "", type = "text") => field(k, label, s[k], hint, type, L(k));
  const cb = (k, label, hint = "") => `<div class="field"><label>${esc(label)}${L(k) ? ' <span class="locked">(set by env)</span>' : ""}</label>
    <input type="checkbox" name="${k}" ${s[k] ? "checked" : ""} ${L(k) ? "disabled" : ""}>${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  const sel = (k, label, opts, hint = "") => `<div class="field"><label>${esc(label)}</label>
    <select name="${k}" ${L(k) ? "disabled" : ""}>${opts.map(([v, t]) => `<option value="${esc(v)}" ${String(s[k]) === v ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
    ${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  const test = (svc) => `<div class="row" style="justify-content:flex-end"><span class="small muted" id="t-${svc}"></span>
    <button class="btn small test" type="button" data-svc="${svc}">Test</button></div>`;

  view.innerHTML = `<h1>Settings</h1><p class="muted">Values set through environment variables are shown as locked.</p>
  <form class="settings" id="settings">
  <fieldset><legend>TVDB (required)</legend>
    ${f("tvdb_api_key", "API key", 'From <a href="https://thetvdb.com/api-information" target="_blank">thetvdb.com</a>. Episode numbers follow TVDB aired order, like Sonarr.', "password")}
    ${f("tvdb_pin", "Subscriber PIN", "Only needed for user-supported keys.", "password")}
    ${f("tvdb_language", "Language", "Three-letter code for episode titles, e.g. eng.")}
    ${test("tvdb")}</fieldset>
  <fieldset><legend>OpenSubtitles (reference subtitles)</legend>
    ${f("opensubtitles_api_key", "API key", 'Create a consumer at <a href="https://www.opensubtitles.com/consumers" target="_blank">opensubtitles.com/consumers</a>.', "password")}
    ${f("opensubtitles_username", "Username", "An account is needed to download. Free accounts have a small daily download limit — downloads are cached, so large series fill in over a few days.")}
    ${f("opensubtitles_password", "Password", "", "password")}
    ${f("subtitle_language", "Subtitle language", "Two-letter code, e.g. en. Also used to pick embedded subtitle tracks.")}
    ${test("opensubtitles")}</fieldset>
  <fieldset><legend>Whisper (files without subtitles)</legend>
    ${cb("whisper_enabled", "Enabled")}
    ${sel("whisper_model", "Model", [["tiny", "tiny"], ["base", "base"], ["small", "small (recommended on CPU)"], ["medium", "medium"], ["large-v3", "large-v3 (GPU)"], ["distil-large-v3", "distil-large-v3"]], "Downloaded to /cache/whisper on first use. Larger is more accurate and slower.")}
    ${sel("whisper_device", "Device", [["auto", "auto"], ["cpu", "CPU"], ["cuda", "NVIDIA GPU"]])}
    ${sel("whisper_compute_type", "Compute type", [["default", "default (int8 on CPU, float16 on GPU)"], ["int8", "int8"], ["int8_float16", "int8_float16"], ["float16", "float16"], ["float32", "float32"]])}
    ${f("whisper_language", "Spoken language", "e.g. en. Leave blank to auto-detect.")}
    ${test("whisper")}</fieldset>
  <fieldset><legend>Title cards</legend>
    <p class="muted small" style="margin-top:0">Reads the episode title shown on screen (OCR) as evidence independent of
      subtitles and references. Each series tests a few files first and only uses it if the show has title cards.</p>
    ${cb("titlecard_enabled", "Enabled")}
    ${f("titlecard_scan_seconds", "Search the first (s)", "How far into each episode to look for the card when its usual position isn't known yet.", "number")}
    ${f("titlecard_fps", "Frames per second", "1 is enough for cards shown for 2+ seconds.")}
    ${cb("titlecard_vision", "Use a vision model when OCR can't read a card", "Sends a few frames to the AI endpoint below. Needs a vision-capable model (e.g. qwen2.5vl in Ollama).")}
    ${f("llm_vision_model", "Vision model", "Leave blank to use the AI model below.")}
    ${sel("titlecard_ocr_device", "OCR on", [["auto", "Auto — NVIDIA GPU when available (-cuda image)"], ["cpu", "CPU"]])}
    ${sel("titlecard_hwaccel", "Video decoding", [["auto", "Auto — use a GPU if one is passed to the container"], ["cuda", "NVIDIA (NVDEC)"], ["vaapi", "Intel / AMD (VAAPI, needs /dev/dri)"], ["off", "CPU only"]],
      "Hardware decoding falls back to the CPU automatically if it doesn't work for a file.")}
    ${test("titlecards")}
  </fieldset>
  <fieldset><legend>AI fallback (optional, OpenAI-compatible)</legend>
    <p class="muted small" style="margin-top:0">Only used for files the dialogue match can't settle, choosing among a short list of
      candidates. Its picks are marked <span class="badge b-ai">AI</span> and are never ticked for apply automatically.</p>
    ${cb("llm_enabled", "Enabled")}
    ${f("llm_base_url", "Base URL", "Ollama: http://HOST:11434/v1 · OpenAI: https://api.openai.com/v1 · LM Studio: http://HOST:1234/v1")}
    ${f("llm_api_key", "API key", "Leave blank for Ollama.", "password")}
    ${f("llm_model", "Model", "e.g. qwen3:8b, llama3.1:8b, gpt-4o-mini")}
    ${test("llm")}</fieldset>
  <fieldset><legend>Sonarr (optional)</legend>
    ${f("sonarr_url", "URL", "e.g. http://192.168.1.10:8989")}
    ${f("sonarr_api_key", "API key", "", "password")}
    ${cb("sonarr_rescan_after_apply", "Rescan after apply / undo")}
    ${cb("sonarr_import_scan", "Scan new downloads", `When Sonarr imports a file, scan just that file and flag the series if its content doesn't match its name. In Sonarr: Settings → Connect → + → Webhook, URL <span class="mono">${esc(location.origin)}/api/sonarr/webhook</span>, method POST, triggers On Import / On File Import and On Upgrade (add the username and password if you've set a login here). The series must have been scanned once.`)}
    ${test("sonarr")}</fieldset>
  <fieldset><legend>Naming</legend>
    <div class="row" style="justify-content:flex-end;margin-bottom:8px"><span class="small muted" id="naming-import-out"></span>
      <button class="btn small" type="button" id="naming-import" title="Reads Sonarr's Media Management naming settings (needs the Sonarr URL and API key above)">Copy from Sonarr</button></div>
    ${f("naming_format", "Episode file name", "Sonarr's tokens work as they are ({Series Title} - S{season:00}E{episode:00} - {Episode Title} {Quality Full}), as do {series} {season:02d} {episode:02d} {title} {quality} {year}. Quality is carried over from the old name (e.g. WEBDL-1080p). MediaInfo and release-group tokens are left out.")}
    ${cb("replace_illegal_characters", "Replace illegal characters", "As in Sonarr (Media Management): \\ / → +, ? → ! (a ? ending a title is dropped), * → -, others removed. Off: all removed.")}
    ${sel("colon_replacement", "Colon replacement", [["smart", "Smart — \"Up: Pups\" → \"Up - Pups\" (Sonarr)"], ["delete", "Delete"], ["dash", "Replace with dash"], ["space_dash", "Replace with space dash"], ["space_dash_space", "Replace with space dash space"], ["custom", "Custom"]], "Match your Sonarr setting so names come out the same.")}
    ${f("colon_replacement_custom", "Custom colon replacement", "Only used when Colon replacement is Custom.")}
    ${sel("multi_episode_style", "Multi-episode style", [["prefixed_range", "Prefixed Range — S01E01-E03 (Sonarr default)"], ["extend", "Extend — S01E01-02-03"], ["duplicate", "Duplicate — S01E01 - S01E02 - S01E03"], ["repeat", "Repeat — S01E01E02E03"], ["scene", "Scene — S01E01-E02-E03"], ["range", "Range — S01E01-03"]])}
    ${f("season_folder_format", "Season folder", "Used only when a season folder doesn't exist yet.")}
    ${f("specials_folder", "Specials folder")}
    ${f("backup_folder", "Backup folder", "Created next to your series folders (<TV>/_episodeid_backup/<Show>/…), out of Sonarr's sight. Displaced files go here — nothing is deleted.")}
    ${cb("allow_splits", "Allow splitting multi-episode files", "Lossless cuts at the black gap between episodes.")}</fieldset>
  <fieldset><legend>File check</legend>
    ${f("auto_check_hours", "Check for new files every (hours)", "Looks for added, replaced or removed video files in every scanned series (names, sizes and dates only — no video is read). A series with changes is flagged on its card and gets references fetched for new episodes; scanning is left to you. 0 turns it off.", "number")}
    <div class="row" style="justify-content:flex-end"><span class="small muted" id="fc-out"></span>
      <button class="btn small" type="button" id="fc-now">Check now</button></div></fieldset>
  <fieldset><legend>Security</legend>
    <p class="muted small" style="margin-top:0">Set both to ask for a login before the web UI opens (your browser asks once and
      remembers it). Leave either blank for no login. Locked out? Set AUTH_USERNAME / AUTH_PASSWORD on the container,
      or remove them from /config/settings.json.</p>
    ${f("auth_username", "Username")}
    ${f("auth_password", "Password", "", "password")}</fieldset>
  <fieldset><legend>Matching (advanced)</legend>
    ${f("window_seconds", "Window length (s)", "", "number")}
    ${f("window_step_seconds", "Window step (s)", "", "number")}
    ${f("min_segment_seconds", "Shortest episode segment (s)", "Shorter runs are treated as noise (recaps, previews).", "number")}
    ${f("min_score", "Minimum score", "TF-IDF cosine similarity a confident match must reach.")}
    ${f("min_margin", "Minimum margin", "How far the best match must beat the runner-up.")}</fieldset>
  <div class="row"><div class="spacer"></div><button class="btn primary" type="submit">Save settings</button></div>
  </form>
  <form class="settings"><fieldset><legend>Cache</legend>
    <p class="muted small" style="margin-top:0">Dialogue, title-card OCR and preview pictures are kept so files aren't read twice.
      They follow a file through renames; a replaced file gets new entries and the old ones stay behind.
      <em>Clear unused</em> deletes entries for files that are no longer in any series (or its backup folder) and all preview
      pictures. Whisper models and downloaded references aren't touched.</p>
    <div id="cache-info" class="small muted">Measuring…</div>
    <div class="row" style="justify-content:flex-end"><span class="small muted" id="cache-out"></span>
      <button class="btn small" type="button" id="cache-prune">Clear unused cache</button></div>
  </fieldset></form>`;

  const form = $("#settings");
  const collect = () => {
    const out = {};
    $$("input, select", form).forEach((el) => {
      if (!el.name || el.disabled) return;
      out[el.name] = el.type === "checkbox" ? el.checked : el.value;
    });
    return out;
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/settings", { method: "PUT", body: collect() }); toast("Settings saved"); refreshChips(); }
    catch (err) { fail(err); }
  };
  const showCache = (c) => {
    $("#cache-info").innerHTML = c.kinds.map((k) => `${esc(k.label)}: ${k.files} files, ${fmtBytes(k.bytes)}`).join("<br>");
  };
  api("/api/cache").then(showCache).catch(() => { $("#cache-info").textContent = ""; });
  $("#fc-now").onclick = async () => {
    const out = $("#fc-out");
    out.textContent = "Checking…";
    try {
      const r = await api("/api/file-check", { method: "POST" });
      out.textContent = r.changed.length ? "Changed since the last scan: " + r.changed.map((c) =>
        `${c.name} (${[c.added && c.added + " new", c.changed && c.changed + " replaced", c.removed && c.removed + " removed"].filter(Boolean).join(", ")})`).join("; ")
        : "No changes.";
    } catch (err) { out.textContent = err.message; }
  };
  $("#cache-prune").onclick = async () => {
    const out = $("#cache-out");
    out.textContent = "Checking every series…";
    try {
      const r = await api("/api/cache/prune", { method: "POST" });
      out.textContent = `Removed ${r.deleted} files (${fmtBytes(r.bytes)}); kept what ${r.videos} videos use.`;
      showCache(r);
    } catch (err) { out.textContent = err.message; }
  };
  $("#naming-import").onclick = async () => {
    const out = $("#naming-import-out");
    out.textContent = "Saving & reading Sonarr…";
    try {
      await api("/api/settings", { method: "PUT", body: collect() });
      const r = await api("/api/sonarr/import-naming", { method: "POST" });
      const n = Object.keys(r.changed).length;
      toast(n ? `Copied ${n} naming setting${n === 1 ? "" : "s"} from Sonarr` : "Naming already matches Sonarr");
      await renderSettings();
      if (r.notes.length) alert(r.notes.join("\n\n"));
    } catch (err) { out.textContent = err.message; }
  };
  $$("button.test", form).forEach((b) => b.onclick = async () => {
    const out = $(`#t-${b.dataset.svc}`);
    out.textContent = "Saving & testing…";
    try {
      await api("/api/settings", { method: "PUT", body: collect() });
      const r = await api(`/api/test/${b.dataset.svc}`, { method: "POST" });
      out.innerHTML = `<span class="badge ${r.ok ? "b-ok" : "b-bad"}">${r.ok ? "OK" : "Failed"}</span> ${esc(r.message)}`;
      refreshChips();
    } catch (err) { out.textContent = err.message; }
  });
}

route();
