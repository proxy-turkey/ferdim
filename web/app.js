/* FerDim web arayuzu - cok dilli (tr/en/es/ru) + turbo.
 * Sunucudan gelen her metin (dosya adi, URL, hata) DOM'a yalniz
 * textContent / value ile yazilir; HTML ayristiran hicbir yol yoktur.
 * Sayfa token tasimaz: adresteki tek kullanimlik bilet (#s=...) bir kez
 * token'a cevrilir, token sekme omru boyunca sessionStorage'da durur. */
"use strict";
let TOKEN = sessionStorage.getItem("ferdimToken") || "";
const $ = (s) => document.querySelector(s);
const CATS = ["video", "music", "compressed", "program", "document", "other"];
const FINAL = new Set(["done", "error", "canceled"]);
const MAX_MIRRORS = 24;
const ACTIONS = [["resume", "act_resume", "▶"], ["pause", "act_pause", "⏸"],
                 ["restart", "act_restart", "↻"], ["zip", "zip_title", "📦"],
                 ["remove", "act_remove", "🗑"]];
const DIGEST_LEN = { sha256: 64, sha512: 128, md5: 32, sha1: 40 };
const ARCH_RE = /\.(zip|tar|tgz|tbz2|txz|tar\.(gz|xz|bz2|zst))$/i;
const HIST_MAX = 240;       // 500 ms tarama -> 2 dakikalik grafik penceresi
const HIST_SEC = 120;       // HIST_MAX * 0.5 sn

let T = {};                 // aktif ceviri sozlugu
let LANG = localStorage.getItem("ferdimLang") || "";
let jobs = [], cfg = {}, filter = "all", lastStats = {};
let histTimer = 0;          // gecmis aramasi icin bekleme
let states = new Map();     // is kimligi -> onceki durum (duyuru icin)
let primed = false;         // ilk tarama yapildi mi (ilk tarama duyurulmaz)
let stateWarned = false;    // durum kurtarma uyarisi bir kez gosterilir
let polling = false;
const rows = new Map();     // is kimligi -> satir ogeleri
const histSpeed = [];       // toplam hiz ornekleri (ring)
const histConn = [];        // acik baglanti sayisi ornekleri (ring)
const jobHist = new Map();  // is kimligi -> hiz ornekleri; is kaybolunca silinir

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
  for (const r of hostRows.values()) r.root.remove();   // profil satirlari
  hostRows.clear();
  if (hostsDlg.open) loadHosts();
}

/* ------------------------------------------------------------------ oturum
 * Bilet adresin #s=... kismindadir. Hemen adres cubugundan (ve gecmisten)
 * silinir, sunucuda bir kez token'a cevrilir. Ayni sekmede yeniden yukleme
 * sessionStorage'daki token ile calisir; yeni sekme yeni bilet ister. */
async function claimSession() {
  if (TOKEN) return TOKEN;
  const m = /(?:^#|&)s=([^&]*)/.exec(location.hash || "");
  if (!m) return "";
  history.replaceState(null, "", location.pathname + location.search);
  const r = await fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nonce: decodeURIComponent(m[1]) }),
  }).then((x) => x.json()).catch(() => ({}));
  if (!r || !r.ok || !r.token) return "";
  sessionStorage.setItem("ferdimToken", r.token);
  return r.token;
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

/* ------------------------------------------------------------- grafikler */
/* Grafikler yalniz yeni tarama verisi geldiginde yeniden cizilir; araya
 * animasyon/gecis konmaz, boylece azaltilmis hareket tercihine uyar. */
const cssColor = (() => {
  const cache = {};
  return (name) => {
    if (!cache[name]) {
      cache[name] = getComputedStyle(document.documentElement)
        .getPropertyValue(name).trim() || "#0ea5e9";
    }
    return cache[name];
  };
})();

/* canvas'i aygit piksel oranina gore olceklendirir (HiDPI) */
function fitCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(cv.clientWidth || cv.width || 1));
  const h = Math.max(1, Math.round(cv.clientHeight || cv.height || 1));
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

/* values eski -> yeni; sabit HIST_MAX penceresi saga yaslanir. Donus: tepe */
function drawLine(cv, values, color, area) {
  const { ctx, w, h } = fitCanvas(cv);
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = cssColor("--line");
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, h - 0.5);
  ctx.lineTo(w, h - 0.5);
  ctx.stroke();
  const peak = values.length ? Math.max(...values) : 0;
  if (values.length < 2) return peak;
  const top = Math.max(1, peak);
  const step = w / Math.max(1, HIST_MAX - 1);
  const x0 = w - (values.length - 1) * step;
  const px = (i) => x0 + i * step;
  const py = (i) => h - 1 - (values[i] / top) * (h - 3);
  ctx.beginPath();
  ctx.moveTo(px(0), py(0));
  for (let i = 1; i < values.length; i++) ctx.lineTo(px(i), py(i));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();
  if (area) {
    ctx.lineTo(px(values.length - 1), h - 1);
    ctx.lineTo(px(0), h - 1);
    ctx.closePath();
    ctx.globalAlpha = 0.18;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
  }
  return peak;
}

function pushHist(arr, v) {
  arr.push(v);
  if (arr.length > HIST_MAX) arr.shift();
}

/* her basarili taramada ornek alir; kaybolan islerin gecmisi silinir */
function sampleHistory() {
  let conns = 0;
  const live = new Set();
  for (const j of jobs) {
    live.add(j.id);
    conns += j.segments || 0;
    const h = jobHist.get(j.id) || [];
    pushHist(h, j.speed || 0);
    jobHist.set(j.id, h);
  }
  for (const id of jobHist.keys()) if (!live.has(id)) jobHist.delete(id);
  pushHist(histSpeed, Number(lastStats.total_speed) || 0);
  pushHist(histConn, conns);
}

function updateGraphs() {
  const curSpeed = histSpeed.length ? histSpeed[histSpeed.length - 1] : 0;
  const curConn = histConn.length ? histConn[histConn.length - 1] : 0;
  const peakSpeed = drawLine($("#speedGraph"), histSpeed, cssColor("--acc"), true);
  const peakConn = drawLine($("#connGraph"), histConn, cssColor("--ok"), true);
  $("#graphSpeedNow").textContent = rate(curSpeed);
  $("#graphConnNow").textContent = String(curConn);
  $("#speedGraph").setAttribute("aria-label", trf("graph_speed_a11y",
    { cur: rate(curSpeed), peak: rate(peakSpeed), sec: HIST_SEC }));
  $("#connGraph").setAttribute("aria-label", trf("graph_conn_a11y",
    { cur: curConn, peak: peakConn, sec: HIST_SEC }));
}

async function api(path, body) {
  const headers = {};
  if (TOKEN) headers["X-FerDim-Token"] = TOKEN;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const opt = { headers };
  if (body !== undefined) { opt.method = "POST"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401) {
    // token dondurulmus/gecersiz: sekmedeki kopyayi at, sayfa yeniden yuklenir
    // ve "oturum baglantisi" iletisini gosterir (sonsuz dongu yok)
    sessionStorage.removeItem("ferdimToken");
    location.reload();
    return {};
  }
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
/* motor hatasi: kararli kod varsa cevirisi, yoksa ham (Turkce) metin */
function errText(code, fallback) {
  const k = code ? "err_" + code : "";
  return k && T[k] !== undefined ? T[k] : (fallback || "");
}

/* dogrulama rozetleri: yan dosya saglamasi ve Merkle bildirimi (ok / warn /
 * mismatch). Sunucu notu (checksum_note / merkle_note) varsa ipucu olarak
 * tasinir; cozunurluk yoksa cevrilmis genel metin gosterilir. */
function integrityNotes(j) {
  const out = [];
  const algos = (j.verified || []).filter((a) => a !== "merkle").join(", ");
  const note = j.checksum_note || (j.extra && j.extra.checksum_note) || "";
  const mnote = j.merkle_note || (j.extra && j.extra.merkle_note) || "";
  if (j.checksum === "ok")
    out.push(["ok chk", "✓ " + (algos ? trf("chk_ok", { a: algos })
                                      : tr("notify_chk_ok")), note || undefined]);
  else if (j.checksum === "warn")
    out.push(["warn chk", "⚠ " + tr("chk_warn"),
              note || tr("err_checksum_mismatch")]);
  else if (j.checksum === "mismatch")
    out.push(["bad chk", "✗ " + tr("notify_chk_mismatch"),
              note || tr("err_checksum_mismatch")]);
  if (j.merkle === "ok" || j.merkle === "repaired")
    out.push(["ok chk", "✓ " + tr("merkle_ok"), mnote || undefined]);
  else if (j.merkle === "warn" || j.merkle === "mismatch")
    out.push([(j.merkle === "warn" ? "warn" : "bad") + " chk",
              (j.merkle === "warn" ? "⚠ " : "✗ ") + tr("merkle_manifest"),
              mnote || tr("err_merkle_mismatch")]);
  return out;
}

function notesFor(j) {
  const out = [];
  if (j.adaptive && j.adaptive < j.segments_cfg)
    out.push(["info", "⚙ " + trf("adaptive_limit", j.adaptive)]);
  if (j.state === "error" && (j.error || j.error_code))
    out.push(["bad", "⚠ " + errText(j.error_code, j.error)]);
  if (j.sched_wait && j.window)
    out.push(["info", "⏱ " + trf("sched_wait", { w: j.window })]);
  if (j.sources_dead > 0)
    out.push(["warn", "⛔ " + trf("mirrors_dead", { n: j.sources_dead })]);
  if (j.warning === "soft_404")
    out.push(["warn", "⚠ " + tr("warn_soft_404")]);
  out.push(...integrityNotes(j));
  return out;
}

function buildRow(j) {
  const btn = {};
  for (const [kind, key, glyph] of ACTIONS) {
    btn[kind] = dom("button", { class: "act", type: "button", "data-act": kind,
                                "aria-label": tr(key), title: tr(key), text: glyph });
    btn[kind].onclick = () => act(kind, j.id);
  }
  const speedVal = dom("span");
  const spark = dom("canvas", { class: "spark", role: "img", width: "64",
                               height: "16", "aria-label": tr("spark_a11y") });
  const r = {
    id: j.id, sig: "", btn,
    cat: dom("span", { class: "cat" }),
    name: dom("span", { class: "fname" }),
    notes: dom("span", { class: "notes", hidden: true }),
    url: dom("div", { class: "url" }),
    fill: dom("i"),
    plabel: dom("span", { class: "plabel" }),
    size: dom("div", { class: "c-size", role: "cell" }),
    speedVal, spark,
    speed: dom("div", { class: "c-speed", role: "cell" }, speedVal, spark),
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
    r.notes.replaceChildren(...notes.map(([cls, text, title]) =>
      dom("span", { class: "note " + cls, text, title })));
    r.notes.hidden = notes.length === 0;
  }

  const label = tr(j.verifying ? "st_verifying" : "st_" + j.state);
  const pct = j.size > 0 ? Math.min(100, Math.floor(j.done * 100 / j.size)) : null;
  if (j.verifying && j.verify_total > 0) {
    // dogrulama ilerlemesi: aktarim bitti, tek okuma suzuluyor
    const vpct = Math.min(100, Math.floor(j.verify_done * 100 / j.verify_total));
    r.bar.classList.remove("unk");
    r.fill.style.width = vpct + "%";
    r.bar.setAttribute("aria-valuenow", String(vpct));
    r.plabel.textContent = vpct + "% — " + label;
  } else if (pct == null) {     // boyut bilinmiyor: belirsiz cubuk
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
  r.speedVal.textContent = playing ? rate(j.speed) : "";
  r.eta.textContent = playing ? eta(j.eta) : "";
  r.spark.hidden = !playing;    // duraklatilan/biten iste bos mini grafik kalmasin
  if (playing) {
    drawLine(r.spark, jobHist.get(j.id) || [], cssColor("--acc"), false);
    r.spark.setAttribute("aria-label",
      trf("spark_a11y", { cur: rate(j.speed) }));
  }
  r.btn.resume.hidden =
    !["paused", "error", "queued", "canceled"].includes(j.state);
  r.btn.pause.hidden = !playing;
  r.btn.zip.hidden =
    !(j.state === "done" && ARCH_RE.test(j.filename || ""));
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

  // birlikte yasam: yalniz acikken gorunur; hat mesgulse tavan uygulanir
  const co = lastStats.coexist || {};
  const cs = $("#coexistState");
  cs.textContent = co.enabled
    ? "⇄ " + tr(co.active ? "coexist_active" : "coexist_on") : "";
  cs.className = "conn " + (co.active ? "ok" : "dim");
  cs.title = tr("coexist_hint");

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
  let finished = false;
  if (primed) {
    for (const j of jobs) {
      const old = states.get(j.id);
      if (old && old !== j.state && FINAL.has(j.state)) {
        say(trf("done_announce", { name: j.filename, state: tr("st_" + j.state) }));
        finished = true;
      }
    }
  }
  states = new Map(jobs.map((j) => [j.id, j.state]));
  primed = true;
  if (finished) { loadHistory(); loadHistStats(); }
}

async function poll() {
  if (polling) return;          // yanimda surmekte olan istek varsa atla
  polling = true;
  let fresh = false;            // yalniz basarili tarama grafige ornek ekler
  const conn = $("#connState");
  try {
    const s = await api("/api/stats");
    if (s && s.ok) {
      jobs = s.jobs || []; lastStats = s; fresh = true;
      conn.textContent = "● " + tr("daemon_ok"); conn.className = "conn ok";
      // bozuk durum dosyasindan kurtarma: bir kez, kalici bir bildirim
      if (s.warning === "state_recovered" && !stateWarned) {
        stateWarned = true;
        notice(tr("warn_state"));
      }
    } else {
      conn.textContent = "● " + tr("daemon_down"); conn.className = "conn bad";
    }
  } catch {
    conn.textContent = "● " + tr("daemon_down"); conn.className = "conn bad";
  }
  polling = false;
  if (fresh) sampleHistory();
  render(); renderStatus(); announce(); updateGraphs();
  // profil paneli acikken ~2 sn'de bir tazelenir (her taramada degil)
  if (hostsDlg.open && ++hostTicks % 4 === 0) loadHosts();
}

/* --------------------------------------------------------------- arsiv */
/* Panel: ic ice listeleme, secmeli cikarma ve onarim. Ilerleme
 * archive-status yoklamasiyla gelir; tum metin textContent ile yazilir. */
let zipJob = "";
let zipTask = 0;

function zipStatus(text) {
  const n = $("#zipStatus");
  n.textContent = text;
  n.hidden = !text;
}

function zipPoll() {
  if (!zipTask) return;
  const task = zipTask;
  api("/api/archive-status", { task }).then((s) => {
    if (!zipTask || zipTask !== task) return;
    if (!s.ok) {
      zipTask = 0;
      zipStatus(trf("zip_failed", { e: s.error || "" }));
      return;
    }
    if (s.state === "running") {
      zipStatus(trf("zip_running", { done: s.done || 0, total: s.total || 0 }));
      setTimeout(zipPoll, 400);
      return;
    }
    zipTask = 0;
    const res = s.result || {};
    const errs = res.errors || [];
    if (s.state === "done" && res.ok) {
      const key = res.repair ? "zip_repaired" : "zip_extracted";
      let text = trf(key, { n: res.extracted || 0, dest: res.dest || "" });
      if (errs.length)
        text += " — " + trf("zip_errors", { n: errs.length, e: errs[0] });
      zipStatus(text);
    } else {
      zipStatus(trf("zip_failed", { e: s.error || (errs[0] || "") }));
    }
  }).catch(() => { zipTask = 0; });
}

async function zipExtract() {
  const members = Array.from(
    document.querySelectorAll("#zipList input.zsel:checked")).map((n) => n.value);
  if (!members.length) { zipStatus(tr("zip_sel_none")); return; }
  const r = await api("/api/archive-extract", { id: zipJob, members });
  if (!r.ok) { zipStatus(trf("zip_failed", { e: r.error || "" })); return; }
  zipTask = r.task;
  zipPoll();
}

async function zipRepair() {
  const r = await api("/api/archive-repair", { id: zipJob });
  if (!r.ok) {
    zipStatus(r.code === "no_manifest" ? tr("zip_nomf")
      : trf("zip_failed", { e: r.error || "" }));
    return;
  }
  zipTask = r.task;
  zipPoll();
}

/* ----------------------------------------------------------------- eylemler */
async function act(kind, id) {
  if (kind === "remove") {
    if (!confirm(tr("confirm_remove"))) return;
    await api("/api/remove", { id, delete_files: true });
  } else if (kind === "zip") {
    const r = await api("/api/archive-list", { id, nested: true });
    if (!r.ok) { alert(tr("zip_title") + ": " + (r.error || "")); return; }
    zipJob = id; zipTask = 0;
    zipStatus("");
    const list = $("#zipList");
    list.replaceChildren();
    const files = (r.entries || []).filter((e) => e.kind === "file");
    $("#zipTitle").textContent =
      `${tr("zip_title")} — ${r.filename} (${files.length}${r.note || ""})`;
    for (const e of files.slice(0, 400)) {
      const box = dom("input", { type: "checkbox", class: "zsel",
        value: e.key, "aria-label": tr("zip_select") + ": " + e.path });
      list.append(dom("div", {
        class: "ziprow" + (e.depth > 1 ? " nested" : ""), role: "listitem" },
        box,
        dom("span", { class: "n", text: e.path }),
        dom("span", { class: "s", text: human(e.size) })));
    }
    if (!files.length)
      list.append(dom("div", { class: "dim", text: tr("zip_empty") }));
    $("#zipDlg").showModal();
    return;
  } else {
    await api("/api/" + kind, { id });
  }
  poll();
}
$("#zipClose").onclick = () => { zipTask = 0; $("#zipDlg").close(); };
$("#zipExtract").onclick = zipExtract;
$("#zipRepair").onclick = zipRepair;

/* ------------------------------------------------- ana makine profilleri */
/* Ogrenilmis profiller ('hosts' komutu: liste / {forget} / {clear}). Satirlar
 * ana makineye gore anahtarlanir ve yerinde guncellenir; her metin
 * textContent ile yazilir. Panel yalniz acikken tazelenir (~2 sn). */
const hostsDlg = $("#hostsDlg");
const hostRows = new Map();      // ana makine -> {root, text, forget, sig}
let hostsGen = 0;                // eski yanit yeni listeyi ezmesin
let hostTicks = 0;

function buildHostRow(host) {
  const text = dom("span", { class: "hrow-text" });
  const forget = dom("button", { class: "act", type: "button",
                                 "data-host": host, text: "✕" });
  forget.onclick = () => forgetHost(forget.dataset.host || "");
  return { text, forget, sig: "",
           root: dom("div", { class: "hostrow", role: "listitem" },
                     text, forget) };
}

function updateHostRow(r, h) {
  const band = h.rtt_band ? tr("rtt_" + h.rtt_band) : "?";
  const seen = h.last_seen
    ? new Date(h.last_seen * 1000).toLocaleString() : "—";
  const text = trf("hosts_row", {
    host: h.host, links: h.links || 0, rtt: band,
    speed: human(h.per_link_bps || 0), samples: h.samples || 0, seen });
  if (text !== r.sig) {          // satir yalniz degisince yeniden yazilir
    r.sig = text;
    r.text.textContent = text;
    r.text.title = text;
  }
  const label = tr("act_remove") + ": " + h.host;
  if (r.forget.getAttribute("aria-label") !== label) {
    r.forget.setAttribute("aria-label", label);
    r.forget.title = label;
  }
  r.forget.dataset.host = h.host;
}

function renderHosts(list) {
  const box = $("#hostsList");
  const keep = new Set();
  let cursor = box.firstChild;
  for (const h of list) {
    keep.add(h.host);
    let r = hostRows.get(h.host);
    if (!r) { r = buildHostRow(h.host); hostRows.set(h.host, r); }
    updateHostRow(r, h);
    if (r.root === cursor) {
      cursor = cursor.nextSibling;
    } else {
      box.insertBefore(r.root, cursor);
      if (cursor) cursor = cursor.nextSibling;
    }
  }
  for (const [host, r] of hostRows) {
    if (!keep.has(host)) { r.root.remove(); hostRows.delete(host); }
  }
  $("#hostsEmpty").hidden = list.length > 0;
  $("#hostsForgetAll").hidden = list.length === 0;
}

async function loadHosts() {
  const gen = ++hostsGen;
  let r = {};
  try { r = await api("/api/hosts", {}); } catch { r = {}; }
  if (gen !== hostsGen) return;          // daha yeni istek yolda
  const st = $("#hostsStatus");
  if (!r || !r.ok) {                     // komut yok / daemon hatasi
    $("#hostsList").replaceChildren();
    hostRows.clear();
    $("#hostsEmpty").hidden = true;
    $("#hostsForgetAll").hidden = true;
    st.textContent = tr("cli_error") + ": " + ((r && r.error) || tr("daemon_lost"));
    st.hidden = false;
    return;
  }
  st.textContent = "";
  st.hidden = true;
  renderHosts(r.hosts || []);
}

async function forgetHost(host) {
  let r = {};
  try { r = await api("/api/hosts", { forget: host }); } catch { r = {}; }
  notice(r && r.ok ? trf("hosts_forgotten", { host })
                   : trf("hosts_unknown", { host }));
  loadHosts();
}

$("#hostsBtn").onclick = () => { hostsDlg.showModal(); loadHosts(); };
$("#hostsClose").onclick = () => hostsDlg.close();
$("#hostsForgetAll").onclick = async () => {
  const n = hostRows.size;
  if (!n) return;
  // onay metni dugmenin kendi etiketinden turetilir (yeni anahtar gerekmez)
  if (!confirm($("#hostsForgetAll").textContent.trim() + "? (" + n + ")")) return;
  let r = {};
  try { r = await api("/api/hosts", { clear: true }); } catch { r = {}; }
  notice(r && r.ok ? trf("hosts_cleared", { n: r.removed || 0 })
                   : tr("daemon_lost"));
  loadHosts();
};

/* --------------------------------------------------------------- gecmis */
/* Sunucudaki SQLite gecmisi: arama, istatistik ve yeniden indirme. Satirlar
 * sunucudan gelen metinle kurulur; hepsi textContent ile yazilir. */
function historyRow(h) {
  const again = dom("button", {
    class: "act", type: "button",
    "aria-label": tr("hist_redownload") + ": " + (h.filename || h.url),
    title: tr("hist_redownload"), text: "↻",
  });
  again.onclick = () => redownload(h.id);
  const when = h.finished_at
    ? new Date(h.finished_at * 1000).toLocaleString() : "";
  return dom("li", { class: "histrow" },
    dom("div", { class: "hname" },
      dom("span", { class: "fname", text: h.filename || h.url }), again),
    dom("div", { class: "url", text: h.url }),
    dom("div", { class: "dim", text: trf("hist_meta", {
      state: tr("st_" + h.state), size: human(h.bytes), date: when,
      host: h.host || "—" }) }),
    dom("div", { class: "dim", text: trf("hist_links", {
      avg: Number(h.avg_links || 0).toFixed(1), peak: h.peak_links || 0 }) }));
}

async function loadHistory() {
  let r = {};
  try {
    r = await api("/api/history", {
      q: $("#histSearch").value.trim(), state: $("#histState").value,
      limit: 100 });
  } catch { r = {}; }
  const list = $("#histList");
  const empty = $("#histEmpty");
  if (!r || !r.ok) {
    list.replaceChildren();
    empty.hidden = true;
    $("#histStats").textContent = tr("hist_failed");
    return;
  }
  list.replaceChildren(...r.items.map(historyRow));
  empty.textContent = tr($("#histSearch").value.trim() || $("#histState").value
    ? "hist_none" : "hist_empty");
  empty.hidden = r.items.length > 0;
}

async function loadHistStats() {
  let r = {};
  try { r = await api("/api/history-stats", {}); } catch { return; }
  if (!r || !r.ok) return;
  const out = [trf("hist_stats",
    { n: r.count || 0, bytes: human(r.bytes || 0) })];
  for (const h of (r.hosts || []).slice(0, 3))
    out.push(trf("hist_host_line", {
      host: h.host, n: h.count || 0,
      links: Number(h.avg_links || 0).toFixed(1),
      speed: rate(h.avg_speed || 0) }));
  $("#histStats").textContent = out.join("  ·  ");
}

async function redownload(id) {
  let r = {};
  try { r = await api("/api/history-redownload", { id }); } catch { r = {}; }
  if (!r || !r.ok) { notice(tr("add_failed")); return; }
  notice(trf("hist_redownloaded", { name: r.filename || "" }));
  poll();
}

$("#histSearch").addEventListener("input", () => {
  clearTimeout(histTimer);
  histTimer = setTimeout(loadHistory, 300);
});
$("#histState").onchange = loadHistory;
$("#histClear").onclick = async () => {
  if (!confirm(tr("hist_confirm_clear"))) return;
  let r = {};
  try { r = await api("/api/history-clear", {}); } catch { r = {}; }
  if (r && r.ok) notice(trf("hist_cleared", { n: r.removed || 0 }));
  loadHistory(); loadHistStats();
};

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
  if (!/^(?:https?|ftps?):\/\/\S+$/i.test(url)) { fail(tr("err_url")); $("#addUrl").focus(); return; }
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
/* ---- saat bazli hiz plani (1.8): okunabilir satirlar + JSON duzenleyici ---- */
function planRuleText(rule) {
  const days = rule.days ? String(rule.days) : tr("plan_every_day");
  const hours = (rule.from || rule.to)
    ? (rule.from || "00:00") + "–" + (rule.to || "24:00") : tr("plan_all_day");
  const limit = rule.limit && String(rule.limit) !== "0"
    ? String(rule.limit) : tr("plan_unlimited");
  return trf("plan_rule", { days, hours, limit });
}
function buildPlanRows(plan) {
  const box = $("#planRows"), state = $("#planState");
  box.replaceChildren();
  const sp = lastStats.speed_plan || {};
  const active = sp.active_rule ? sp.active_rule.index : -1;
  (plan || []).forEach((rule, i) => {
    box.append(dom("div", {
      class: i === active ? "planrow active" : "planrow",
      "data-rule": String(i),
      text: (i === active ? "● " : "○ ") + planRuleText(rule),
    }));
  });
  if (!sp.enabled) state.textContent = tr("plan_state_none");
  else if (sp.manual) state.textContent = tr("plan_state_manual");
  else if (active >= 0)
    state.textContent = trf("plan_state_active",
      { limit: sp.limit ? rate(sp.limit) : tr("plan_unlimited") });
  else state.textContent = tr("plan_state_off");
  $("#setPlan").value = plan && plan.length
    ? JSON.stringify(plan, null, 2) : "";
  $("#planError").hidden = true;
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
  buildPlanRows(cfg.speed_plan);
  $("#setWatchDirs").value = (cfg.watch_dirs || []).join("\n");
  $("#setWatchDone").value = cfg.watch_done || "delete";
  $("#setSidecar").checked = !!cfg.checksum_sidecar;
  $("#setCoexist").checked = !!cfg.coexist;
  $("#setRetries").value = cfg.retries ?? 5;
  $("#setTimeout").value = cfg.timeout ?? 30;
  $("#setStall").value = cfg.stall_timeout ?? 20;
  $("#setHistDays").value = cfg.history_days ?? 365;
  setDlg.showModal();
};
$("#setClose").onclick = () => setDlg.close();
$("#setSave").onclick = async () => {
  const num = (id, lo, hi, fallback) => {
    const v = parseInt($(id).value, 10);
    return Number.isNaN(v) ? fallback : Math.min(hi, Math.max(lo, v));
  };
  // plan JSON'u kaydetmeden once dogrulanir: sunucu bozuk kurali atardi
  const planRaw = $("#setPlan").value.trim();
  let plan = [];
  if (planRaw) {
    try {
      plan = JSON.parse(planRaw);
      if (!Array.isArray(plan) ||
          plan.some((x) => !x || typeof x !== "object")) throw new Error("plan");
    } catch (e) {
      const p = $("#planError");
      p.textContent = tr("plan_bad");
      p.hidden = false;
      $("#setPlan").focus();
      return;
    }
  }
  $("#planError").hidden = true;
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
    coexist: $("#setCoexist").checked,
    retries: num("#setRetries", 0, 20, cfg.retries ?? 5),
    timeout: num("#setTimeout", 5, 300, cfg.timeout ?? 30),
    stall_timeout: num("#setStall", 5, 120, cfg.stall_timeout ?? 20),
    history_days: num("#setHistDays", 0, 3650, cfg.history_days ?? 365),
    speed_plan: plan,
  } });
  if (r && r.ok === false) {
    // sunucu reddetti (orn. HTTP'den degistirilemeyen bir dizin ayari):
    // dialog acik kalsin ki kullanici degeri duzeltebilsin
    notice(errText(r.code, r.error));
    return;
  }
  if (r && r.config) cfg = r.config;
  notice(r && r.warning === "download_dir" ? tr("warn_dir") : tr("saved"));
  localStorage.setItem("ferdimLang", lang === "auto" ? "" : lang);
  setDlg.close();
  await loadLang(lang);
  loadHistory(); loadHistStats();
  poll();
};

/* ---- baslangic ---- */
(async () => {
  TOKEN = await claimSession();
  await loadLang();
  if (!TOKEN) {
    // elde token yok: API'ye hic gidilmez, yalniz "nasil acilir" iletisi
    const n = $("#noSession");
    n.textContent = tr("web_no_session");
    n.hidden = false;
    $("#connState").textContent = "● " + tr("daemon_lost");
    $("#connState").className = "conn bad";
    return;
  }
  const r = await api("/api/config").catch(() => ({}));
  if (r && r.config) cfg = r.config;
  await poll();
  await loadHistory();
  await loadHistStats();
  setInterval(poll, 500);
})();
