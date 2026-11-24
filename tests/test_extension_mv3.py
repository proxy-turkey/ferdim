"""Manifest V3 eklenti testleri (manifest, arka plan betigi, davranis).

1.7 ile eklenti kaynagi MV3'tur (docs/mv3-spike.md). Burada uc katman
dogrulanir:

- manifest: MV3 anahtarlari, izin bolunmesi, event page, Gecko kimligi;
- background.js: `tabs.executeScript` yalnizca scripting API'si yoksa
  calisan yedek yol; periyodik zamanlayici yok; menuler onInstalled/
  onStartup'ta;
- davranis: `node` ile sahte tarayici API'si (vm) icinde gercek betik
  calistirilir - sekme durumu storage.session'da kalir, yeni olay sayfasi
  uyaninca geri yuklenir, grab iki manifest surumunde de calisir.

node yoksa davranis sinifi atlanir; manifest/kaynak kontrolleri calisir.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
MANIFEST = EXT / "manifest.json"
BACKGROUND = EXT / "background.js"
SNIFF = EXT / "sniff.js"
LOCALES = EXT / "_locales"
LANG_DIRS = {"en", "tr", "es", "ru"}
NODE = shutil.which("node")

HARNESS = r"""'use strict';
const fs = require("fs");
const vm = require("vm");

const bgSrc = fs.readFileSync(process.argv[2], "utf8");
const sniffSrc = fs.readFileSync(process.argv[3], "utf8");

const tick = () => new Promise((r) => setTimeout(r, 0));

const EXT_ID = "ferdim@test";
const POPUP_SENDER = () => ({ id: EXT_ID, url: "moz-extension://test/popup.html" });
const TAB_SENDER = (tabId) => ({ id: EXT_ID, tab: { id: tabId } });

/* Sahte tarayici: yalnizca background.js'in kullandigi yuzey. */
function makeEnv(o) {
  o = o || {};
  const log = { scripting: [], executeScript: [], created: [], removed: 0,
                sessionSet: [], localSet: [], native: [], cookieQueries: [],
                pageShows: [] };
  const listeners = {};
  const on = (name) => ({
    addListener: (fn) => { (listeners[name] = listeners[name] || []).push(fn); },
  });
  const fire = async (name, ...args) => {
    for (const fn of listeners[name] || []) await fn(...args);
  };
  const area = (obj, logKey) => ({
    async get(k) {
      if (k === undefined || k === null) return { ...obj };
      return Object.prototype.hasOwnProperty.call(obj, k) ? { [k]: obj[k] } : {};
    },
    async set(v) {
      Object.assign(obj, v);
      log[logKey].push(JSON.parse(JSON.stringify(v)));
    },
  });
  const liveMenus = new Set();
  const browser = {
    i18n: { getMessage: (k) => k },
    storage: {
      local: area(o.local || {}, "localSet"),
      onChanged: on("storageChanged"),
    },
    runtime: {
      id: EXT_ID,
      /* sendNativeMessage(uygulama, mesaj): ikinci arguman yuk. */
      sendNativeMessage: async (app, msg) => {
        log.native.push(JSON.parse(JSON.stringify(msg || {})));
        return { ok: true, port: 8378, turbo: false };
      },
      getURL: (p) => "moz-extension://test/" + p,
      getManifest: () => ({ version: "0.0.0" }),
      onMessage: on("message"),
      onInstalled: on("installed"),
      onStartup: on("startup"),
    },
    browserAction: {
      setBadgeText: async () => {},
      setBadgeBackgroundColor: async () => {},
    },
    pageAction: {
      show: async (tabId) => { log.pageShows.push(tabId); },
      hide: async () => {},
    },
    contextMenus: {
      removeAll: async () => { log.removed += 1; liveMenus.clear(); },
      create: (d) => {
        if (liveMenus.has(d.id)) throw new Error("duplicate id " + d.id);
        liveMenus.add(d.id);
        log.created.push(d);
      },
      onClicked: on("menuClick"),
    },
    tabs: {
      query: async () => o.tabs || [],
      sendMessage: async () => {},
      executeScript: async (tabId, details) => {
        log.executeScript.push({ tabId: tabId, details: details });
        return [o.legacyLinks || []];
      },
      onRemoved: on("tabRemoved"),
    },
    downloads: {
      onCreated: on("downloadCreated"),
      cancel: async () => {},
      erase: async () => {},
    },
    webRequest: { onHeadersReceived: on("headers") },
    /* partitionKey'siz sorgu bolumlenmemis cerezleri doner; partitionKey'li
     * sorgu yalnizca o ust duzey siteye ait bolumlu cerezleri (Firefox
     * davranisi: ikisi ayri sorulur). */
    cookies: {
      getAll: async (details) => {
        log.cookieQueries.push(JSON.parse(JSON.stringify(details)));
        const all = o.cookies || [];
        const want = details.partitionKey;
        return all.filter((c) => want
          ? !!c.partitionKey && c.partitionKey.topLevelSite === want.topLevelSite
          : !c.partitionKey);
      },
    },
    notifications: { create: () => {} },
  };
  if (o.scripting !== false) {
    browser.scripting = {
      executeScript: async (details) => {
        log.scripting.push({
          tabId: details.target && details.target.tabId,
          isFunc: typeof details.func === "function",
          funcSource: String(details.func),
          args: details.args,
        });
        return [{ frameId: 0, result: o.scriptingResult || [] }];
      },
    };
  }
  if (o.session) browser.storage.session = area(o.session, "sessionSet");

  const sandbox = {
    browser: browser,
    navigator: { userAgent: "node-test" },
    URL: URL,
    Set: Set,
    console: console,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
  };
  vm.createContext(sandbox);
  vm.runInContext(sniffSrc, sandbox, { filename: "sniff.js" });
  vm.runInContext(bgSrc, sandbox, { filename: "background.js" });
  return { log: log, listeners: listeners, fire: fire };
}

const sendMessage = (env, msg, sender) => new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error("sendResponse yok")), 5000);
  const sendResponse = (r) => { clearTimeout(timer); resolve(r); };
  try {
    for (const fn of env.listeners.message || []) {
      fn(msg, sender || POPUP_SENDER(), sendResponse);
    }
  } catch (e) { clearTimeout(timer); reject(e); }
});

(async () => {
  const out = {};

  /* menu kurulumu iki kez: yinelenen kimlik olmadan 4+4 tanim */
  {
    const env = makeEnv({});
    await env.fire("installed");
    await env.fire("installed");
    await tick();
    await tick();
    out.menus = {
      removed: env.log.removed,
      ids: env.log.created.map((d) => d.id),
      startupHooks: (env.listeners.startup || []).length,
    };
  }

  /* medya durumu: storage.session'a yaz, uyanan olay sayfasi geri yukler */
  const session = {};
  const env1 = makeEnv({ session: session, tabs: [{ id: 7 }] });
  await env1.fire("headers", {
    tabId: 7, url: "https://cdn.example/f/movie.mp4", statusCode: 200,
    responseHeaders: [{ name: "content-type", value: "video/mp4" }],
  });
  await tick();
  await tick();
  out.persistedKeys = Object.keys(session.mediaByTab || {});
  out.firstMedia = (await sendMessage(env1, { getMedia: true, tabId: 7 })).media;

  const env2 = makeEnv({ session: session, tabs: [{ id: 7 }] });
  out.afterRestart = (await sendMessage(env2, { getMedia: true, tabId: 7 })).media;

  /* kapanmis sekme hidrasyonda dusurulur */
  const env3 = makeEnv({ session: session, tabs: [{ id: 99 }] });
  await sendMessage(env3, { getMedia: true, tabId: 99 });
  await tick();
  await tick();
  out.afterPrune = Object.keys(session.mediaByTab || {});

  /* sekme kapaninca kayit silinir */
  const env4 = makeEnv({ session: session, tabs: [{ id: 7 }] });
  await sendMessage(env4, { getMedia: true, tabId: 7 });
  await env4.fire("tabRemoved", 7);
  await tick();
  await tick();
  out.afterTabRemoved = Object.keys(session.mediaByTab || {});

  /* soguk baslangic: hidrasyon bitmeden sekme kapanir. Bos harita
   * yazilirsa diger sekmelerin kayitli medyasi silinir. */
  {
    const hit = (name) => ({ url: "https://cdn.example/f/" + name + ".mp4",
                             kind: "video", role: "direct",
                             filename: name + ".mp4" });
    const cold = { mediaByTab: { 7: [hit("gone")], 9: [hit("keep")] } };
    const env = makeEnv({ session: cold, tabs: [{ id: 7 }, { id: 9 }] });
    await env.fire("tabRemoved", 7);          // hidrasyon calismamis
    await tick();
    await tick();
    out.coldStart = {
      keys: Object.keys(cold.mediaByTab || {}),
      sets: env.log.sessionSet.length,
      media: (await sendMessage(env, { getMedia: true, tabId: 9 })).media
        .map((m) => m.url),
    };
  }

  /* storage.session yok (MV2): durum yalnizca bellekte, ayni baglamda calisir */
  {
    const env = makeEnv({ tabs: [{ id: 7 }] });
    await env.fire("headers", {
      tabId: 7, url: "https://cdn.example/f/track.mp3", statusCode: 200,
      responseHeaders: [{ name: "content-type", value: "audio/mpeg" }],
    });
    await tick();
    await tick();
    out.memoryOnlyMedia =
      (await sendMessage(env, { getMedia: true, tabId: 7 })).media;
  }

  /* grab: scripting API (MV3, Firefox >= 102)/ */
  {
    const env = makeEnv({
      scriptingResult: [{ url: "https://x.example/a.zip", text: "a" }] });
    const r = await sendMessage(env, { grab: true, tabId: 7 });
    out.grabMV3 = {
      links: r.links,
      calls: env.log.scripting,
      legacyCalls: env.log.executeScript.length,
    };
  }

  /* grab: scripting API yok -> tabs.executeScript kodu */
  {
    const env = makeEnv({ scripting: false,
                          legacyLinks: [{ url: "https://x.example/b.zip" }] });
    const r = await sendMessage(env, { grab: true, tabId: 7 });
    out.grabMV2 = {
      links: r.links,
      calls: env.log.executeScript.map((c) => ({
        tabId: c.tabId, code: c.details && c.details.code })),
    };
  }

  /* enjekte edilen toplayici serilestirilmis kaynaktan sayfada calisir */
  {
    const env = makeEnv({ scriptingResult: [] });
    await sendMessage(env, { grab: true, tabId: 7 });
    const source = env.log.scripting[0].funcSource;
    const page = vm.createContext({
      URL: URL,
      Set: Set,
      location: { href: "https://page.example/dir/" },
      document: {
        querySelectorAll: () => [
          { href: "https://cdn.example/a.zip", textContent: " A " },
          { href: "mailto:x@example", textContent: " mail " },
          { href: "https://cdn.example/a.zip", textContent: " A " },
        ],
      },
    });
    out.extract = vm.runInContext("(" + source + ")", page)(false);
  }

  /* cerezler: ayni site indirme tam cerez tasir; bolumlu cerez sekmenin
   * ust duzey sitesiyle sinirli sorgudan gelir */
  {
    const env = makeEnv({ cookies: [
      { name: "sid", value: "S1", domain: "files.example.com",
        sameSite: "strict", firstPartyDomain: "" },
      { name: "part", value: "P1", domain: "files.example.com",
        sameSite: "lax", firstPartyDomain: "",
        partitionKey: { topLevelSite: "https://example.com" } },
    ] });
    await env.fire("downloadCreated", {
      id: 1, state: "in_progress",
      url: "https://files.example.com/f/big.zip",
      referrer: "https://www.example.com/page",
      filename: "/home/u/Downloads/big.zip", mime: "application/zip",
    });
    out.sameSiteCookies = {
      cookies: (env.log.native.filter((m) => m.cmd === "download")[0] || {})
        .cookies,
      queries: env.log.cookieQueries,
    };
  }

  /* cerezler: kotu niyetli sayfanin baslattigi capraz site indirme yalnizca
   * SameSite=None cerezleri tasir, baska bir birinci taraf baglaminin (FPI)
   * cerezleri hic eklenmez */
  {
    const env = makeEnv({ cookies: [
      { name: "sid", value: "SECRET", sameSite: "strict", firstPartyDomain: "" },
      { name: "lax", value: "LAX", sameSite: "lax", firstPartyDomain: "" },
      { name: "none", value: "N1", sameSite: "no_restriction",
        firstPartyDomain: "" },
      { name: "fpiOther", value: "X", sameSite: "no_restriction",
        firstPartyDomain: "other.example" },
      { name: "fpiOk", value: "OK", sameSite: "no_restriction",
        firstPartyDomain: "evil.test" },
    ] });
    await env.fire("downloadCreated", {
      id: 2, state: "in_progress",
      url: "https://victim.example/f/invoice.pdf",
      referrer: "https://evil.test/hostile",
      mime: "application/pdf",
    });
    out.crossSiteCookies = {
      cookies: (env.log.native.filter((m) => m.cmd === "download")[0] || {})
        .cookies,
      queries: env.log.cookieQueries,
    };
  }

  /* mesaj guveni: icerik betigi (sender.tab dolu) ve yabanci eklenti
   * grab/download/turbo isteyemez; popup isteyebilir; panel yalnizca kendi
   * sekmesi icin acilir */
  {
    const env = makeEnv({ scriptingResult: [{ url: "https://x.example/a.zip" }] });
    out.senders = {
      contentGrab: await sendMessage(env, { grab: true, tabId: 7 },
                                     TAB_SENDER(3)),
      contentDownload: await sendMessage(
        env, { download: true, url: "https://victim.example/x" }, TAB_SENDER(3)),
      contentTurbo: await sendMessage(env, { turbo: true }, TAB_SENDER(3)),
      contentGetMedia: await sendMessage(env, { getMedia: true, tabId: 7 },
                                         TAB_SENDER(3)),
      foreignGrab: await sendMessage(env, { grab: true, tabId: 7 },
                                     { id: "other@ext" }),
      foreignPanel: await sendMessage(env, { openPanel: true },
                                      { id: "other@ext", tab: { id: 3 } }),
      popupGrab: await sendMessage(env, { grab: true, tabId: 7 }),
      popupTurbo: await sendMessage(env, { turbo: true }),
      popupDownload: await sendMessage(
        env, { download: true, url: "https://page.example/a.zip" }),
      contentPanel: await sendMessage(env, { openPanel: true }, TAB_SENDER(3)),
      scriptingTabs: env.log.scripting.map((c) => c.tabId),
      downloads: env.log.native.filter((m) => m.cmd === "download")
        .map((m) => m.url),
      turbos: env.log.native.filter((m) => m.cmd === "turbo").length,
      pageShows: env.log.pageShows,
    };
  }

  process.stdout.write(JSON.stringify(out));
})().catch((e) => {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(1);
});
"""


class ManifestV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_version(self):
        self.assertEqual(self.m["manifest_version"], 3)

    def test_host_permissions_split(self):
        self.assertEqual(self.m["host_permissions"], ["<all_urls>"])
        self.assertNotIn("<all_urls>", self.m["permissions"])

    def test_permissions_kept(self):
        perms = set(self.m["permissions"])
        for p in ("downloads", "webRequest", "contextMenus", "cookies",
                  "storage", "tabs", "notifications", "nativeMessaging",
                  "scripting"):
            self.assertIn(p, perms)

    def test_event_page_not_service_worker(self):
        bg = self.m["background"]
        self.assertNotIn("service_worker", bg)  # Firefox MV3 event page
        self.assertNotIn("persistent", bg)
        self.assertEqual(bg["scripts"], ["sniff.js", "background.js"])
        for name in bg["scripts"]:
            self.assertTrue((EXT / name).is_file(), name)

    def test_action_namespace(self):
        self.assertIn("action", self.m)
        self.assertNotIn("browser_action", self.m)
        self.assertEqual(self.m["action"]["default_popup"], "popup.html")
        self.assertIn("page_action", self.m)  # Firefox MV3 page_action korur
        for key in ("action", "page_action"):
            page = self.m[key]["default_popup"]
            self.assertTrue((EXT / page).is_file(), page)

    def test_gecko_identity_and_floor(self):
        gecko = self.m["browser_specific_settings"]["gecko"]
        self.assertEqual(gecko["id"], "ferdim@ferdim.app")
        # storage.session Firefox 115, MV3 109: taban ikisini de kapsar.
        self.assertGreaterEqual(float(gecko["strict_min_version"]), 115.0)
        self.assertIn("data_collection_permissions", gecko)
        self.assertIn("gecko_android",
                      self.m["browser_specific_settings"])

    def test_options_and_content_script(self):
        self.assertEqual(self.m["options_ui"]["page"], "options.html")
        match = self.m["content_scripts"][0]["matches"]
        self.assertIn("https://*/*", match)
        self.assertEqual(self.m["content_scripts"][0]["js"], ["overlay.js"])


class BackgroundSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = BACKGROUND.read_text(encoding="utf-8")

    def test_scripting_api_with_guarded_fallback(self):
        self.assertIn("browser.scripting.executeScript", self.src)
        self.assertIn("target: { tabId }", self.src)
        self.assertIn("func: collectLinks", self.src)
        self.assertIn("args: [!!selectionOnly]", self.src)
        # MV2 yedegi koseli parantezle: web-ext linter'i MV3'te duz erisimi
        # "kaldirilmis API" sayar, oysa cagri yalnizca scripting yoksa calisir.
        self.assertIn('browser.tabs["executeScript"]', self.src)
        self.assertNotRegex(self.src, r"tabs\.executeScript")
        # scripting destegi once denenir; kodu calistiran yedek cagri sonra.
        self.assertLess(
            self.src.index("if (browser.scripting && browser.scripting.executeScript)"),
            self.src.index("legacy.call("))

    def test_no_periodic_timer_in_event_page(self):
        self.assertNotIn("setInterval", self.src)

    def test_menus_registered_on_install_and_startup(self):
        self.assertIn("runtime.onInstalled.addListener", self.src)
        self.assertIn("runtime.onStartup.addListener", self.src)

    def test_media_state_in_storage_session(self):
        self.assertIn("browser.storage.session", self.src)
        self.assertIn('MEDIA_KEY = "mediaByTab"', self.src)
        self.assertIn("sessionArea.set(", self.src)

    def test_persist_waits_for_hydration(self):
        """Soguk baslangicta yazma hidrasyona baglidir: bos harita
        storage.session'daki onceki sekme kayitlarini silerdi."""
        self.assertIn("!mediaHydrated", self.src)
        self.assertLess(self.src.index("!mediaHydrated"),
                        self.src.index("sessionArea.set("))
        block = self.src.split("browser.tabs.onRemoved.addListener", 1)[1]
        self.assertIn("await mediaReady()", block.split("});", 1)[0])

    def test_native_messaging_kept(self):
        self.assertIn('const NATIVE = "ferdim"', self.src)
        self.assertIn("runtime.sendNativeMessage", self.src)


@unittest.skipUnless(NODE and BACKGROUND.is_file(), "node bulunamadi")
class BackgroundBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ferdim-mv3-")
        harness = Path(cls.tmp) / "harness.cjs"
        harness.write_text(HARNESS, encoding="utf-8")
        res = subprocess.run(
            [NODE, str(harness), str(BACKGROUND), str(SNIFF)],
            text=True, capture_output=True, timeout=120)
        if res.returncode != 0:
            raise AssertionError(f"harness: {res.stderr}")
        cls.out = json.loads(res.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_menus_idempotent_on_install(self):
        m = self.out["menus"]
        self.assertEqual(m["removed"], 2)  # her turda removeAll
        self.assertEqual(m["ids"], ["fd-link", "fd-media", "fd-page",
                                    "fd-selection"] * 2)
        self.assertEqual(m["startupHooks"], 1)

    def test_media_survives_event_page_restart(self):
        self.assertEqual(self.out["persistedKeys"], ["7"])
        first = self.out["firstMedia"]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["kind"], "video")
        self.assertEqual(first[0]["role"], "direct")
        self.assertEqual(first[0]["filename"], "movie.mp4")
        after = self.out["afterRestart"]
        self.assertEqual(len(after), 1, "storage.session'dan geri yuklenmeli")
        self.assertEqual(after[0]["url"], first[0]["url"])

    def test_stale_tab_media_pruned(self):
        self.assertEqual(self.out["afterPrune"], [])

    def test_tab_close_clears_session_state(self):
        self.assertEqual(self.out["afterTabRemoved"], [])

    def test_cold_start_tab_close_keeps_other_tabs(self):
        """Hidrasyon bitmeden gelen onRemoved bos haritayi yazip diger
        sekmelerin medyasini silmemeli."""
        cold = self.out["coldStart"]
        self.assertEqual(cold["keys"], ["9"], cold)
        self.assertEqual(cold["media"], ["https://cdn.example/f/keep.mp4"])
        self.assertEqual(cold["sets"], 1)

    def test_mv2_without_session_still_serves_memory(self):
        media = self.out["memoryOnlyMedia"]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["kind"], "audio")

    def test_grab_uses_scripting_api(self):
        g = self.out["grabMV3"]
        self.assertEqual(g["legacyCalls"], 0)
        self.assertEqual(len(g["calls"]), 1)
        call = g["calls"][0]
        self.assertEqual(call["tabId"], 7)
        self.assertTrue(call["isFunc"], "func enjekte edilmeli")
        self.assertIn("function collectLinks", call["funcSource"])
        self.assertEqual(call["args"], [False])
        self.assertEqual(g["links"], [{"url": "https://x.example/a.zip",
                                       "text": "a"}])

    def test_grab_falls_back_to_tabs_execute_script(self):
        g = self.out["grabMV2"]
        self.assertEqual(g["links"], [{"url": "https://x.example/b.zip"}])
        self.assertEqual(len(g["calls"]), 1)
        call = g["calls"][0]
        self.assertEqual(call["tabId"], 7)
        self.assertIn("(function collectLinks(selectionOnly)", call["code"])
        self.assertTrue(call["code"].endswith(")(false)"))

    def test_injected_extractor_is_self_contained(self):
        self.assertEqual(self.out["extract"],
                         [{"url": "https://cdn.example/a.zip", "text": "A"}])

    def test_same_site_download_carries_cookies(self):
        """Ayni site indirme cerezleri korur; bolumlu cerez sekmenin ust
        duzey sitesiyle sinirli sorgudan, digerleri bolumlenmemis sorgudan."""
        s = self.out["sameSiteCookies"]
        self.assertEqual(s["cookies"], "part=P1; sid=S1", s)
        self.assertEqual(len(s["queries"]), 2, s)
        self.assertEqual(s["queries"][0]["partitionKey"],
                         {"topLevelSite": "https://example.com"})
        self.assertNotIn("partitionKey", s["queries"][1])
        self.assertEqual(
            s["queries"][1]["url"], "https://files.example.com/f/big.zip")

    def test_cross_site_download_only_sends_samesite_none(self):
        """Capraz site indirmede Strict/Lax cerez yok: kotu niyetli sayfa
        baska sitenin oturumuyla GET yaptiramaz."""
        c = self.out["crossSiteCookies"]
        self.assertEqual(c["cookies"], "none=N1; fpiOk=OK", c)
        # bolumlu sorgu yok: bolum anahtari sekmenin sitesi, hedef baska site
        self.assertEqual(c["queries"],
                         [{"url": "https://victim.example/f/invoice.pdf"}])

    def test_content_script_sender_cannot_act(self):
        s = self.out["senders"]
        self.assertFalse(s["contentGrab"]["ok"])
        self.assertFalse(s["contentDownload"]["ok"])
        self.assertFalse(s["contentTurbo"]["ok"])
        self.assertFalse(s["contentGetMedia"]["ok"])
        # grab enjeksiyonu yalnizca popup isteginde calisti
        self.assertEqual(s["scriptingTabs"], [7])
        # indirme ve turbo yalnizca popup isteklerinden gitti
        self.assertEqual(s["downloads"], ["https://page.example/a.zip"])
        self.assertEqual(s["turbos"], 1)

    def test_popup_sender_can_act(self):
        s = self.out["senders"]
        self.assertTrue(s["popupGrab"]["ok"])
        self.assertEqual(s["popupGrab"]["links"],
                         [{"url": "https://x.example/a.zip"}])
        self.assertTrue(s["popupTurbo"]["ok"])
        self.assertTrue(s["popupDownload"]["ok"])

    def test_overlay_panel_only_own_tab(self):
        s = self.out["senders"]
        self.assertTrue(s["contentPanel"]["ok"])
        self.assertFalse(s["foreignPanel"]["ok"])
        self.assertEqual(s["pageShows"], [3])


class LocaleParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = {
            d.name: json.loads((d / "messages.json").read_text(encoding="utf-8"))
            for d in sorted(LOCALES.iterdir()) if d.is_dir()
        }

    def test_all_four_languages_present(self):
        self.assertEqual(set(self.messages), LANG_DIRS)

    def test_same_keys_everywhere(self):
        keys = {lang: frozenset(m) for lang, m in self.messages.items()}
        en = keys["en"]
        for lang in LANG_DIRS - {"en"}:
            self.assertEqual(keys[lang], en, f"{lang} anahtar farki")

    def test_html_and_script_keys_exist(self):
        used = set()
        for html in ("popup.html", "options.html"):
            text = (EXT / html).read_text(encoding="utf-8")
            used |= set(re.findall(r'data-i18n(?:-ph)?="([A-Za-z0-9_]+)"', text))
        for js in ("background.js", "popup.js", "options.js"):
            text = (EXT / js).read_text(encoding="utf-8")
            used |= set(re.findall(r'_\("([A-Za-z0-9_]+)"', text))
        missing = sorted(used - set(self.messages["en"]))
        self.assertEqual(missing, [], "eksik i18n anahtarlari")
        for lang in LANG_DIRS:
            self.assertEqual(sorted(used - set(self.messages[lang])), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
