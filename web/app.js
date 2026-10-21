/* FerDim web arayuzu - cok dilli (tr/en/es/ru) + turbo */
"use strict";
const TOKEN = window.FERDIM_TOKEN;
const $ = (s) => document.querySelector(s);

let T = {};            // aktif ceviri sozlugu
let LANG = localStorage.getItem("ferdimLang") || "";
let jobs = [], cfg = {}, filter = "all", lastStats = {};

const CAT_ICON = { video: "🎬", music: "🎵", compressed: "🗜",
                   program: "📦", document: "📄", other: "File" };
const tr = (k) => (T[k] !== undefined ? T[k] : k);
const trf = (k, n) => tr(k).replace("{n}", String(n));

function human(n) {
  if (n == null || n < 0) return "?";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < 4) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
function eta(s) {
  if (s == null || s < 0) return "--";
  s = Math.round(s);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return Math.floor(s / 3600) + "h";
}
async function api(path, body) {
  const opt = { headers: { "X-FerDim-Token": TOKEN } };
  if (body) { opt.method = "POST"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status == 401) { location.reload(); return {}; }
  return r.json();
}

/* ------------------------------------------------------------ i18n */
function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const k = el.dataset.i18n;
    const v = T[k];
    if (v !== undefined) el.textContent = v;
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    const v = T[el.dataset.i18nPh];
    if (v !== undefined) el.placeholder = v;
  });
  document.title = "FerDim — " + tr("app_title");
}
async function loadLang(lang) {
  // oncelik: secilen -> tarayici dili -> sunucu config
  const tryLang = lang || LANG ||
    (navigator.language || "").slice(0, 2);
  const r = await fetch("/api/i18n?lang=" + encodeURIComponent(tryLang),
                        { headers: { "X-FerDim-Token": TOKEN } })
    .then((x) => x.json()).catch(() => ({}));
  if (r && r.ok) { T = r.strings || {}; LANG = r.lang; }
  applyI18n();
}

/* ---------------------------------------------------------------- render */
function row(j) {
  const pct = j.size > 0 ? Math.floor(j.done * 100 / j.size) : null;
  const el = document.createElement("div"); el.className = "row";
  const playing = j.state === "downloading" || j.state === "connecting";
  const st = `<span class="state ${j.state}">${tr("st_" + j.state)}${j.state === "error" && j.error ? " ⚠" : ""}</span>`;
  const prog = pct == null
    ? `<div class="bar unk"><i></i><span>${human(j.done)}</span></div>`
    : `<div class="bar"><i style="width:${pct}%"></i><span>${pct}% — ${st}</span></div>`;
  const acts = [];
  if (["paused", "error", "queued", "canceled"].includes(j.state))
    acts.push(`<button class="act" title="${tr("act_resume")}" data-act="resume">▶</button>`);
  if (playing || j.state === "paused")
    acts.push(`<button class="act" title="${tr("act_pause")}" data-act="pause">⏸</button>`);
  acts.push(`<button class="act" title="${tr("act_restart")}" data-act="restart">↻</button>`);
  if (j.state === "done" && /\.zip$/i.test(j.filename))
    acts.push(`<button class="act" title="${tr("zip_title")}" data-act="zip">📦</button>`);
  acts.push(`<button class="act" title="${tr("act_remove")}" data-act="remove">🗑</button>`);
  const adaptive = (j.adaptive && j.adaptive < j.segments_cfg)
    ? `⚙ ${trf("adaptive_limit", j.adaptive)} · ` : "";
  el.innerHTML = `
    <div class="c-file">
      <div class="name"><span class="cat">${CAT_ICON[j.category] || "File"} ${tr("cat_" + j.category)}</span>${j.filename}</div>
      <div class="url" title="${j.error || j.url}">${adaptive}${j.error ? "⚠ " + j.error : j.url}</div>
    </div>
    ${prog}
    <div class="c-size">${human(j.done)} / ${human(j.size)}</div>
    <div class="c-speed">${playing ? human(j.speed) + "/s" : ""}</div>
    <div class="c-eta">${playing ? eta(j.eta) : ""}</div>
    <div class="c-act">${acts.join("")}</div>`;
  el.title = j.fullpath || "";
  el.querySelectorAll(".act").forEach((b) => b.onclick = () => act(b.dataset.act, j.id));
  return el;
}

function render() {
  const list = $("#jobList"); list.innerHTML = "";
  const vis = jobs.filter((j) => filter === "all" || j.category === filter);
  $("#empty").hidden = vis.length > 0;
  for (const j of vis) list.appendChild(row(j));
  const speed = jobs.reduce((a, j) => a + (j.speed || 0), 0);
  $("#globalSpeed").textContent = human(speed) + "/s";
  $("#nmState").textContent = lastStats.nm_connected ? "● " + tr("nm_ok") : "○ " + tr("nm_off");
  $("#nmState").className = "conn " + (lastStats.nm_connected ? "ok" : "dim");
  $("#schedState").textContent = lastStats.schedule_active ? "⏱ " + tr("sched_active") : "";
  $("#dirLabel").textContent = tr("folder_label") + ": " + (lastStats.download_dir || "");
  const turbo = !!lastStats.turbo;
  $("#turboBtn").classList.toggle("active", turbo);
  $("#turboBtn").textContent = turbo ? "⚡ " + tr("turbo_on") : "⚡ " + tr("turbo");
  $("#turboBar").hidden = !turbo;
}

async function poll() {
  try {
    const s = await api("/api/stats");
    if (s && s.ok) {
      jobs = s.jobs || []; lastStats = s;
      $("#connState").textContent = "● " + tr("daemon_ok");
      $("#connState").className = "conn ok";
    }
  } catch {
    $("#connState").textContent = "● " + tr("daemon_down");
    $("#connState").className = "conn bad";
  }
  render();
}

async function act(kind, id) {
  if (kind === "remove") {
    if (!confirm(tr("confirm_remove"))) return;
    await api("/api/remove", { id, delete_files: true });
  } else if (kind === "zip") {
    const r = await api("/api/zip-preview", { id });
    const list = $("#zipList");
    list.innerHTML = "";
    if (r.ok) {
      $("#zipTitle").textContent = `${tr("zip_title")} — ${
        r.filename} (${r.entries.length}${r.note})`;
      for (const [name, size] of r.entries.slice(0, 400)) {
        const d = document.createElement("div");
        d.className = "ziprow";
        d.innerHTML = `<span class="n"></span><span class="s">${human(size)}</span>`;
        d.querySelector(".n").textContent = name;
        list.appendChild(d);
      }
      if (!r.entries.length) list.innerHTML =
        `<div class="dim">${tr("zip_empty")}</div>`;
      $("#zipDlg").showModal();
    } else {
      alert(tr("zip_title") + ": " + (r.error || ""));
    }
  } else if (kind === "pause") await api("/api/pause", { id });
  else if (kind === "resume") await api("/api/resume", { id });
  else if (kind === "restart") await api("/api/restart", { id });
  poll();
}

/* ---- sekmeler ---- */
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-cat]");
  if (!b) return;
  filter = b.dataset.cat;
  document.querySelectorAll("#tabs button[data-cat]")
    .forEach((x) => x.classList.toggle("active", x === b));
  render();
});
$("#clearFinished").onclick = async () => { await api("/api/clear-finished"); poll(); };

/* ---- turbo ---- */
$("#turboBtn").onclick = async () => {
  await api("/api/turbo", { on: !lastStats.turbo });
  poll();
};

/* ---- yeni indirme ---- */
const addDlg = $("#addDlg");
$("#addBtn").onclick = () => { addDlg.showModal(); $("#addUrl").focus(); };
$("#addCancel").onclick = () => addDlg.close();
$("#addUrl").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#addOk").click(); });
$("#addOk").onclick = async () => {
  const url = $("#addUrl").value.trim();
  if (!/^https?:\/\//i.test(url)) { $("#addUrl").focus(); return; }
  await api("/api/add", {
    url, filename: $("#addName").value.trim(),
    segments: parseInt($("#addSeg").value) || 32,
    sha256: $("#addSha").value.trim(),
  });
  $("#addSha").value = "";
  addDlg.close();
  $("#addUrl").value = ""; $("#addName").value = "";
  poll();
};

$("#zipClose").onclick = () => $("#zipDlg").close();

/* ---- hiz limiti ---- */
$("#limitSel").onchange = async (e) => {
  await api("/api/limit", { value: parseInt(e.target.value) }); poll();
};

/* ---- ayarlar ---- */
const setDlg = $("#setDlg");
$("#settingsBtn").onclick = async () => {
  const r = await api("/api/config"); cfg = r.config || {};
  $("#setDir").value = cfg.download_dir || lastStats.download_dir || "";
  $("#setSeg").value = cfg.segments || 8;
  $("#setConc").value = cfg.max_concurrent || 3;
  $("#setSchedStart").value = (cfg.schedule || {}).start || "02:00";
  $("#setSchedStop").value = (cfg.schedule || {}).stop || "08:00";
  $("#setSched").checked = !!(cfg.schedule || {}).enabled;
  $("#setLang").value = cfg.language || "auto";
  setDlg.showModal();
};
$("#setClose").onclick = () => setDlg.close();
$("#setSave").onclick = async () => {
  const lang = $("#setLang").value;
  await api("/api/config", { config: {
    download_dir: $("#setDir").value.trim() || null,
    segments: parseInt($("#setSeg").value) || 8,
    max_concurrent: parseInt($("#setConc").value) || 3,
    language: lang,
    schedule: {
      enabled: $("#setSched").checked,
      start: $("#setSchedStart").value, stop: $("#setSchedStop").value,
    },
  }});
  localStorage.setItem("ferdimLang", lang === "auto" ? "" : lang);
  setDlg.close();
  await loadLang(lang);
  poll();
};

/* ---- baslangic ---- */
(async () => {
  await loadLang();
  const r = await api("/api/config").catch(() => ({}));
  cfg = r.config || {};
  const lim = document.querySelector(
    '#limitSel option[value="' + (cfg.speed_limit || 0) + '"]');
  if (lim) lim.selected = true;
  poll();
  setInterval(poll, 500);
})();
