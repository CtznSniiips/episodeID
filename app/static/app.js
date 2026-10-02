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

function modal(html) {
  $("#modal-box").innerHTML = html;
  $("#modal").classList.remove("hidden");
  return $("#modal-box");
}
function closeModal() { $("#modal").classList.add("hidden"); $("#modal-box").innerHTML = ""; }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

const fmtTime = (s) => {
  s = Math.max(0, Math.round(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const when = (iso) => iso ? new Date(iso).toLocaleString() : "";

const STATUS_BADGE = {
  OK: "b-ok", OK_ORDER_DIFFERS: "b-warn", MISMATCH: "b-bad", LOW_CONFIDENCE: "b-warn",
  NO_TEXT: "", NO_MATCH: "b-warn", ERROR: "b-bad", MANUAL: "b-info",
};
const KIND_BADGE = { rename: "b-info", split: "b-warn", aside: "b-bad", review: "", ok: "b-ok" };

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
    <div class="spacer"></div><button class="btn primary" id="add">+ Add series</button></div>`;
  if (!status.tvdb) {
    html += `<div class="card" style="margin-top:16px">Start by adding your TVDB API key in
      <a href="#/settings">Settings</a> — EpisodeID uses TVDB episode numbering so names line up with Sonarr.</div>`;
  }
  if (!list.length) {
    html += `<div class="empty">No series yet. Click <b>Add series</b> to pick a show folder.</div>`;
  } else {
    html += `<div class="grid" style="margin-top:16px">` + list.map((s) => {
      const pct = s.episode_count ? Math.round(100 * s.reference_count / s.episode_count) : 0;
      const counts = s.last_scan ? Object.entries(s.last_scan.counts).map(([k, v]) =>
        `<span class="badge ${STATUS_BADGE[k] || ""}">${esc(k)} ${v}</span>`).join("") :
        `<span class="muted small">Not scanned yet</span>`;
      return `<div class="card series-card" data-id="${s.id}">
        <div class="name">${esc(s.name)} <span class="muted">${esc(s.year)}</span></div>
        <div class="path muted">${esc(s.path)}</div>
        <div class="small muted">References: ${s.reference_count} / ${s.episode_count} episodes</div>
        <div class="bar"><div style="width:${pct}%"></div></div>
        <div class="counts">${counts}</div>
        ${s.active_job ? `<div class="small"><span class="badge b-info">${esc(s.active_job.kind)} running…</span></div>` : ""}
      </div>`;
    }).join("") + `</div>`;
  }
  view.innerHTML = html;
  $("#add").onclick = () => addSeriesDialog();
  $$(".series-card").forEach((c) => c.onclick = () => location.hash = `#/series/${c.dataset.id}`);
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
  let lastId = null, wasActive = false;
  const tick = async () => {
    if (!state.series || state.series.id !== sid) return clearInterval(pollTimer);
    const list = await api(`/api/jobs?series_id=${sid}`).catch(() => []);
    const j = list[0];
    const box = $("#job");
    if (!box) return clearInterval(pollTimer);
    if (!j) { box.innerHTML = ""; return; }
    const active = j.status === "queued" || j.status === "running";
    const full = await api(`/api/jobs/${j.id}`);
    const pct = Math.round((full.progress || 0) * 100);
    const badge = { done: "b-ok", failed: "b-bad", cancelled: "", running: "b-info", queued: "" }[j.status];
    box.innerHTML = `<div class="card job">
      <div class="row"><b>${esc(labelJob(j.kind))}</b>
        <span class="badge ${badge}">${esc(j.status)}</span>
        <span class="muted small">${esc(full.message || "")}</span><div class="spacer"></div>
        ${active ? `<button class="btn small danger" id="cancel-job">Cancel</button>` :
          `<button class="btn small" id="hide-job">Hide</button>`}</div>
      ${active ? `<div class="bar" style="margin-top:8px"><div style="width:${pct}%"></div></div>` : ""}
      <pre id="joblog">${esc(full.log.split("\n").slice(-300).join("\n"))}</pre></div>`;
    const pre = $("#joblog");
    pre.scrollTop = pre.scrollHeight;
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
    if (!active) clearInterval(pollTimer);
  };
  await tick();
  pollTimer = setInterval(tick, 1200);
}

function labelJob(kind) {
  return { scan: "Scan", fetch_refs: "Fetch references", refresh_episodes: "Load TVDB episodes",
    apply: "Apply changes", undo: "Undo", replan: "Rebuild plan" }[kind] || kind;
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
      <span class="muted">“${esc(tc.text)}” at ${fmtTime(tc.time)} → ${esc(tc.code)}${
      sg.dialogue_code && !conflict ? ` (dialogue said ${esc(sg.dialogue_code)})` : ""}</span></div>`;
  }
  if (sg.llm) h += `<div class="small"><span class="badge b-ai">AI → ${esc(sg.llm.code)} ${Math.round(sg.llm.confidence * 100)}%</span>
    <span class="muted">${esc(sg.llm.reason)}</span></div>`;
  return h + `</span>`;
}

async function tabPlan() {
  const plan = await api(`/api/series/${state.series.id}/plan`);
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
  const actionable = plan.items.filter((it) => ["rename", "split", "aside"].includes(it.kind));
  const filters = [["actions", "Changes", actionable.length], ["review", "Needs review", counts.review || 0],
    ["ok", "Correct", counts.ok || 0], ["all", "All", plan.items.length]];
  const shown = plan.items.filter((it) => state.filter === "all" ? true :
    state.filter === "actions" ? ["rename", "split", "aside"].includes(it.kind) : it.kind === state.filter);
  const applied = plan.status !== "draft";
  const selCount = actionable.filter((it) => it.selected).length;

  tab.innerHTML = `<div class="row" style="margin-bottom:10px">
      <div class="filters">${filters.map(([k, l, n]) =>
        `<button class="btn small ${state.filter === k ? "active" : ""}" data-f="${k}">${l} (${n})</button>`).join("")}</div>
      <div class="spacer"></div>
      <span class="muted small">Plan #${plan.id} · ${esc(when(plan.created))}${applied ? " · <b>applied</b>" : ""}</span>
      ${applied ? "" : `<button class="btn primary" id="apply" ${selCount ? "" : "disabled"}>Apply ${selCount} change${selCount === 1 ? "" : "s"}…</button>`}
    </div>
    ${shown.length ? `<table><thead><tr>
      <th class="check">${applied ? "" : `<input type="checkbox" id="all-sel" title="Select all">`}</th>
      <th>File → result</th><th>Detected in the file</th><th style="width:150px"></th></tr></thead><tbody>
      ${shown.map((it) => rowHtml(it, eps, applied)).join("")}</tbody></table>` :
      `<div class="empty">Nothing here.</div>`}`;

  $$(".filters .btn", tab).forEach((b) => b.onclick = () => { state.filter = b.dataset.f; tabPlan(); });
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
  $$("button.clear-ov", tab).forEach((b) => b.onclick = async () => {
    await api(`/api/series/${state.series.id}/overrides`, { method: "POST",
      body: { source: b.dataset.src, codes: null } }).catch(fail);
    tabPlan();
  });
  const ap = $("#apply", tab);
  if (ap) ap.onclick = () => confirmApply(plan, actionable.filter((it) => it.selected));
}

function rowHtml(it, eps, applied) {
  const actionable = ["rename", "split", "aside"].includes(it.kind);
  const targets = (it.targets || []).map((t) =>
    `<div class="arrow">↳ <span class="path">${esc(t.path)}</span>${t.duplicate ? ' <span class="badge b-bad">duplicate</span>' : ""}</div>`).join("");
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
      <div class="path" style="margin-top:4px">${esc(it.source)}</div>${targets}
      <div class="muted small">${esc(it.reason || "")}</div></td>
    <td>${segs}${src}</td>
    <td>${applied ? "" : `<button class="btn small set-ep" data-src="${esc(it.source)}" data-cur="${esc(cur)}">Set episode…</button>
      ${ovr ? `<button class="btn small clear-ov" data-src="${esc(it.source)}">Clear</button>` : ""}`}</td>
  </tr>`;
}

function setEpisodeDialog(source, current) {
  const box = modal(`<h2 style="margin-top:0">Set episode</h2>
    <div class="path">${esc(source)}</div>
    <p class="muted small">Type the episode code(s) this file really contains, in the order they play —
      e.g. <span class="mono">S02E05</span> or <span class="mono">S02E05 S02E06</span> for a two-part file.
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

function confirmApply(plan, items) {
  const n = (k) => items.filter((i) => i.kind === k).length;
  const box = modal(`<h2 style="margin-top:0">Apply ${items.length} changes?</h2>
    <ul><li>${n("rename")} renames</li><li>${n("split")} splits (lossless, original kept in the backup folder)</li>
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
        title_cards: f.title_cards.value, title_cards_scope: f.title_cards_scope.value } });
      toast("Saved");
    } catch (err) { fail(err); }
  };
  $("#find-wiki").onclick = () => { startJob("fetch_refs", { find_wiki: true }); toast("Looking for the wiki — see the job log."); };
  $("#del").onclick = async () => {
    if (!confirm("Remove this series from EpisodeID? No media files are touched.")) return;
    await api(`/api/series/${s.id}`, { method: "DELETE" });
    location.hash = "#/";
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
    ${test("sonarr")}</fieldset>
  <fieldset><legend>Naming</legend>
    ${f("naming_format", "Episode file name", "Tokens: {series} {season:02d} {episode:02d} {title} {quality}. {quality} is carried over from the old name (e.g. WEBDL-1080p).")}
    ${sel("multi_episode_style", "Multi-episode style", [["prefixed_range", "Prefixed range — S01E01-E02 (Sonarr default)"], ["extend", "Extend — S01E01-02"], ["repeat", "Repeat — S01E01E02"]])}
    ${f("season_folder_format", "Season folder", "Used only when a season folder doesn't exist yet.")}
    ${f("specials_folder", "Specials folder")}
    ${f("backup_folder", "Backup folder", "Created inside each series folder. Displaced files go here — nothing is deleted.")}
    ${cb("allow_splits", "Allow splitting multi-episode files", "Lossless cuts at the black gap between episodes.")}</fieldset>
  <fieldset><legend>Matching (advanced)</legend>
    ${f("window_seconds", "Window length (s)", "", "number")}
    ${f("window_step_seconds", "Window step (s)", "", "number")}
    ${f("min_segment_seconds", "Shortest episode segment (s)", "Shorter runs are treated as noise (recaps, previews).", "number")}
    ${f("min_score", "Minimum score", "TF-IDF cosine similarity a confident match must reach.")}
    ${f("min_margin", "Minimum margin", "How far the best match must beat the runner-up.")}</fieldset>
  <div class="row"><div class="spacer"></div><button class="btn primary" type="submit">Save settings</button></div>
  </form>`;

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
