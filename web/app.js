/* FerDim web arayuzu - cok dilli (tr/en/es/ru) + turbo.
 * Sunucudan gelen her metin (dosya adi, URL, hata) DOM'a yalniz
 * textContent / value ile yazilir; HTML ayristiran hicbir yol yoktur. */
"use strict";
const TOKEN = window.FERDIM_TOKEN;
const $ = (s) => document.querySelector(s);
const CATS = ["video", "music", "compressed", "program", "document", "other"];
const FINAL = new Set(["done", "error", "canceled"]);
const MAX_MIRRORS = 24;
const ACTIONS = [["resume", "act_resume", "▶"], ["pause", "act_pause", "⏸"],
                 ["restart", "act_restart", "↻"], ["zip", "zip_title", "📦"],
                 ["remove", "act_remove", "🗑"]];
const DIGEST_LEN = { sha256: 64, sha512: 128, md5: 32, sha1: 40 };

let T = {};                 // aktif ceviri sozlugu
let LANG = localStorage.getItem("ferdimLang") || "";
let jobs = [], cfg = {}, filter = "all", lastStats = {};
let states = new Map();     // is kimligi -> onceki durum (duyuru icin)
let primed = false;         // ilk tarama yapildi mi (ilk tarama duyurulmaz)
let polling = false;
const rows = new Map();     // is kimligi -> satir ogeleri

const CAT_ICON = { video: "🎬", music: "🎵", compressed: "🗜",
                   program: "📦", document: "📄", other: "" };

/* ------------------------------------------------------------------ i18n */
const tr = (k) => (T[k] !== undefined ? T[k] : k);
function trf(k, vars) {     // trf(anahtar, 3) yada trf(anahtar, {n: 3, w: "x"})
  let s = tr(k);
  const v = vars !== null && typeof vars === "object" ? vars : { n: vars };
  for (const key in v) s = s.split("{" + key + "}").join(String(v[key]));
  return s;
}

function applyI18n() {
  for (const n of document.querySelectorAll("[data-i18n]")) {
    const v = T[n.dataset.i18n];
    if (v !== undefined) n.textContent = v;
  }
  for (const n of document.querySelectorAll("[data-i18n-ph]")) {
    const v = T[n.dataset.i18nPh];
    if (v !== undefined) n.placeholder = v;
  }
  for (const n of document.querySelectorAll("[data-i18n-title]")) {
    const v = T[n.dataset.i18nTitle];
    if (v !== undefined) n.title = v;
  }
  for (const n of document.querySelectorAll("[data-i18n-aria]")) {
    const v = T[n.dataset.i18nAria];
    if (v !== undefined) n.setAttribute("aria-label", v);
  }
  document.documentElement.lang = LANG || "tr";
  document.title = "FerDim — " + tr("app_title");
}

async function loadLang(lang) {
  // oncelik: secilen -> tarayici dili -> sunucu config
  const want = lang || LANG || (navigator.language || "").slice(0, 2);
  const r = await fetch("/api/i18n?lang=" + encodeURIComponent(want),
                        { headers: { "X-FerDim-Token": TOKEN } })
    .then((x) => x.json()).catch(() => ({}));
  if (r && r.ok) { T = r.strings || {}; LANG = r.lang; }
  applyI18n();
  // dugme adlari ve notlar kurulumda cevrildi: satirlar yeni dille kurulur
  for (const r of rows.values()) r.root.remove();
  rows.clear();
}

/* ------------------------------------------------------------- yardimcilar */
/* ogeleri olusturur; metin yalniz textContent ile yazilir, olaylar yalniz
 * dinleyici olarak baglanir (satir ici olay niteligi reddedilir) */
function dom(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const k in attrs || {}) {
    const v = attrs[k];
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (/^on/i.test(k)) throw new Error("olay niteligi kullanilmaz: " + k);
    else n.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids) if (kid != null) n.append(kid);
  return n;
}

function human(n) {
  if (n == null || n < 0) return "?";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < 4) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
const rate = (n) => human(n) + tr("per_sec");
function eta(s) {
  if (s == null || s < 0) return "--";
  s = Math.round(s);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return Math.floor(s / 3600) + "h";
}
/* sunucudaki parse_checksum_spec ile ayni kural (algo:ozet ya da uzunliktan) */
function checksumOk(s) {
  const m = /^(sha256|sha512|md5|sha1)\s*:\s*([0-9a-f]+)$/i.exec(s);
  if (m) return m[2].length === DIGEST_LEN[m[1].toLowerCase()];
  return /^[0-9a-f]+$/i.test(s) && Object.values(DIGEST_LEN).includes(s.length);
}
/* '2:00' -> '02:00' (type=time yalniz iki basamakli saati kabul eder) */
const hm = (s) => (/^\d:/.test(s || "") ? "0" + s : (s || ""));

async function api(path, body) {
  const opt = { headers: { "X-FerDim-Token": TOKEN } };
  if (body !== undefined) { opt.method = "POST"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401) { location.reload(); return {}; }
  return r.json();
}

let noticeTimer = 0;
function notice(text) {    // ekranda gorunen kisa bildirim (role=status)
  const n = $("#notice");
  n.textContent = text; n.hidden = !text;
  clearTimeout(noticeTimer);
  if (text) noticeTimer = setTimeout(() => { n.hidden = true; }, 8000);
}
function say(text) {       // ekran okuyucuya duyuru (is bitti / hata)
  const live = $("#live");
  live.textContent = "";
  setTimeout(() => { live.textContent = text; }, 60);
}

/* ------------------------------------------------------------------ satir */
function notesFor(j) {
  const out = [];
  if (j.adaptive && j.adaptive < j.segments_cfg)
    out.push(["info", "⚙ " + trf("adaptive_limit", j.adaptive)]);
  if (j.state === "error" && j.error) out.push(["bad", "⚠ " + j.error]);
  if (j.sched_wait && j.window)
    out.push(["info", "⏱ " + trf("sched_wait", { w: j.window })]);
  if (j.sources_dead > 0)
    out.push(["warn", "⛔ " + trf("mirrors_dead", { n: j.sources_dead })]);
  if (j.state === "done" && j.checksum === "warn")
    out.push(["warn", "⚠ " + tr("chk_warn")]);
  if (j.state === "done" && j.verified && j.verified.length)
    out.push(["ok", "✓ " + trf("chk_ok", { a: j.verified.join(", ") })]);
  return out;
}

function buildRow(j) {
  const btn = {};
  for (const [kind, key, glyph] of ACTIONS) {
    btn[kind] = dom("button", { class: "act", type: "button", "data-act": kind,
                                "aria-label": tr(key), title: tr(key), text: glyph });
    btn[kind].onclick = () => act(kind, j.id);
  }
  const r = {
    id: j.id, sig: "", btn,
    cat: dom("span", { class: "cat" }),
    name: dom("span", { class: "fname" }),
    notes: dom("span", { class: "notes", hidden: true }),
    url: dom("div", { class: "url" }),
    fill: dom("i"),
    plabel: dom("span", { class: "plabel" }),
    size: dom("div", { class: "c-size", role: "cell" }),
    speed: dom("div", { class: "c-speed", role: "cell" }),
    eta: dom("div", { class: "c-eta", role: "cell" }),
  };
  r.bar = dom("div", { class: "bar", role: "progressbar",
                       "aria-valuemin": "0", "aria-valuemax": "100",
                       "aria-label": tr("a11y_progress") }, r.fill, r.plabel);
  r.root = dom("div", { class: "row", role: "row" },
    dom("div", { class: "c-file", role: "cell" },
      dom("div", { class: "name" }, r.cat, r.name), r.notes, r.url),
    dom("div", { class: "c-prog", role: "cell" }, r.bar),
    r.size, r.speed, r.eta,
    dom("div", { class: "c-act", role: "cell" },
      btn.resume, btn.pause, btn.restart, btn.zip, btn.remove));
  return r;
}

function updateRow(r, j) {
  const chip = [CAT_ICON[j.category], tr("cat_" + j.category)]
    .filter(Boolean).join(" ");
  r.cat.textContent = chip;
  r.name.textContent = j.filename;
  r.url.textContent = j.url;
  r.url.title = j.fullpath || j.url;

  const notes = notesFor(j);
  const sig = JSON.stringify(notes);
  if (sig !== r.sig) {          // notlar yalniz degisince yeniden kurulur
    r.sig = sig;
    r.notes.replaceChildren(...notes.map(([cls, text]) =>
      dom("span", { class: "note " + cls, text })));
    r.notes.hidden = notes.length === 0;
  }

  const label = tr(j.verifying ? "st_verifying" : "st_" + j.state);
  const pct = j.size > 0 ? Math.min(100, Math.floor(j.done * 100 / j.size)) : null;
  if (pct == null) {            // boyut bilinmiyor: belirsiz cubuk
    r.bar.classList.add("unk");
    r.fill.style.width = "100%";
    r.bar.removeAttribute("aria-valuenow");
    r.plabel.textContent = human(j.done) + " — " + label;
  } else {
    r.bar.classList.remove("unk");
    r.fill.style.width = pct + "%";
    r.bar.setAttribute("aria-valuenow", String(pct));
    r.plabel.textContent = pct + "% — " + label;
  }
  r.bar.setAttribute("aria-valuetext", r.plabel.textContent);

  const playing = j.state === "downloading" || j.state === "connecting";
  r.size.textContent = human(j.done) + " / " + human(j.size);
  r.speed.textContent = playing ? rate(j.speed) : "";
  r.eta.textContent = playing ? eta(j.eta) : "";
  r.btn.resume.hidden =
    !["paused", "error", "queued", "canceled"].includes(j.state);
  r.btn.pause.hidden = !playing;
  r.btn.zip.hidden =
    !(j.state === "done" && /\.zip$/i.test(j.filename || ""));
}

/* anahtarli guncelleme: satirlar yerinde degisir, klavye odagi korunur */
function render() {
  const list = $("#jobList");
  const vis = jobs.filter((j) => filter === "all" || j.category === filter);
  $("#empty").hidden = vis.length > 0;
  let cursor = list.firstChild;
  for (const j of vis) {
    let r = rows.get(j.id);
    if (!r) { r = buildRow(j); rows.set(j.id, r); }
    if (r.root === cursor) {
      cursor = cursor.nextSibling;
    } else {
      list.insertBefore(r.root, cursor);
      if (cursor) cursor = cursor.nextSibling;
    }
    updateRow(r, j);
  }
  const keep = new Set(vis.map((j) => j.id));
  for (const [id, r] of rows) {
    if (!keep.has(id)) { r.root.remove(); rows.delete(id); }
  }
}

function renderStatus() {
  const sum = jobs.reduce((a, j) => a + (j.speed || 0), 0);
  $("#globalSpeed").textContent = rate(sum);
  const nm = !!lastStats.nm_connected;
  $("#nmState").textContent = (nm ? "● " : "○ ") + tr(nm ? "nm_ok" : "nm_off");
  $("#nmState").className = "conn " + (nm ? "ok" : "dim");
  $("#schedState").textContent =
    lastStats.schedule_active ? "⏱ " + tr("sched_active") : "";
  $("#dirLabel").textContent =
    tr("folder_label") + ": " + (lastStats.download_dir || "");

  const turbo = !!lastStats.turbo;
  const tb = $("#turboBtn");
  tb.classList.toggle("active", turbo);
  tb.setAttribute("aria-pressed", turbo ? "true" : "false");
  tb.textContent = turbo ? "⚡ " + tr("turbo_on") : "⚡ " + tr("turbo");
  tb.title = turbo ? tr("turbo_title") + " — " + tr("turbo_on")
                   : tr("turbo_title");
  $("#turboBar").hidden = !turbo;

  // hiz limiti secimi o an uygulanan limiti gosterir (CLI'dan degismis olsa
  // bile); sabit seceneklerde olmayan deger ozel secenekle gosterilir
  const sel = $("#limitSel"), lim = Number(lastStats.speed_limit || 0);
  const custom = $("#limitCustom");
  let hit = lim === 0;
  for (const o of sel.options)
    if (o !== custom && Number(o.value) === lim) hit = true;
  custom.hidden = custom.disabled = hit;
  custom.textContent = hit ? "" : rate(lim) + " (" + tr("limit_custom") + ")";
  custom.value = hit ? "" : String(lim);
  sel.value = String(lim);
}

function announce() {
  if (primed) {
    for (const j of jobs) {
      const old = states.get(j.id);
      if (old && old !== j.state && FINAL.has(j.state))
        say(trf("done_announce", { name: j.filename, state: tr("st_" + j.state) }));
    }
  }
  states = new Map(jobs.map((j) => [j.id, j.state]));
  primed = true;
}

async function poll() {
  if (polling) return;          // yanimda surmekte olan istek varsa atla
  polling = true;
  const conn = $("#connState");
  try {
    const s = await api("/api/stats");
    if (s && s.ok) {
      jobs = s.jobs || []; lastStats = s;
      conn.textContent = "● " + tr("daemon_ok"); conn.className = "conn ok";
    } else {
      conn.textContent = "● " + tr("daemon_down"); conn.className = "conn bad";
    }
  } catch {
    conn.textContent = "● " + tr("daemon_down"); conn.className = "conn bad";
  }
  polling = false;
  render(); renderStatus(); announce();
}

/* ----------------------------------------------------------------- eylemler */
async function act(kind, id) {
  if (kind === "remove") {
    if (!confirm(tr("confirm_remove"))) return;
    await api("/api/remove", { id, delete_files: true });
  } else if (kind === "zip") {
    const r = await api("/api/zip-preview", { id });
    if (!r.ok) { alert(tr("zip_title") + ": " + (r.error || "")); return; }
    const list = $("#zipList");
    list.replaceChildren();
    $("#zipTitle").textContent = `${tr("zip_title")} — ${r.filename} (${
      r.entries.length}${r.note || ""})`;
    for (const [name, size] of r.entries.slice(0, 400)) {
      list.append(dom("div", { class: "ziprow", role: "listitem" },
        dom("span", { class: "n", text: name }),
        dom("span", { class: "s", text: human(size) })));
    }
    if (!r.entries.length)
      list.append(dom("div", { class: "dim", text: tr("zip_empty") }));
    $("#zipDlg").showModal();
    return;
  } else {
    await api("/api/" + kind, { id });
  }
  poll();
}
$("#zipClose").onclick = () => $("#zipDlg").close();

/* ---- sekmeler (roving tabindex + ok tuslari; odak secimi takip eder) ---- */
const tablist = $('[role="tablist"]');
const tabs = () => [...tablist.querySelectorAll('[role="tab"]')];
function selectTab(btn, focus) {
  filter = btn.dataset.cat;
  for (const t of tabs()) {
    const on = t === btn;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
    t.tabIndex = on ? 0 : -1;
  }
  $("#jobPanel").setAttribute("aria-labelledby", btn.id);
  if (focus) btn.focus();
  render();
}
tablist.addEventListener("click", (e) => {
  const b = e.target.closest('[role="tab"]');
  if (b) selectTab(b);
});
tablist.addEventListener("keydown", (e) => {
  const b = e.target.closest('[role="tab"]');
  if (!b) return;
  const all = tabs(), i = all.indexOf(b);
  let n = null;
  if (e.key === "ArrowRight" || e.key === "ArrowDown") n = all[(i + 1) % all.length];
  else if (e.key === "ArrowLeft" || e.key === "ArrowUp")
    n = all[(i - 1 + all.length) % all.length];
  else if (e.key === "Home") n = all[0];
  else if (e.key === "End") n = all[all.length - 1];
  if (n) { e.preventDefault(); selectTab(n, true); }
});

$("#clearFinished").onclick = async () => { await api("/api/clear-finished"); poll(); };

/* ---- turbo ---- */
$("#turboBtn").onclick = async () => {
  await api("/api/turbo", { on: !lastStats.turbo });
  poll();
};

/* ---- yeni indirme ---- */
const addDlg = $("#addDlg");
$("#addBtn").onclick = () => {
  $("#addError").hidden = true;
  addDlg.showModal();
  $("#addUrl").focus();
};
$("#addCancel").onclick = () => addDlg.close();
$("#addUrl").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#addOk").click(); }
});
$("#addOk").onclick = async () => {
  const err = $("#addError");
  const fail = (msg) => { err.textContent = msg; err.hidden = !msg; };
  fail("");
  const url = $("#addUrl").value.trim();
  if (!/^https?:\/\/\S+$/i.test(url)) { fail(tr("err_url")); $("#addUrl").focus(); return; }
  const chk = $("#addChk").value.trim();
  if (chk && !checksumOk(chk)) {   // sunucu da reddeder; erken soyle
    fail(tr("cli_bad_checksum")); $("#addChk").focus(); return;
  }
  const mirrors = $("#addMirrors").value.split(/\s+/).map((s) => s.trim())
    .filter((u) => /^https?:\/\//i.test(u)).slice(0, MAX_MIRRORS);
  const seg = parseInt($("#addSeg").value, 10);
  const r = await api("/api/add", {
    url, filename: $("#addName").value.trim(),
    segments: seg >= 1 && seg <= 32 ? seg : 0,   // 0 = sunucu varsayilani
    checksum: chk, mirrors,
    paused: !$("#addNow").checked,
  });
  if (!r.ok) { fail(tr("add_failed") + " " + (r.error || "")); return; }
  for (const id of ["addUrl", "addName", "addChk", "addMirrors", "addSeg"])
    $("#" + id).value = "";
  addDlg.close();
  if (r.ids && r.ids.length > 1) say(trf("cli_added_n", r.ids.length));
  poll();
};

/* ---- hiz limiti (turbo acikken limit secmek turbo'yu kapatir) ---- */
$("#limitSel").onchange = async (e) => {
  await api("/api/limit", { value: parseInt(e.target.value, 10) || 0 });
  poll();
};

/* ---- ayarlar ---- */
const setDlg = $("#setDlg");
function buildCatRows(cats) {
  const box = $("#catSched");
  box.replaceChildren();
  for (const c of CATS) {
    const w = (cats && cats[c]) || {};
    const name = tr("tab_" + c);
    const on = dom("input", { type: "checkbox", "data-w": "on", "aria-label": name });
    const a = dom("input", { type: "time", "data-w": "start",
                             value: hm(w.start) || "02:00",
                             "aria-label": name + " — " + tr("win_start") });
    const b = dom("input", { type: "time", "data-w": "stop",
                             value: hm(w.stop) || "08:00",
                             "aria-label": name + " — " + tr("win_stop") });
    on.checked = !!(w.start && w.stop);
    const sync = () => { a.disabled = b.disabled = !on.checked; };
    on.onchange = sync;
    sync();
    box.append(dom("div", { class: "catrow", "data-cat": c }, on,
      dom("span", { class: "catname", text: name }), a,
      dom("span", { class: "dash", text: "–", "aria-hidden": "true" }), b));
  }
}
function readCatRows() {
  const out = {};
  for (const row of $("#catSched").children) {
    const [on, a, b] = row.querySelectorAll("input");
    if (on.checked && a.value && b.value)
      out[row.dataset.cat] = { start: a.value, stop: b.value };
  }
  return out;
}
$("#settingsBtn").onclick = async () => {
  const r = await api("/api/config");
  if (r && r.config) cfg = r.config;
  const sch = cfg.schedule || {};
  $("#setDir").value = cfg.download_dir || "";
  $("#setDir").placeholder = lastStats.download_dir || "";
  $("#setLang").value = cfg.language || "auto";
  $("#setSeg").value = cfg.segments ?? 8;
  $("#setConc").value = cfg.max_concurrent ?? 3;
  $("#setSched").checked = !!sch.enabled;
  $("#setSchedStart").value = hm(sch.start) || "02:00";
  $("#setSchedStop").value = hm(sch.stop) || "08:00";
  buildCatRows(sch.categories);
  $("#setWatchDirs").value = (cfg.watch_dirs || []).join("\n");
  $("#setWatchDone").value = cfg.watch_done || "delete";
  $("#setSidecar").checked = !!cfg.checksum_sidecar;
  $("#setRetries").value = cfg.retries ?? 5;
  $("#setTimeout").value = cfg.timeout ?? 30;
  $("#setStall").value = cfg.stall_timeout ?? 20;
  setDlg.showModal();
};
$("#setClose").onclick = () => setDlg.close();
$("#setSave").onclick = async () => {
  const num = (id, lo, hi, fallback) => {
    const v = parseInt($(id).value, 10);
    return Number.isNaN(v) ? fallback : Math.min(hi, Math.max(lo, v));
  };
  const lang = $("#setLang").value;
  const r = await api("/api/config", { config: {
    download_dir: $("#setDir").value.trim() || null,
    language: lang,
    segments: num("#setSeg", 1, 32, cfg.segments ?? 8),
    max_concurrent: num("#setConc", 1, 32, cfg.max_concurrent ?? 3),
    schedule: {
      enabled: $("#setSched").checked,
      start: $("#setSchedStart").value || "02:00",
      stop: $("#setSchedStop").value || "08:00",
      categories: readCatRows(),    // config-set tum 'schedule'i degistirdigi
    },                              // icin kategori pencereleri de gonderilir
    watch_dirs: $("#setWatchDirs").value.split("\n")
      .map((s) => s.trim()).filter(Boolean),
    watch_done: $("#setWatchDone").value,
    checksum_sidecar: $("#setSidecar").checked,
    retries: num("#setRetries", 0, 20, cfg.retries ?? 5),
    timeout: num("#setTimeout", 5, 300, cfg.timeout ?? 30),
    stall_timeout: num("#setStall", 5, 120, cfg.stall_timeout ?? 20),
  } });
  if (r && r.config) cfg = r.config;
  notice(r && r.warning === "download_dir" ? tr("warn_dir") : tr("saved"));
  localStorage.setItem("ferdimLang", lang === "auto" ? "" : lang);
  setDlg.close();
  await loadLang(lang);
  poll();
};

/* ---- baslangic ---- */
(async () => {
  await loadLang();
  const r = await api("/api/config").catch(() => ({}));
  if (r && r.config) cfg = r.config;
  await poll();
  setInterval(poll, 500);
})();
