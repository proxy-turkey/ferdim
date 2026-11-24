/* FerDim Entegrasyon Modülü - arka plan betigi
 * Davranis modeli:
 *  - tarayici indirmesini yakala -> iptal et -> FerDim'e aktar
 *  - medya/indirme adaylarini webRequest ile dinle (siniflandirma
 *    sniff.js icinde, saf fonksiyon), sayfa basliginda rozet
 *  - sag tik menusu, "tum baglantilari tara" (grab) kutuphanesi
 *
 * MV2 ve MV3 ayni kaynaktan calisir (docs/mv3-spike.md). MV3'te arka plan
 * bir olay sayfasidir: sekme durumu storage.session'da tutulur, betik
 * enjeksiyonu scripting API'siyle yapilir, periyodik zamanlayici yoktur.
 */
/* global FerDimSniff */
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

/* MV2 (browser_action/page_action) ile MV3 (action) ayni kodu paylassin:
 * docs/mv3-spike.md. MV3 action'da show/hide yok, rozet zaten gorunur. */
const toolbar = browser.browserAction || browser.action;
const pageButton = browser.pageAction || {
  show: () => Promise.resolve(),
  hide: () => Promise.resolve(),
};

/* ---------------------------------------------------------------- options */
async function loadOpts() {
  const st = await browser.storage.local.get("opts");
  opts = { ...DEFAULT_OPTS, ...(st.opts || {}) };
}
browser.storage.onChanged.addListener((ch, area) => {
  if (area === "local" && ch.opts) loadOpts();
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
  toolbar.setBadgeBackgroundColor(
    { color: hostOk ? "#22c55e" : "#ef4444" });
  return hostOk;
}

/* ------------------------------------------------------------ sekme durumu */
/* mediaByTab: tabId -> Map(url -> sniff.js verdict'i). Bellekte onbellek,
 * storage.session'da kalici kopya: olay sayfasi uykuya dalinca harita
 * kaybolur, ilk erisimde geri yuklenir. storage.session yoksa (eski
 * tarayici) yalnizca bellek kullanilir. */
const sessionArea = (browser.storage && browser.storage.session) || null;
const MEDIA_KEY = "mediaByTab";
const mediaByTab = new Map();
let mediaReadyPromise = null;
/* Hidrasyon bitmeden yazma yok: soguk baslangicta onRemoved gibi bir olay
 * bos haritayi storage.session'a yazip onceki sekme kayitlarini silmesin. */
let mediaHydrated = false;

function mediaSnapshot() {
  const out = {};
  for (const [tabId, map] of mediaByTab) out[tabId] = [...map.values()];
  return out;
}

let persistQueued = false;
function persistMedia() {
  if (!sessionArea || !mediaHydrated || persistQueued) return;
  persistQueued = true;
  Promise.resolve().then(async () => {
    persistQueued = false;
    try {
      await sessionArea.set({ [MEDIA_KEY]: mediaSnapshot() });
    } catch (e) { /* kota/izin yok: bellek yeter */ }
  });
}

async function hydrateMedia() {
  try {
    if (sessionArea) {
      const st = await sessionArea.get(MEDIA_KEY);
      for (const [key, hits] of Object.entries((st && st[MEDIA_KEY]) || {})) {
        const tabId = Number(key);
        if (!Number.isInteger(tabId) || !Array.isArray(hits)) continue;
        const map = new Map();
        for (const h of hits) if (h && h.url) map.set(h.url, h);
        if (map.size) mediaByTab.set(tabId, map);
      }
    }
  } finally {
    // Okuma hata verse de kilit acilir: durum kalici olarak yazilamaz kalmasin
    mediaHydrated = true;
  }
  // Kapanmis sekmelerin kaydi kalmasin (sekme kimligi yeniden kullanilir).
  const tabs = await browser.tabs.query({});
  const live = new Set(tabs.map((t) => t.id));
  let dropped = false;
  for (const tabId of [...mediaByTab.keys()]) {
    if (!live.has(tabId)) { mediaByTab.delete(tabId); dropped = true; }
  }
  if (dropped) persistMedia();
}

/* Ilk erisimde bir kez yukle; es zamanli cagrilar ayni promise'i bekler. */
function mediaReady() {
  if (!mediaReadyPromise) {
    mediaReadyPromise = hydrateMedia().catch(() => {});
  }
  return mediaReadyPromise;
}

/* ------------------------------------------------------------------ utils */
function nameFromUrl(u) {
  try {
    const p = new URL(u).pathname.split("/").filter(Boolean).pop() || "";
    return decodeURIComponent(p);
  } catch { return ""; }
}
/* Tarayici indirmesi devralinirken kullanilir (baslik yok, yalnizca URL+mime). */
function looksMedia(u, mime) {
  const ul = (u || "").toLowerCase().split("?")[0];
  if (/\.(mp4|mkv|webm|flv|avi|mov|m4v|ts|3gp|ogv|m3u8|mpd)$/.test(ul)) return true;
  if (/\.(mp3|m4a|flac|ogg|opus|wav|aac)$/.test(ul)) return "audio";
  const m = (mime || "").toLowerCase();
  if (m.startsWith("video/") || m.includes("mpegurl")) return true;
  if (m.startsWith("audio/")) return "audio";
  return false;
}

/* ---------------------------------------------------- site ve cerez kapsami */
/* PSL yok (stdlib/DOM): kayitli alan adi yerine son iki etiket karsilastirilir.
 * "a.example.com" ile "b.example.com" ayni site sayilir; "co.uk" gibi cok
 * etiketli public suffix'lerde fazla eslesme kabul edilir. IP ve tek etiketli
 * host'larda tam eslesme istenir. */
function hostOf(u) {
  try { return new URL(u).hostname.toLowerCase(); } catch (e) { return ""; }
}
function isIpHost(h) {
  return h.startsWith("[") || h.includes(":") ||
         /^\d{1,3}(\.\d{1,3}){3}$/.test(h);
}
function siteOf(u) {
  const h = hostOf(u);
  if (!h || isIpHost(h)) return h;
  const labels = h.split(".");
  return labels.length <= 2 ? h : labels.slice(-2).join(".");
}
function sameSite(a, b) {
  const sa = siteOf(a);
  return !!sa && sa === siteOf(b);
}
/* Birinci taraf alani (FPI) baglamin host'uyla eslesiyor mu. */
function fpdMatches(fpd, ctxUrl) {
  const h = hostOf(ctxUrl);
  const f = String(fpd || "").toLowerCase();
  if (!h || !f) return false;
  return h === f || h.endsWith("." + f) || f.endsWith("." + h);
}
/* partitionKey.topLevelSite semali bir sitedir (https://example.com). */
function topLevelSite(u) {
  try {
    const p = new URL(u);
    if (p.protocol !== "https:" && p.protocol !== "http:") return "";
    const s = siteOf(u);
    return s ? p.protocol + "//" + s : "";
  } catch (e) { return ""; }
}

/* Cerezler: ayni site indirmede tam, siteler arasi indirmede yalnizca
 * SameSite=None. Boylece kotu niyetli bir sayfanin baslattigi capraz site
 * indirme baska sitenin oturum cerezlerini FerDim'e tasimaz (GET CSRF).
 * Bolumlu (CHIPS) cerezler yalnizca sekmenin ust duzey sitesiyle sinirli
 * sorguyla ve yalnizca ayni site baglaminda eklenir. */
async function collectCookies(url, ctxUrl) {
  const same = sameSite(url, ctxUrl);
  const seen = new Set();
  const parts = [];
  const add = (list) => {
    for (const c of list || []) {
      if (!c || !c.name) continue;
      if (!same && c.sameSite !== "no_restriction") continue;
      // FPI acik: baska bir birinci taraf baglaminin cerezleri gitmez
      if (c.firstPartyDomain && !fpdMatches(c.firstPartyDomain, ctxUrl)) {
        continue;
      }
      // Ayni cerez iki sorgudan da gelebilir: birebir tekrari at
      const key = c.name + "=" + c.value;
      if (seen.has(key)) continue;
      seen.add(key);
      parts.push(key);
    }
  };
  if (same) {
    const tls = topLevelSite(ctxUrl);
    if (tls) {
      try {
        add(await browser.cookies.getAll(
          { url, partitionKey: { topLevelSite: tls } }));
      } catch (e) { /* partitionKey desteklenmiyor */ }
    }
  }
  try {
    add(await browser.cookies.getAll({ url }));
  } catch (e) { return ""; }  // cookies izni yok
  return parts.join("; ");
}

/* ------------------------------------------------------- indirme yakalama */
async function onDownloadCreated(item) {
  await ready;
  if (item.state !== "in_progress") return;
  const url = item.url || "";
  if (!/^(https?|ftp):/i.test(url)) return; // blob:/data: yakalanamaz
  if (url.startsWith("http://127.0.0.1:" + hostPort)) return;

  if (opts.takeover === "off") return;
  const mime = item.mime || "";
  const media = looksMedia(url, mime);
  if (opts.takeover === "media" && !media) return;
  if (opts.takeover === "media" && media === "audio" && !opts.includeAudio) return;
  // MB = 1024^2 (panoda gosterilen boyutlarla ayni birim)
  const minBytes = (opts.minTakeoverMB || 0) * 1048576;
  const known = item.totalBytesSuggested || item.totalBytes || 0;
  if (minBytes && known && known < minBytes) return;

  // tarayici indirmesini iptal et, listeden sil
  try {
    await browser.downloads.cancel(item.id);
    await browser.downloads.erase({ id: item.id });
  } catch (e) { /* zaten bitti */ }

  // Baglam: indirmeyi baslatan sayfa. Cerez kapsami da buradan gelir.
  const ctx = item.referrer || await activeTabUrl();
  await handoff(url, {
    filename: item.filename ? item.filename.split(/[\\/]/).pop() : nameFromUrl(url),
    referer: opts.sendReferer ? ctx : "",
    ctx,
    mime,
  });
}

/* Cerez baglami icin: indirmeyi baslatan sekmenin adresi. */
async function activeTabUrl() {
  try {
    const tabs = await browser.tabs.query({ active: true, currentWindow: true });
    const u = tabs && tabs[0] && tabs[0].url;
    return u && /^https?:/.test(u) ? u : "";
  } catch (e) { return ""; }
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
    const ctx = extra.ctx || extra.referer || await activeTabUrl();
    const cookies = await collectCookies(url, ctx);
    if (cookies) msg.cookies = cookies;
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
/* Siniflandirma sniff.js icinde (saf fonksiyon); burada yalnizca kablolama:
 * parca isteklerini ele, rozette medya sayisi tut, overlay'e haber ver. */
function onHeaders(details) {
  if (details.tabId < 0) return;
  // Olay sayfasi uyanirken opts ve sekme durumu henuz yuklenmemis olabilir.
  mediaReady().then(async () => {
    await optsReady;
    const hit = FerDimSniff.classify(
      { url: details.url, status: details.statusCode,
        headers: details.responseHeaders },
      { minBytes: Math.round((opts.minMediaMB || 0) * 1048576) });
    if (!hit) return;
    if (hit.kind === "audio" && !opts.includeAudio) return;
    // HLS/DASH parcasi: indirilecek oge manifesttir, parca degil
    if (hit.role === "segment") return;
    recordHit(details.tabId, hit);
  }).catch(() => {});
}

function recordHit(tabId, hit) {
  let map = mediaByTab.get(tabId);
  if (map && map.has(hit.url)) return;
  const manifestSeen = map ? hasManifest(map) : false;
  if (hit.segmentish && manifestSeen) return;
  if (!map) { map = new Map(); mediaByTab.set(tabId, map); }
  map.set(hit.url, hit);
  if (hit.role === "manifest") dropSegments(map);
  if (map.size > 60) map.delete(map.keys().next().value);
  persistMedia();
  updateBadge(tabId);
  if (opts.overlayButton) {
    browser.tabs.sendMessage(tabId, {
      ferdimMedia: true, count: map.size,
    }).catch(() => {});
  }
}

function hasManifest(map) {
  for (const it of map.values()) if (it.role === "manifest") return true;
  return false;
}

/* Manifest bulundu: daha once eklenmis parca kayitlari listeden cikar. */
function dropSegments(map) {
  for (const it of map.values()) if (it.segmentish) map.delete(it.url);
}

function updateBadge(tabId) {
  const map = mediaByTab.get(tabId);
  const n = map ? map.size : 0;
  if (opts.badge) {
    toolbar.setBadgeText({ tabId, text: n ? String(n) : "" });
  }
  if (n) pageButton.show(tabId);
  else pageButton.hide(tabId).catch(() => {});
}

browser.tabs.onRemoved.addListener(async (tabId) => {
  // Hidrasyon beklemeden yazmak onceki sekme kayitlarini siler
  await mediaReady();
  if (mediaByTab.delete(tabId)) persistMedia();
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
/* Menu tanimlari tarayicida kalicidir ama olay sayfasi her uyanista yeniden
 * kurmamali: yalnizca kurulum/guncelleme ve tarayici acilisi. Cagrilar
 * sirayla islenir, boylece removeAll/create yarismaz. */
let menuQueue = Promise.resolve();
function refreshMenus() {
  const run = async () => {
    try { await browser.contextMenus.removeAll(); } catch (e) { /* yok */ }
    for (const d of menuDefs()) {
      try { browser.contextMenus.create({ ...d }); }
      catch (e) { /* ayni kimlik: onceki tur kurmus */ }
    }
  };
  menuQueue = menuQueue.then(run, run);
  return menuQueue;
}
browser.runtime.onInstalled.addListener(() => { refreshMenus(); });
if (browser.runtime.onStartup) {
  browser.runtime.onStartup.addListener(() => { refreshMenus(); });
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
    pageButton.show(tab.id).catch(() => {});
  }
});

/* --------------------------------------------------- sayfa baglanti tarama */
/* Enjekte edilen toplayici kapanis degiskeni kullanmaz: scripting API
 * islevi kaynagindan serilestirip sayfaya tasir. */
function collectLinks(selectionOnly) {
  const roots = [];
  if (selectionOnly) {
    const s = document.getSelection();
    if (s && !s.isCollapsed && s.rangeCount) {
      for (let i = 0; i < s.rangeCount; i++) {
        const d = document.createElement("div");
        d.appendChild(s.getRangeAt(i).cloneContents());
        roots.push(d);
      }
    }
  }
  const out = [];
  for (const r of (roots.length ? roots : [document])) {
    for (const a of r.querySelectorAll("a[href]")) {
      let u = "";
      try { u = new URL(a.href, location.href).href; } catch (e) { continue; }
      if (!/^https?:/.test(u)) continue;
      out.push({ url: u, text: (a.textContent || "").trim().slice(0, 80) });
    }
  }
  const seen = new Set();
  return out.filter((x) => !seen.has(x.url) && seen.add(x.url)).slice(0, 800);
}

async function grabTab(tabId, selectionOnly) {
  try {
    // MV3: scripting API. Koseli parantez bilinclidir: web-ext linter'i
    // MV3'te bu API'yi "kaldirilmis" sayar, oysa cagri yalnizca scripting
    // API'si olmayan (MV2, Firefox < 102) tarayicida calisir.
    const legacy = browser.tabs && browser.tabs["executeScript"];
    if (browser.scripting && browser.scripting.executeScript) {
      const res = await browser.scripting.executeScript({
        target: { tabId },
        func: collectLinks,
        args: [!!selectionOnly],
      });
      const first = res && res[0];
      if (first && first.error) return [];
      return (first && first.result) || [];
    }
    if (!legacy) return [];
    const res = await legacy.call(browser.tabs, tabId, {
      code: `(${collectLinks.toString()})(${JSON.stringify(!!selectionOnly)})`,
    });
    return (res && res[0]) || [];
  } catch (e) {
    return []; // about: sayfalari, iptal edilmis izin vb.
  }
}

/* -------------------------------------------------------- mesajlasma (UI) */
/* Guven: eklentinin kendi sayfalari (popup, secenekler) tam yetkilidir:
 * sender.id bizim ve sender.tab yok. Icerik betikleri (sender.tab dolu)
 * yalnizca kendi sekmeleri icin panel acabilir; sayfadaki bir betik
 * grab/download/turbo isteyemez. */
function trustedSender(sender) {
  return !!sender && sender.id === browser.runtime.id && !sender.tab;
}
function ownTabSender(sender) {
  return !!sender && sender.id === browser.runtime.id && !!sender.tab;
}

browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    await ready;
    if (!msg) return sendResponse({ ok: false });
    if (msg.openPanel) {
      if (!ownTabSender(sender)) return sendResponse({ ok: false });
      pageButton.show(sender.tab.id).catch(() => {});
      return sendResponse({ ok: true });
    }
    if (!trustedSender(sender)) return sendResponse({ ok: false });
    if (msg.getMedia) {
      await mediaReady();
      await pingHost();   // popup acildi: ana makine durumunu tazele
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
    sendResponse({ ok: false });
  })();
  return true; // async yanit
});

/* ------------------------------------------------------------------ kurulum */
/* Olay sayfasi her uyanista burayi calistirir: ayarlari yukle, ana makineyi
 * bir kez yokla. Periyodik zamanlayici yok (uykuda olurse zaten calismaz);
 * durum popup acilisinda tazelenir. */
const optsReady = loadOpts().catch(() => {});
const ready = optsReady.then(pingHost).catch(() => {});

browser.downloads.onCreated.addListener(onDownloadCreated);
browser.webRequest.onHeadersReceived.addListener(
  onHeaders,
  { urls: ["http://*/*", "https://*/*"],
    types: ["main_frame", "sub_frame", "media", "xmlhttprequest", "other",
            "object"] },
  ["responseHeaders"]);
