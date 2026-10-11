"use strict";

const $ = (s) => document.querySelector(s);
const jobs = new Map();          // id -> job
const rows = new Map();          // id -> element
const selected = new Set();
let anchor = null;               // for shift-click range selection
let info = { presets: {}, arr: {} };
let detailId = null;

// ---------- helpers ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new Error((data && data.detail) || (data && data.error) || `HTTP ${res.status}`);
  return data;
}
function bytes(n) {
  if (n == null || isNaN(n)) return "?";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}
function dur(s) {
  if (s == null) return "";
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60}s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}
function ago(t) {
  const d = (Date.now() / 1000) - t;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString();
}
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
let toastTimer;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 3500);
}
const ACTIVE = new Set(["downloading", "processing"]);
const arrName = (k) => (k === "radarr" ? "Radarr" : "Sonarr");

// ---------- rendering ----------
function describe(j) {
  const size = j.size ? bytes(j.size) : null;
  let sub = "", state = "";
  switch (j.status) {
    case "queued":
      sub = j.downloaded ? `${bytes(j.downloaded)} of ${size ?? "?"} (${(j.progress ?? 0).toFixed(1)}%)` : j.url;
      state = "Queued"; break;
    case "downloading":
      sub = `${bytes(j.downloaded)} of ${size ?? "?"}` + (j.progress != null ? ` (${j.progress.toFixed(1)}%)` : "")
        + (j.eta != null ? ` — ${dur(j.eta)} remaining` : "");
      state = `Downloading${j.speed ? ` at ${bytes(j.speed)}/s` : ""}`; break;
    case "processing":
      sub = size ? `${size}` : ""; state = "Processing (merging / converting)…"; break;
    case "paused":
      sub = j.downloaded ? `${bytes(j.downloaded)} of ${size ?? "?"} (${(j.progress ?? 0).toFixed(1)}%)` : j.url;
      state = "Paused"; break;
    case "done":
      sub = `${size ?? ""}${j.files.length > 1 ? ` — ${j.files.length} files` : ""} — finished ${ago(j.finished_at)}`;
      state = "Finished"; break;
    case "error":
      sub = j.url; state = `Error: ${j.error ?? "unknown"}`; break;
  }
  return { sub, state };
}

function render(j) {
  let el = rows.get(j.id);
  if (!el) {
    el = document.createElement("div");
    el.className = "job";
    el.dataset.id = j.id;
    el.innerHTML = `<div class="name"></div><span class="badge" hidden></span>
      <div class="sub"></div><div class="bar"><i></i></div><div class="state"></div>`;
    rows.set(j.id, el);
  }
  const { sub, state } = describe(j);
  el.className = `job s-${j.status}${selected.has(j.id) ? " selected" : ""}`;
  el.querySelector(".name").textContent = j.release_name || j.title || j.url;
  el.querySelector(".name").title = j.title || j.url;
  const badge = el.querySelector(".badge");
  badge.hidden = !j.arr;
  if (j.arr) badge.textContent = arrName(j.arr);
  el.querySelector(".sub").textContent = sub;
  el.querySelector(".bar > i").style.width = `${j.status === "processing" ? 100 : (j.progress ?? 0)}%`;
  const st = el.querySelector(".state");
  st.textContent = state;
  if (j.arr && j.arr_status) {
    const ok = j.arr_status === "imported" || j.arr_status === "scan completed";
    const span = document.createElement("span");
    span.className = ok ? "arr-ok" : (j.arr_status.startsWith("scan") || j.arr_status === "queued" ? "" : "arr-bad");
    span.textContent = ` · ${arrName(j.arr)}: ${j.arr_status}`;
    st.appendChild(span);
  }
  return el;
}

function matches(j) {
  const f = $("#filter").value;
  if (f === "active" && !ACTIVE.has(j.status)) return false;
  if (!["all", "active"].includes(f) && j.status !== f) return false;
  const q = $("#search").value.trim().toLowerCase();
  if (q && ![j.title, j.url, j.release_name].some((s) => (s || "").toLowerCase().includes(q))) return false;
  return true;
}

function renderAll() {
  const list = $("#list");
  const ordered = [...jobs.values()].sort((a, b) => b.id - a.id);
  const frag = document.createDocumentFragment();
  for (const j of ordered) {
    const el = render(j);
    el.hidden = !matches(j);
    frag.appendChild(el);
  }
  list.querySelectorAll(".job").forEach((el) => el.remove());
  list.appendChild(frag);
  $("#empty").hidden = jobs.size > 0;
  updateChrome();
}

function upsert(j) {
  const isNew = !jobs.has(j.id);
  jobs.set(j.id, j);
  if (isNew) return renderAll();
  const el = render(j);
  el.hidden = !matches(j);
  updateChrome();
  if (detailId === j.id) fillDetails(j, false);
}

function removeJob(id) {
  jobs.delete(id); selected.delete(id);
  rows.get(id)?.remove(); rows.delete(id);
  if (detailId === id) closeDetails();
  $("#empty").hidden = jobs.size > 0;
  updateChrome();
}

function updateChrome() {
  const sel = [...selected].map((id) => jobs.get(id)).filter(Boolean);
  $("#btn-resume").disabled = !sel.some((j) => ["paused", "error"].includes(j.status));
  $("#btn-pause").disabled = !sel.some((j) => ACTIVE.has(j.status) || j.status === "queued");
  $("#btn-remove").disabled = sel.length === 0;
  let speed = 0, active = 0, queued = 0, done = 0, err = 0;
  for (const j of jobs.values()) {
    if (ACTIVE.has(j.status)) { active++; speed += j.speed || 0; }
    else if (j.status === "queued") queued++;
    else if (j.status === "done") done++;
    else if (j.status === "error") err++;
  }
  $("#sb-speed").textContent = `↓ ${bytes(speed)}/s`;
  $("#sb-counts").textContent = `${active} active · ${queued} queued · ${done} finished` + (err ? ` · ${err} failed` : "");
  document.title = active ? `Spool — ↓ ${bytes(speed)}/s` : "Spool";
}

// ---------- selection ----------
$("#list").addEventListener("click", (e) => {
  const el = e.target.closest(".job");
  if (!el) { selected.clear(); renderAll(); return; }
  const id = +el.dataset.id;
  if (e.shiftKey && anchor != null) {
    const visible = [...$("#list").querySelectorAll(".job:not([hidden])")].map((r) => +r.dataset.id);
    const [a, b] = [visible.indexOf(anchor), visible.indexOf(id)].sort((x, y) => x - y);
    if (!(e.ctrlKey || e.metaKey)) selected.clear();
    visible.slice(a, b + 1).forEach((x) => selected.add(x));
  } else if (e.ctrlKey || e.metaKey) {
    selected.has(id) ? selected.delete(id) : selected.add(id);
    anchor = id;
  } else {
    selected.clear(); selected.add(id); anchor = id;
    if (detailId != null) openDetails(id);
  }
  for (const [rid, r] of rows) r.classList.toggle("selected", selected.has(rid));
  updateChrome();
});
$("#list").addEventListener("dblclick", (e) => {
  const el = e.target.closest(".job");
  if (el) openDetails(+el.dataset.id);
});

// ---------- toolbar actions ----------
async function forSelected(fn) {
  const ids = [...selected];
  const results = await Promise.allSettled(ids.map(fn));
  const failed = results.filter((r) => r.status === "rejected");
  if (failed.length) toast(failed[0].reason.message);
}
$("#btn-resume").onclick = () => forSelected((id) => api(`/api/jobs/${id}/resume`, { method: "POST" }));
$("#btn-pause").onclick = () => forSelected((id) => api(`/api/jobs/${id}/pause`, { method: "POST" }));
$("#btn-remove").onclick = () => {
  const n = selected.size;
  const first = jobs.get([...selected][0]);
  $("#rm-title").textContent = n > 1 ? `Remove ${n} downloads?` : "Remove download?";
  $("#rm-text").textContent = n > 1 ? "They will be removed from the list." : (first?.title || first?.url || "");
  $("#rm-files").checked = false;
  $("#dlg-remove").showModal();
};
$("#rm-cancel").onclick = () => $("#dlg-remove").close();
$("#dlg-remove form").addEventListener("submit", (e) => {
  e.preventDefault();
  const files = $("#rm-files").checked;
  $("#dlg-remove").close();
  forSelected((id) => api(`/api/jobs/${id}?delete_files=${files}`, { method: "DELETE" }));
});
$("#filter").onchange = $("#search").oninput = () => {
  for (const j of jobs.values()) rows.get(j.id).hidden = !matches(j);
};

// ---------- add dialog ----------
function openAdd(prefill = "") {
  $("#add-error").hidden = true;
  $("#add-urls").value = prefill;
  $("#add-release").value = "";
  $("#release-check").hidden = true;
  $("#match-list").hidden = true;
  document.querySelector('#add-arr input[value=""]').checked = true;
  syncArr();
  $("#dlg-add").showModal();
  (prefill ? $("#add-preset") : $("#add-urls")).focus();
}
function syncArr() {
  const v = document.querySelector("#add-arr input:checked").value;
  $("#release-wrap").hidden = !v;
  $("#match-wrap").hidden = v !== "sonarr";
  if (v === "sonarr") loadSeries();
  $("#add-release").required = !!v;
  $("#add-release").placeholder = v === "radarr" ? "Movie Name (2024)" : "Show Name - S01E05";
  $("#release-hint").textContent = v === "radarr"
    ? "Radarr identifies the movie from this name: title and year."
    : "Sonarr identifies the episode from this name: series, season and episode (S01E05) or air date (2024-03-15).";
}
$("#add-arr").addEventListener("change", () => { $("#release-check").hidden = true; syncArr(); });

// ---------- release-name check (asks the *arr how it parses the name) ----------
const pad = (n) => String(n).padStart(2, "0");
function describeParse(kind, r) {
  if (!r.parsed) return [false, `${arrName(kind)} can't parse this name`];
  if (kind === "radarr") {
    const name = `${r.title ?? "?"}${r.year ? ` (${r.year})` : ""}`;
    return r.match ? [true, `✓ ${r.match}`] : [false, `Parsed as ${name}, but it's not in Radarr`];
  }
  const where = r.air_date ? r.air_date
    : r.episodes.length ? r.episodes.map((e) => `S${pad(r.season)}E${pad(e)}`).join(", ")
    : r.season != null ? `season ${r.season}` : "";
  if (!r.match) return [false, `Parsed as ${r.title ?? "?"} ${where}, but the series is not in Sonarr`];
  if (!r.matched_episodes.length) return [false, `${r.match} ${where}: no such episode in Sonarr`];
  const eps = r.matched_episodes.map((e) => `S${pad(e.season)}E${pad(e.episode)}${e.title ? ` "${e.title}"` : ""}`);
  return [true, `✓ ${r.match}, ${eps.join(", ")}`];
}
$("#add-check").onclick = async () => {
  const kind = document.querySelector("#add-arr input:checked").value;
  const title = $("#add-release").value.trim();
  const out = $("#release-check");
  if (!kind || !title) return;
  out.className = "muted"; out.textContent = "Checking…"; out.hidden = false;
  try {
    const [ok, msg] = describeParse(kind, await api(`/api/arr/${kind}/parse?title=${encodeURIComponent(title)}`));
    out.className = ok ? "ok" : "bad"; out.textContent = msg;
  } catch (err) {
    out.className = "bad"; out.textContent = err.message;
  }
};
$("#add-release").addEventListener("input", () => ($("#release-check").hidden = true));

// ---------- auto-match: video URL -> Sonarr episode ----------
let seriesLoaded = false;
async function loadSeries() {
  if (seriesLoaded) return;
  try {
    const list = await api("/api/arr/sonarr/series");
    const sel = $("#add-series");
    for (const s of list) sel.add(new Option(s.year ? `${s.title} (${s.year})` : s.title, s.id));
    seriesLoaded = true;
  } catch { /* the dropdown keeps "detect automatically"; Match reports the real error */ }
}
function pickCandidate(btn, c) {
  $("#add-release").value = c.release_name;
  for (const b of document.querySelectorAll("#match-list .cand")) b.classList.toggle("on", b === btn);
  $("#add-check").click();
}
$("#add-match").onclick = async () => {
  const url = $("#add-urls").value.split(/\s+/).filter(Boolean);
  const out = $("#match-list");
  out.replaceChildren(); out.hidden = false;
  const note = (cls, text) => { const p = document.createElement("small"); p.className = cls; p.textContent = text; out.append(p); };
  if (url.length !== 1) return note("bad", "Matching needs exactly one URL");
  const btn = $("#add-match"); btn.disabled = true;
  note("muted", "Reading video info…");
  try {
    const series = $("#add-series").value;
    const r = await api("/api/arr/sonarr/match", { method: "POST", body: { url: url[0], series_id: series ? +series : null } });
    out.replaceChildren();
    const v = r.video;
    note("muted", `Video: "${v.title ?? "?"}"${v.upload_date ? `, uploaded ${v.upload_date}` : ""}${v.channel ? ` by ${v.channel}` : ""}`);
    if (r.error) return note("bad", r.error);
    if (!series) note("muted", `Series detected: ${r.series.title} (change it above if wrong)`);
    if (!r.candidates.length) return note("bad", `No episode of ${r.series.title} matches this video's title or air date`);
    r.candidates.forEach((c, i) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn cand";
      b.innerHTML = `<b>S${pad(c.season)}E${pad(c.episode)}</b><span>${esc(c.title || "")}</span>`
        + `<span class="muted">${esc(c.reason)} · ${Math.round(c.score * 100)}%</span>`;
      b.onclick = () => pickCandidate(b, c);
      out.append(b);
      if (i === 0) pickCandidate(b, c);
    });
  } catch (err) {
    out.replaceChildren(); note("bad", err.message);
  } finally {
    btn.disabled = false;
  }
};
$("#btn-add").onclick = () => openAdd();
$("#add-cancel").onclick = () => $("#dlg-add").close();
$("#form-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const arr = document.querySelector("#add-arr input:checked").value || null;
  const body = {
    urls: $("#add-urls").value.split(/\s+/).filter(Boolean),
    preset: $("#add-preset").value,
    subfolder: $("#add-sub").value.trim() || null,
    playlist: $("#add-playlist").checked,
    arr, release_name: arr ? $("#add-release").value.trim() : null,
  };
  try {
    const created = await api("/api/jobs", { method: "POST", body });
    created.forEach(upsert);
    $("#dlg-add").close();
  } catch (err) {
    $("#add-error").textContent = err.message; $("#add-error").hidden = false;
  }
});

// paste / drop a link anywhere
const looksLikeUrl = (s) => /^https?:\/\/\S+$/i.test((s || "").trim().split(/\s+/)[0] || "");
document.addEventListener("paste", (e) => {
  if (document.querySelector("dialog[open]") || ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  const text = e.clipboardData.getData("text");
  if (looksLikeUrl(text)) { e.preventDefault(); openAdd(text.trim()); }
});
document.addEventListener("dragover", (e) => { e.preventDefault(); document.body.classList.add("dragging"); });
document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) document.body.classList.remove("dragging"); });
document.addEventListener("drop", (e) => {
  e.preventDefault(); document.body.classList.remove("dragging");
  const text = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text");
  if (looksLikeUrl(text)) openAdd(text.trim());
});
document.addEventListener("keydown", (e) => {
  if (document.querySelector("dialog[open]") || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  if (e.key === "a" && !e.ctrlKey && !e.metaKey) { e.preventDefault(); openAdd(); }
  if ((e.key === "Delete" || e.key === "Backspace") && selected.size) { e.preventDefault(); $("#btn-remove").click(); }
  if (e.key === "Escape") closeDetails();
  if ((e.ctrlKey || e.metaKey) && e.key === "a") {
    e.preventDefault();
    for (const j of jobs.values()) if (!rows.get(j.id).hidden) selected.add(j.id);
    for (const [rid, r] of rows) r.classList.toggle("selected", selected.has(rid));
    updateChrome();
  }
});

// ---------- details drawer ----------
async function openDetails(id) {
  const j = jobs.get(id); if (!j) return;
  detailId = id;
  $("#details").hidden = false;
  fillDetails(j, true);
}
function closeDetails() { detailId = null; $("#details").hidden = true; }
$("#d-close").onclick = closeDetails;
let logTimer = null;
function fillDetails(j, fetchLog) {
  $("#d-title").textContent = j.release_name || j.title || j.url;
  const rowsHtml = [
    ["Status", j.status + (j.error ? ` — ${j.error}` : "")],
    ["URL", `<a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.url)}</a>`, true],
    ["Title", j.title],
    ["Quality", info.presets[j.preset] || j.preset],
    ["Size", j.size ? bytes(j.size) : null],
    ["Saved to", j.output_path],
    ["Files", j.files.length > 1 ? j.files.map((f) => f.split("/").pop()).join("\n") : null],
    [arrName(j.arr), j.arr ? (j.arr_status || "waiting for download") : null],
    ["Added", new Date(j.created_at * 1000).toLocaleString()],
  ].filter((r) => r[1]);
  $("#d-meta").innerHTML = rowsHtml.map(([k, v, raw]) => `<dt>${esc(k)}</dt><dd>${raw ? v : esc(v)}</dd>`).join("");
  $("#d-retry").hidden = ACTIVE.has(j.status) || j.status === "queued";
  $("#d-retry").textContent = j.status === "done" ? "Download again" : j.status === "paused" ? "Resume" : "Retry download";
  $("#d-arr").hidden = !(j.arr && j.status === "done" && info.arr[j.arr]);
  $("#d-arr").textContent = `Send to ${arrName(j.arr)} again`;
  clearTimeout(logTimer);
  if (fetchLog || ACTIVE.has(j.status)) logTimer = setTimeout(() => loadLog(j.id), fetchLog ? 0 : 800);
}
async function loadLog(id) {
  if (detailId !== id) return;
  try {
    const log = await (await fetch(`/api/jobs/${id}/log`)).text();
    const pre = $("#d-log");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
    pre.textContent = log || "(empty)";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } catch { /* ignore */ }
}
$("#d-retry").onclick = () => {
  const j = jobs.get(detailId); if (!j) return;
  const action = j.status === "paused" || j.status === "error" ? "resume" : "retry";
  api(`/api/jobs/${j.id}/${action}`, { method: "POST" }).catch((e) => toast(e.message));
};
$("#d-arr").onclick = () => {
  api(`/api/jobs/${detailId}/arr`, { method: "POST" }).then(() => toast("Sent")).catch((e) => toast(e.message));
};

// ---------- settings ----------
function renderCookies(c) {
  const chip = $("#sb-cookies");
  if (!c.present) {
    chip.className = "chip off"; chip.textContent = "No cookies";
    $("#ck-status").innerHTML = "No cookies loaded. Downloads run anonymously.";
  } else if (!c.valid) {
    chip.className = "chip off"; chip.textContent = "Cookies invalid";
    $("#ck-status").innerHTML = `<span class="error">cookies.txt is present but invalid: ${esc(c.error)}</span>`;
  } else {
    chip.className = "chip ok"; chip.textContent = `Cookies · ${c.domains.length} site${c.domains.length === 1 ? "" : "s"}`;
    const doms = c.domains.slice(0, 8).join(", ") + (c.domains.length > 8 ? `, +${c.domains.length - 8} more` : "");
    $("#ck-status").innerHTML = `<b>${c.entries} cookies</b> for ${esc(doms)}<br><span class="muted">Updated ${ago(c.updated_at)}</span>`;
  }
  $("#ck-delete").hidden = !c.present;
}
async function saveCookies(text) {
  $("#ck-error").hidden = true;
  try {
    renderCookies(await api("/api/cookies", { method: "PUT", body: { text } }));
    $("#ck-text").value = ""; $("#ck-paste").hidden = true;
    toast("Cookies saved");
  } catch (e) { $("#ck-error").textContent = e.message; $("#ck-error").hidden = false; }
}
$("#ck-file").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  await saveCookies(await f.text()); e.target.value = "";
};
$("#ck-paste-toggle").onclick = () => { $("#ck-paste").hidden = !$("#ck-paste").hidden; $("#ck-text").focus(); };
$("#ck-save").onclick = () => saveCookies($("#ck-text").value);
$("#ck-delete").onclick = async () => {
  if (!confirm("Remove the stored cookies?")) return;
  renderCookies(await api("/api/cookies", { method: "DELETE" }));
};
$("#btn-settings").onclick = $("#sb-cookies").onclick = () => $("#dlg-settings").showModal();
$("#set-close").onclick = () => $("#dlg-settings").close();

function renderArr() {
  const box = $("#arr-list");
  box.innerHTML = "";
  for (const kind of ["sonarr", "radarr"]) {
    const c = info.arr[kind];
    const row = document.createElement("div");
    row.className = "arr-row";
    row.innerHTML = c
      ? `<b>${arrName(kind)}</b><span class="muted">${esc(c.url)} · sees downloads at ${esc(c.downloads_path)}</span><button class="btn">Test</button>`
      : `<b>${arrName(kind)}</b><span class="muted">Not configured — set ${kind.toUpperCase()}_URL and ${kind.toUpperCase()}_API_KEY</span>`;
    const btn = row.querySelector("button");
    if (btn) btn.onclick = async () => {
      btn.textContent = "Testing…";
      try {
        const r = await api(`/api/arr/${kind}/test`, { method: "POST" });
        btn.textContent = `OK · v${r.version}`;
      } catch (e) { btn.textContent = "Failed"; toast(e.message); }
    };
    box.appendChild(row);
  }
  for (const input of document.querySelectorAll("#add-arr input[value]:not([value=''])")) {
    input.disabled = !info.arr[input.value];
    input.parentElement.title = input.disabled ? `${arrName(input.value)} is not configured` : "";
  }
  $("#arr-box").hidden = Object.keys(info.arr).length === 0;
}

// ---------- live updates ----------
function connect() {
  const es = new EventSource("/api/events");
  es.onopen = async () => {
    $("#sb-conn").classList.add("live");
    $("#sb-conn").title = "Live updates connected";
    const list = await api("/api/jobs");     // resync after (re)connect
    jobs.clear(); rows.clear(); list.forEach((j) => jobs.set(j.id, j));
    renderAll();
  };
  es.onerror = () => { $("#sb-conn").classList.remove("live"); $("#sb-conn").title = "Reconnecting…"; };
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "job") upsert(ev.job);
    else if (ev.type === "removed") removeJob(ev.id);
    else if (ev.type === "cookies") renderCookies(ev.cookies);
  };
}

// refresh relative times ("finished 3 min ago") once a minute
setInterval(() => { for (const j of jobs.values()) if (j.status === "done") render(j); }, 60_000);

// ---------- boot ----------
(async function boot() {
  info = await api("/api/info");
  $("#add-preset").innerHTML = Object.entries(info.presets)
    .map(([k, v]) => `<option value="${k}"${k === info.default_preset ? " selected" : ""}>${esc(v)}</option>`).join("");
  $("#about").textContent = `Spool ${info.version} · yt-dlp ${info.ytdlp_version} · up to ${info.max_concurrent} downloads at once`;
  $("#bookmarklet").href = `javascript:void(window.open('${location.origin}/?add='+encodeURIComponent(location.href),'spool'))`;
  renderArr();
  renderCookies(await api("/api/cookies"));
  connect();
  const add = new URLSearchParams(location.search).get("add");
  if (add) { history.replaceState(null, "", "/"); openAdd(add); }
})().catch((e) => toast(`Could not load Spool: ${e.message}`));
