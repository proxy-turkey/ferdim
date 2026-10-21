/* FerDim Entegrasyon Modülü - arka plan betigi
 * Davranis modeli:
 *  - tarayici indirmesini yakala -> iptal et -> FerDim'e aktar
 *  - video/ses medyalarini webRequest ile dinle, sayfa basliginda rozet
 *  - sag tik menusu, "tum baglantilari tara" (grab) kutuphanesi
 */
"use strict";

const NATIVE = "ferdim";
const _ = (k, subs) => {
  const m = browser.i18n.getMessage(k, subs);
  return m || k;
};

const DEFAULT_OPTS = {
  takeover: "all",        // all | media | off
  minTakeoverMB: 0,       // 0 = hepsi
  minMediaMB: 1,
  includeAudio: true,
  overlayButton: true,
  badge: true,
  sendCookies: true,
  sendReferer: true,
  segments: 32,           // azami guc varsayilan
};

let opts = { ...DEFAULT_OPTS };
let hostOk = false;
let hostPort = 8378;
let hostTurbo = false;

/* mediaByTab: tabId -> Map(url -> {url,size,mime,filename,ext}) */
const mediaByTab = new Map();
const grabsByTab = new Map(); // tabId -> [link]

/* ---------------------------------------------------------------- options */
async function loadOpts() {
  const st = await browser.storage.local.get("opts");
  opts = { ...DEFAULT_OPTS, ...(st.opts || {}) };
}
browser.storage.onChanged.addListener((ch, area) => {
  if (area === "local" && ch.opts) loadOpts().then(refreshMenus);
});

/* ------------------------------------------------------------ native host */
function sendNative(msg) {
  return browser.runtime.sendNativeMessage(NATIVE, msg);
}

async function pingHost() {
  try {
    const r = await sendNative({ cmd: "ping" });
    hostOk = !!(r && r.ok);
    if (r && r.port) hostPort = r.port;
    if (r && typeof r.turbo === "boolean") hostTurbo = r.turbo;
  } catch (e) {
    hostOk = false;
  }
  browser.browserAction.setBadgeBackgroundColor(
    { color: hostOk ? "#22c55e" : "#ef4444" });
  return hostOk;
}

/* ------------------------------------------------------------------ utils */
function headerValue(headers, name) {
  const h = (headers || []).find((x) =>
    (x.name || "").toLowerCase() === name);
  return h ? h.value : "";
}
function nameFromUrl(u) {
  try {
    const p = new URL(u).pathname.split("/").filter(Boolean).pop() || "";
    return decodeURIComponent(p);
  } catch { return ""; }
}
function looksMedia(u, mime) {
  const ul = (u || "").toLowerCase().split("?")[0];
  if (/\.(mp4|mkv|webm|flv|avi|mov|m4v|ts|3gp|ogv|m3u8|mpd)$/.test(ul)) return true;
  if (/\.(mp3|m4a|flac|ogg|opus|wav|aac)$/.test(ul)) return "audio";
  const m = (mime || "").toLowerCase();
  if (m.startsWith("video/") || m.includes("mpegurl")) return true;
  if (m.startsWith("audio/")) return "audio";
  return false;
}
function humanSize(n) {
  if (!n || n < 0) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}

/* ------------------------------------------------------- indirme yakalama */
async function onDownloadCreated(item) {
  if (item.state !== "in_progress") return;
  const url = item.url || "";
  if (!/^(https?|ftp):/i.test(url)) return; // blob:/data: yakalanamaz
  if (url.startsWith("http://127.0.0.1:" + hostPort)) return;

  if (opts.takeover === "off") return;
  const mime = item.mime || "";
  const media = looksMedia(url, mime);
  if (opts.takeover === "media" && !media) return;
  if (opts.takeover === "media" && media === "audio" && !opts.includeAudio) return;
  const minBytes = (opts.minTakeoverMB || 0) * 1e6;
  const known = item.totalBytesSuggested || item.totalBytes || 0;
  if (minBytes && known && known < minBytes) return;

  // tarayici indirmesini iptal et, listeden sil
  try {
    await browser.downloads.cancel(item.id);
    await browser.downloads.erase({ id: item.id });
  } catch (e) { /* zaten bitti */ }

  let referer = item.referrer || "";
  if (!referer) {
    const tabs = await browser.tabs.query({ active: true, currentWindow: true });
    referer = tabs[0] && tabs[0].url && /^https?:/.test(tabs[0].url) ? tabs[0].url : "";
  }
  await handoff(url, {
    filename: item.filename ? item.filename.split(/[\\/]/).pop() : nameFromUrl(url),
    referer: opts.sendReferer ? referer : "",
    mime,
  });
}

async function handoff(url, extra = {}) {
  const msg = {
    cmd: "download",
    url,
    filename: extra.filename || "",
    referer: extra.referer || "",
    ua: navigator.userAgent,
    segments: opts.segments,
  };
  if (opts.sendCookies && /^https?:/i.test(url)) {
    try {
      const cs = await browser.cookies.getAll({ url });
      if (cs.length) msg.cookies = cs.map((c) => c.name + "=" + c.value).join("; ");
    } catch (e) { /* cookies izni yok */ }
  }
  try {
    const r = await sendNative(msg);
    if (!r || !r.ok) notify(_("notifyStartFail"),
      (r && r.error) || "-");
  } catch (e) {
    notify(_("notifyServiceOff"), _("notifyServiceOffBody"));
  }
}

function notify(title, message) {
  try {
    browser.notifications.create({
      type: "basic",
      title,
      message,
      iconUrl: browser.runtime.getURL("icons/icon.svg"),
    });
  } catch (e) { /* notifications yok */ }
}

/* ------------------------------------------------------- medya dinleyicisi */
function onHeaders(details) {
  if (details.tabId < 0) return;
  const ct = (headerValue(details.responseHeaders, "content-type") || "").toLowerCase();
  const cd = headerValue(details.responseHeaders, "content-disposition");
  const size = parseInt(headerValue(details.responseHeaders, "content-length") || "0", 10);
  const url = details.url;
  const kind = looksMedia(url, ct);
  if (!kind) return;
  if (kind === "audio" && !opts.includeAudio) return;
  if (kind === true && size && size < (opts.minMediaMB || 0) * 1e6) return;
  // sayfa icerigi degil, indirme amacli oge olmali (ana cerceve harici)
  let filename = nameFromUrl(url);
  const m = /filename="?([^";]+)"?/i.exec(cd || "");
  if (m) filename = decodeURIComponent(m[1]);
  if (!filename) return;

  let map = mediaByTab.get(details.tabId);
  if (!map) { map = new Map(); mediaByTab.set(details.tabId, map); }
  if (!map.has(url)) {
    map.set(url, { url, size, mime: ct || (kind === "audio" ? "audio/*" : "video/*"),
                   filename, kind });
    if (map.size > 60) map.delete(map.keys().next().value);
    updateBadge(details.tabId);
    if (opts.overlayButton) {
      browser.tabs.sendMessage(details.tabId, {
        ferdimMedia: true, count: map.size,
      }).catch(() => {});
    }
  }
}

function updateBadge(tabId) {
  const map = mediaByTab.get(tabId);
  const n = map ? map.size : 0;
  if (opts.badge) {
    browser.browserAction.setBadgeText({ tabId, text: n ? String(n) : "" });
  }
  if (n) browser.pageAction.show(tabId);
  else browser.pageAction.hide(tabId).catch(() => {});
}

browser.tabs.onRemoved.addListener((tabId) => {
  mediaByTab.delete(tabId);
  grabsByTab.delete(tabId);
});

/* ------------------------------------------------------------ sag tik menu */
function menuDefs() {
  return [
    { id: "fd-link", title: _("menuDownload"), contexts: ["link"] },
    { id: "fd-media", title: _("menuDownload"), contexts: ["video", "audio", "image"] },
    { id: "fd-page", title: _("menuGrabAll"), contexts: ["page"] },
    { id: "fd-selection", title: _("menuGrabSel"), contexts: ["selection"] },
  ];
}
async function refreshMenus() {
  try { await browser.contextMenus.removeAll(); } catch (e) {}
  for (const d of menuDefs()) browser.contextMenus.create({ ...d });
}
browser.contextMenus.onClicked.addListener(async (info, tab) => {
  const referer = tab && tab.url || "";
  if (info.menuItemId === "fd-link") {
    await handoff(info.linkUrl, { referer, filename: nameFromUrl(info.linkUrl) });
  } else if (info.menuItemId === "fd-media") {
    const u = info.srcUrl || info.linkUrl;
    if (u) await handoff(u, { referer, filename: nameFromUrl(u) });
  } else if (info.menuItemId === "fd-page" || info.menuItemId === "fd-selection") {
    await grabTab(tab.id, info.menuItemId === "fd-selection");
    browser.pageAction.open(tab.id).catch(() => {});
  }
});

/* --------------------------------------------------- sayfa baglanti tarama */
async function grabTab(tabId, selectionOnly) {
  const results = await browser.tabs.executeScript(tabId, {
    code: `(() => {
      const roots = [];
      const s = document.getSelection();
      if (s && !s.isCollapsed && s.rangeCount) {
        for (let i = 0; i < s.rangeCount; i++) {
          const d = document.createElement('div');
          d.appendChild(s.getRangeAt(i).cloneContents());
          roots.push(d);
        }
      }
      const out = [];
      for (const r of (roots.length ? roots : [document])) {
        for (const a of r.querySelectorAll('a[href]')) {
          let u = '';
          try { u = new URL(a.href, location.href).href; } catch (e) { continue; }
          if (!/^https?:/.test(u)) continue;
          out.push({ url: u, text: (a.textContent || '').trim().slice(0, 80) });
        }
      }
      const seen = new Set();
      return out.filter(x => !seen.has(x.url) && seen.add(x.url)).slice(0, 800);
    })()`,
  });
  grabsByTab.set(tabId, results[0] || []);
  return results[0] || [];
}

/* -------------------------------------------------------- mesajlasma (UI) */
browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg.getMedia) {
      const map = mediaByTab.get(msg.tabId);
      return sendResponse({
        ok: true, hostOk, port: hostPort, turbo: hostTurbo,
        media: map ? [...map.values()] : [],
      });
    }
    if (msg.grab) {
      const links = await grabTab(msg.tabId, false);
      return sendResponse({ ok: true, links, hostOk, port: hostPort });
    }
    if (msg.download) {
      await handoff(msg.url, { referer: msg.referer || "",
                               filename: msg.filename || nameFromUrl(msg.url) });
      return sendResponse({ ok: true });
    }
    if (msg.downloadBatch) {
      for (const it of msg.items) {
        await handoff(it.url, { filename: it.filename || "",
                                referer: msg.referer || "" });
      }
      return sendResponse({ ok: true, count: msg.items.length });
    }
    if (msg.pingHost) return sendResponse({ ok: await pingHost(), port: hostPort });
    if (msg.turbo !== undefined) {
      try {
        const r = await sendNative({ cmd: "turbo", on: !!msg.turbo });
        hostTurbo = !!(r && r.turbo);
        return sendResponse({ ok: true, turbo: hostTurbo });
      } catch (e) {
        return sendResponse({ ok: false });
      }
    }
    if (msg.getTurbo) return sendResponse({ ok: true, turbo: hostTurbo });
    if (msg.openPanel && sender.tab) {
      browser.pageAction.open(sender.tab.id).catch(() => {});
      return sendResponse({ ok: true });
    }
    sendResponse({ ok: false });
  })();
  return true; // async yanit
});

/* ------------------------------------------------------------------ kurulum */
browser.downloads.onCreated.addListener(onDownloadCreated);
browser.webRequest.onHeadersReceived.addListener(
  onHeaders,
  { urls: ["http://*/*", "https://*/*"],
    types: ["media", "xmlhttprequest", "other", "sub_frame", "object"] },
  ["responseHeaders"]);

loadOpts().then(async () => {
  await refreshMenus();
  await pingHost();
  setInterval(pingHost, 60000);
});
