"use strict";
const $ = (s) => document.querySelector(s);
let tabId = -1;

const _ = (k, subs) => browser.i18n.getMessage(k, subs) || k;

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const v = browser.i18n.getMessage(el.dataset.i18n);
    if (v) el.textContent = v;
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    const v = browser.i18n.getMessage(el.dataset.i18nPh);
    if (v) el.placeholder = v;
  });
}

function humanSize(n) {
  if (!n || n < 0) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
const send = (m) => browser.runtime.sendMessage(m);

async function init() {
  applyI18n();
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  tabId = tabs[0] ? tabs[0].id : -1;
  const r = await send({ getMedia: true, tabId });
  renderHost(r);
  renderMedia(r.media || []);
  $("#ver").textContent = "v" + browser.runtime.getManifest().version;
}

function renderHost(r) {
  const el = $("#hostState");
  el.textContent = r && r.hostOk ? "● " + _("hostOk") : "○ " + _("hostOff");
  el.className = "state " + (r && r.hostOk ? "ok" : "bad");
  $("#webUi").href = "http://127.0.0.1:" + ((r && r.port) || 8378);
  renderTurbo(!!(r && r.turbo));
}

function renderTurbo(on) {
  const b = $("#turboBtn");
  b.classList.toggle("active", on);
  b.textContent = on ? "⚡ " + _("turboOn") : "⚡ " + _("turbo");
  b.title = _("turboHint");
}

$("#turboBtn").onclick = async () => {
  const r = await send({ getTurbo: true });
  const r2 = await send({ turbo: !r.turbo });
  renderTurbo(!!(r2 && r2.turbo));
};

function renderMedia(items) {
  const list = $("#mediaList");
  list.innerHTML = "";
  $("#mediaEmpty").hidden = items.length > 0;
  $("#mediaTitle").textContent = `${_("popupTitle")} (${items.length})`;
  for (const it of items) {
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `
      <span class="ico"></span>
      <div class="meta">
        <div class="n"></div>
        <div class="s"></div>
      </div>
      <button class="copy">⧉</button>
      <button class="primary"></button>`;
    el.querySelector(".ico").textContent = it.kind === "audio" ? "🎵" : "🎬";
    const n = el.querySelector(".n");
    n.textContent = it.filename || it.url;
    n.title = it.url;
    el.querySelector(".s").textContent =
      `${humanSize(it.size) || "?"} · ${it.mime.split(";")[0]}`;
    el.querySelector(".copy").title = _("btnCopy");
    el.querySelector(".primary").textContent = _("btnDownload");
    el.querySelector(".primary").onclick = () => {
      send({ download: true, url: it.url, filename: it.filename });
      el.querySelector(".primary").textContent = "✓";
    };
    el.querySelector(".copy").onclick = () => {
      navigator.clipboard.writeText(it.url);
      el.querySelector(".copy").textContent = "✓";
    };
    list.appendChild(el);
  }
}

$("#optBtn").onclick = () => browser.runtime.openOptionsPage();
$("#urlAdd").onclick = () => {
  const u = $("#urlInput").value.trim();
  if (!/^https?:\/\//i.test(u)) return;
  send({ download: true, url: u });
  $("#urlInput").value = "";
  window.close();
};
$("#urlInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#urlAdd").click();
});

/* ---- sayfa baglanti tarama ---- */
let grabLinks = [];
$("#grabBtn").onclick = async () => {
  $("#grabBtn").textContent = _("scanning");
  const r = await send({ grab: true, tabId });
  grabLinks = r.links || [];
  $("#grabBtn").hidden = true;
  $("#grabBox").hidden = false;
  renderGrab();
};
function matchPreset(u, preset) {
  if (!preset) return true;
  if (preset === "media") return /\.(mp4|mkv|webm|flv|avi|mov|m4v|mp3|m4a|flac|ogg|opus|ts)(\?|$)/i.test(u);
  if (preset === "archive") return /\.(zip|rar|7z|tar|gz|bz2|xz|iso)(\?|$)/i.test(u);
  if (preset === "doc") return /\.(pdf|doc|docx|xls|xlsx|ppt|pptx|odt|epub)(\?|$)/i.test(u);
  return true;
}
function renderGrab() {
  const filt = $("#grabFilter").value.toLowerCase();
  const preset = $("#grabPreset").value;
  const vis = grabLinks.filter((l) =>
    (!filt || l.url.toLowerCase().includes(filt) ||
     (l.text || "").toLowerCase().includes(filt)) &&
    matchPreset(l.url, preset));
  $("#grabCount").textContent =
      browser.i18n.getMessage("linksCount",
                              [String(vis.length), String(grabLinks.length)]);
  const list = $("#grabList");
  list.innerHTML = "";
  for (const l of vis.slice(0, 200)) {
    const el = document.createElement("label");
    el.className = "item";
    const name = decodeURIComponent(l.url.split("/").pop() || l.url);
    el.innerHTML = `
      <input type="checkbox" checked>
      <div class="meta">
        <div class="n"></div>
        <div class="s dim"></div>
      </div>`;
    const n = el.querySelector(".n");
    n.textContent =
      l.text && l.text !== name ? l.text + " → " + name : name;
    n.title = l.url;
    el.querySelector(".s").textContent = l.url;
    el.dataset.url = l.url;
    el.dataset.name = name;
    list.appendChild(el);
  }
}
$("#grabFilter").addEventListener("input", renderGrab);
$("#grabPreset").addEventListener("change", renderGrab);
$("#grabAll").addEventListener("change", (e) => {
  document.querySelectorAll("#grabList input[type=checkbox]")
    .forEach((c) => (c.checked = e.target.checked));
});
$("#grabDl").onclick = () => {
  const items = [...document.querySelectorAll("#grabList .item")]
    .filter((el) => el.querySelector("input").checked)
    .map((el) => ({ url: el.dataset.url, filename: el.dataset.name }));
  if (!items.length) return;
  send({ downloadBatch: true, items });
  $("#grabDl").textContent = _("sentCount", [String(items.length)]);
};

init();
