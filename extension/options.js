"use strict";
const DEFAULTS = {
  takeover: "all", minTakeoverMB: 0, minMediaMB: 1, includeAudio: true,
  badge: true, overlayButton: true, sendCookies: true, sendReferer: true,
  segments: 32,
};
const $ = (s) => document.querySelector(s);

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const v = browser.i18n.getMessage(el.dataset.i18n);
    if (v) el.textContent = v;
  });
}

async function load() {
  applyI18n();
  const st = await browser.storage.local.get("opts");
  const o = { ...DEFAULTS, ...(st.opts || {}) };
  $("#takeover").value = o.takeover;
  $("#minTakeoverMB").value = o.minTakeoverMB;
  $("#minMediaMB").value = o.minMediaMB;
  $("#includeAudio").checked = o.includeAudio;
  $("#badge").checked = o.badge;
  $("#overlayButton").checked = o.overlayButton;
  $("#segments").value = o.segments;
  $("#sendCookies").checked = o.sendCookies;
  $("#sendReferer").checked = o.sendReferer;
}

$("#save").onclick = async () => {
  const o = {
    takeover: $("#takeover").value,
    minTakeoverMB: parseFloat($("#minTakeoverMB").value) || 0,
    minMediaMB: parseFloat($("#minMediaMB").value) || 0,
    includeAudio: $("#includeAudio").checked,
    badge: $("#badge").checked,
    overlayButton: $("#overlayButton").checked,
    segments: Math.max(1, Math.min(32, parseInt($("#segments").value) || 8)),
    sendCookies: $("#sendCookies").checked,
    sendReferer: $("#sendReferer").checked,
  };
  await browser.storage.local.set({ opts: o });
  const st = $("#status");
  st.textContent = browser.i18n.getMessage("saved") || "✓";
  setTimeout(() => (st.textContent = ""), 1500);
};

load();
