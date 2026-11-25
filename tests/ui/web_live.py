"""FerDim web arayuzu canli testi: yaltilmis daemon + gercek Chromium
(playwright). PORT, SRV, SLOW, TOKEN_PATH, SOCK, WATCH_BASE cevre
degiskenlerini run.sh verir; UI_SHOTS verilirse ekran goruntuleri oraya
yazilir (yoksa gecici dizin). Sayfa token tasimaz: tarayici, soketten alinan
tek kullanimlik biletli adresle (#s=<nonce>) acilir. WATCH_BASE daemon'in ev
dizininin altinda olmali: HTTP config-set baska yollari reddeder."""
import datetime as dt
import json
import os
import socket
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

PORT = int(os.environ["PORT"])
SRV = os.environ["SRV"]
SLOW = os.environ["SLOW"]
TOKEN = open(os.environ["TOKEN_PATH"]).read().strip()
SOCK = os.environ.get("SOCK") or os.path.join(
    os.path.dirname(os.environ["TOKEN_PATH"]), "ferdim.sock")
OUT = os.environ.get("UI_SHOTS") or tempfile.mkdtemp(prefix="ferdim-ui-shots-")
# HTTP config-set yalniz ev dizininin ALTINDAKI klasorleri kabul eder;
# run.sh daemon'in HOME'unu bu gecici koke tasir (gercek ~/.config'e
# dokunulmaz), bu yuzden yol zorunlu: yanlislikla gercek eve yazilmasin.
WATCH_BASE = os.environ["WATCH_BASE"]
WATCH_DIRS = [os.path.join(WATCH_BASE, "izlenen"),
              os.path.join(WATCH_BASE, "ikinci")]
for _d in WATCH_DIRS:
    os.makedirs(_d, exist_ok=True)
fails, errors = [], []


def web_session() -> str:
    """`ferdim web` ile ayni yol: soketten bilet, tek kullanimlik adres."""
    req = json.dumps({"cmd": "web-session", "req_id": 1}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(SOCK)
        s.sendall(req)
        buf = b""
        while b"\n" not in buf:
            part = s.recv(65536)
            if not part:
                break
            buf += part
    r = json.loads(buf.split(b"\n", 1)[0])
    if not r.get("ok") or not r.get("url"):
        raise SystemExit(f"web-session basarisiz: {r}")
    return r["url"]


def api(path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-FerDim-Token": TOKEN, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name +
          (f"   -> {str(extra)[:200]}" if not cond else ""))
    if not cond:
        fails.append(name)


def jobs():
    return {j["filename"]: j for j in api("/api/stats")["jobs"]}


def wait_job(name, states, timeout=20):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        j = jobs().get(name)
        if j and j["state"] in states:
            return j
        time.sleep(0.2)
    return jobs().get(name)


def closed_window():
    now = dt.datetime.now()
    m = now.hour * 60 + now.minute
    fmt = lambda x: f"{(x // 60) % 24:02d}:{x % 60:02d}"
    return fmt(m + 10), fmt(m + 20)       # su an +10..+20 dk: simdi kapali


# ---------------------------------------------------------------- on hazirlik
st, sp = closed_window()
api("/api/config", {"config": {"schedule": {
    "enabled": False, "start": "02:00", "stop": "08:00",
    "categories": {"video": {"start": st, "stop": sp}}}}})
r = api("/api/add", {"url": f"{SRV}/arsiv.zip"})
check("arsiv.zip eklendi", r.get("ok"), r)
j = wait_job("arsiv.zip", ("done", "error"))
check("arsiv.zip bitti + dogrulandi", j and j["state"] == "done"
      and j["checksum"] == "ok" and "sha256" in j["verified"], j)
api("/api/add", {"url": f"{SRV}/rapor.pdf"})
j = wait_job("rapor.pdf", ("done", "error"))
check("rapor.pdf bayat yan dosya -> uyari", j and j["checksum"] == "warn", j)
api("/api/add", {"url": f"{SRV}/yazilim.deb", "paused": True})
j = wait_job("yazilim.deb", ("paused",))
check("yazilim.deb duraklatili eklendi", j and j["state"] == "paused", j)
api("/api/add", {"url": f"{SRV}/yok-iso.iso"})
j = wait_job("yok-iso.iso", ("error",))
check("olmayan dosya -> hata", j and j["state"] == "error" and j["error"], j)
api("/api/add", {"url": f"{SRV}/ornek-video.mp4"})
time.sleep(1.5)
j = jobs().get("ornek-video.mp4")
check("video kategori penceresini bekliyor", j and j["state"] == "queued"
      and j["sched_wait"] and j["window"] == f"{st}-{sp}", j)

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 880},
                              locale="tr-TR")
    page = ctx.new_page()

    def on_console(m):
        if m.type != "error":
            return
        loc = (m.location or {}).get("url") or ""
        # /api/hosts HTTP yolunu sunucu lane'i ekliyor; yol yokken tarayici
        # 404'u konsola yazar (istek panelde durum metnine donusuyor).
        if "404" in m.text and loc.endswith("/api/hosts"):
            return
        errors.append("console: " + m.text)

    page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    page.on("console", on_console)
    reqs = []
    page.on("request", lambda r: reqs.append(r.url))
    url = web_session()
    page.goto(url)
    # bilet adres cubugundan silinmis olmali (#s=... kalici olmamali)
    check("bilet adres cubugundan silinir",
          "#s=" not in page.url and page.url.startswith(
              f"http://127.0.0.1:{PORT}/"), page.url)
    page.wait_for_selector("#jobList .row", timeout=15000)
    page.wait_for_timeout(900)

    # ------------------------------------------------------------- yapi
    check("<html lang>", page.evaluate("document.documentElement.lang") == "tr")
    check("<title>", page.title() == "FerDim — İndirme Yöneticisi", page.title())
    q = lambda sel: page.locator(sel).count()
    check("tablist/tab (7)", q('[role="tablist"] [role="tab"]') == 7)
    check("tabpanel <- aria-labelledby aktif sekme",
          page.get_attribute("#jobPanel", "aria-labelledby") == "tab-all")
    check("tablo/sutun basliklari", q('[role="table"] [role="columnheader"]') == 6)
    check("satirlar role=row + hucreler",
          q('#jobList [role="row"]') == 5 and q('#jobList [role="cell"]') == 30)
    check("ilerleme cubuklari progressbar", q('[role="progressbar"]') == 5)
    check("hiz/baglanti grafigi canvaslari cizilir", page.evaluate("""() =>
          [document.querySelector('#speedGraph'), document.querySelector('#connGraph')]
            .every(c => c && c.getBoundingClientRect().width > 100
                     && c.getBoundingClientRect().height >= 40
                     && c.width >= c.getBoundingClientRect().width)"""))
    check("grafik aria-label degeri tasir", page.evaluate(
        """() => /[0-9]/.test(document.querySelector('#speedGraph')
             .getAttribute('aria-label') || '')"""))
    check("simge dugmelerinin adi var",
          page.evaluate("""() => [...document.querySelectorAll('#jobList .act')]
             .filter(b => !b.hidden && !(b.getAttribute('aria-label') || '').trim()).length""") == 0)
    check("atla baglantisi", page.locator("a.skip").get_attribute("href") == "#jobPanel")

    # --------------------------------------- oturumsuz sayfa (token sizmaz)
    # Sayfayi okuyabilen baska bir yerel kullanici API'yi kullanamaz: token
    # gomulmez, sayfa yalniz "ferdim web" iletisini gosterir.
    ctx0 = browser.new_context(viewport={"width": 1000, "height": 700},
                               locale="tr-TR")
    p0 = ctx0.new_page()
    p0.goto(f"http://127.0.0.1:{PORT}/")
    p0.wait_for_selector("#noSession", state="visible", timeout=10000)
    check(" bilet yok: yonlendirme iletisi gorunur",
          "ferdim web" in p0.inner_text("#noSession"), p0.inner_text("#noSession"))
    check(" bilet yok: is listesi bos",
          p0.locator("#jobList .row").count() == 0)
    check(" bilet yok: token sayfada yok", TOKEN not in p0.content())
    ctx0.close()

    # --------------------------------------------------------- icerik
    txt = page.inner_text("#jobList")
    for needle in ("arsiv.zip", "doğrulandı: sha256", "sağlama uyuşmadı",
                   "penceresini bekliyor", f"{st}-{sp}", "rapor.pdf",
                   "yazilim.deb", "yok-iso.iso", "ornek-video.mp4"):
        check(f"satir metni: {needle!r}", needle in txt)
    row = page.locator("#jobList .row", has_text="yazilim.deb")
    check("duraklatilmis is: devam dugmesi var, duraklat yok",
          row.locator('[data-act="resume"]').is_visible()
          and not row.locator('[data-act="pause"]').is_visible())
    bar = page.locator("#jobList .row", has_text="arsiv.zip").locator('[role="progressbar"]')
    check("dogrulanan isin cubugu %100", bar.get_attribute("aria-valuenow") == "100")
    check("hata isi hata metni tasir", "⚠" in page.inner_text(
        "#jobList .row:has-text('yok-iso.iso')"))
    page.screenshot(path=f"{OUT}/01-liste.png")

    # ------------------------------------------- akis (SSE) tek canli yol
    check("akis /api/events acildi",
          any("/api/events" in u for u in reqs), reqs[:5])
    before = sum(1 for u in reqs if "/api/stats" in u)
    page.wait_for_timeout(2200)          # akis acikken yoklama olmamali
    after = sum(1 for u in reqs if "/api/stats" in u)
    check(" akis acikken /api/stats yoklamasi yok", before == after,
          (before, after))
    # yeni is yoklamasiz gorunur, silinen satir akisla duser
    r = api("/api/add", {"url": f"{SRV}/rapor.pdf", "filename": "akis.pdf",
                         "paused": True})
    jid = r.get("id", "")
    page.wait_for_selector("#jobList .row:has-text('akis.pdf')", timeout=8000)
    check(" SSE: yeni is akisla geldi", True)
    api("/api/remove", {"id": jid, "delete_files": True})
    page.wait_for_selector("#jobList .row:has-text('akis.pdf')",
                           state="detached", timeout=8000)
    check(" SSE: silinen is listeden dustu", True)
    check("gecmis: sayfa dolmadikca 'daha fazla' gizli",
          not page.is_visible("#histMore"))

    # ---------------------------------------------------- XSS (textContent)
    # Veri artik SSE akisindan gelir: hem akis hem yoklama yedegi
    # zehirlenir, boylece hangi yol calisirsa calissin ayni kural sinanir.
    poison_job = {
        "id": "xss-1", "filename": '<img src=x onerror="window.__xss=1">',
        "url": "javascript:window.__xss=3", "state": "error",
        "error": '<svg onload="window.__xss=2"></svg>', "error_code": "",
        "category": "other", "size": 10, "done": 5, "speed": 0, "eta": -1,
        "segments": 0, "segments_cfg": 1, "segments_effective": 1,
        "checksum": "", "merkle": "", "verified": [], "sources_dead": 0,
        "sched_wait": False, "window": "", "verifying": False,
        "verify_done": 0, "verify_total": 0, "warning": "",
        "best_speed": 0, "dir": "", "fullpath": "", "created": 0,
        "kind": "http", "mirrors": 0, "mirror_health": [],
        "adaptive": None, "retry_after_until": 0,
    }

    def poison(route):
        resp = route.fetch()
        data = resp.json()
        for j in data.get("jobs", [])[:1]:
            j["filename"] = '<img src=x onerror="window.__xss=1">'
            j["error"] = '<svg onload="window.__xss=2"></svg>'
            j["state"] = "error"
            j["url"] = 'javascript:window.__xss=3'
        try:
            route.fulfill(response=resp, body=json.dumps(data))
        except Exception:   # unroute ile yarisan istek: zaten islendi
            pass

    def poison_events(route):
        frame = {"ok": True, "jobs": [poison_job], "total_speed": 0,
                 "active": 0, "rev": 1, "full": True, "removed": [],
                 "total_jobs": 1, "turbo": False, "download_dir": ""}
        try:
            route.fulfill(status=200,
                          headers={"Content-Type": "text/event-stream",
                                   "Cache-Control": "no-store"},
                          body="event: stats\ndata: "
                               + json.dumps(frame) + "\n\n")
        except Exception:   # unroute ile yarisan istek: zaten islendi
            pass

    page.route("**/api/events", poison_events)
    page.route("**/api/stats", poison)
    page.reload()                       # akis yeniden kurulsun (route etkili)
    page.wait_for_selector("#jobList .row", timeout=15000)
    page.wait_for_timeout(1300)
    check(" XSS: script calismadi", page.evaluate("window.__xss") is None)
    check(" XSS: eleman uretilmedi", q("#jobList img, #jobList svg") == 0)
    check(" XSS: metin literal gorunur",
          '<img src=x onerror="window.__xss=1">' in page.inner_text("#jobList"))
    page.unroute("**/api/events", poison_events)
    page.unroute("**/api/stats", poison)
    page.reload()                       # gercek akis geri gelir
    page.wait_for_selector("#jobList .row:has-text('arsiv.zip')", timeout=15000)
    check(" XHR: istatistik duzeldi", "arsiv.zip" in page.inner_text("#jobList"))

    # ------------------------------- 10 000 is: yalniz gorunen pencere cizilir
    many = []
    for i in range(10000):
        many.append({
            "id": f"j{i}", "filename": f"dosya-{i}.bin",
            "url": f"http://example.invalid/{i}", "state": "queued",
            "category": "other", "size": 1000, "done": 0, "speed": 0,
            "eta": -1, "segments": 0, "segments_cfg": 1,
            "segments_effective": 1, "checksum": "", "merkle": "",
            "verified": [], "sources_dead": 0, "sched_wait": False,
            "window": "", "verifying": False, "verify_done": 0,
            "verify_total": 0, "warning": "", "best_speed": 0, "dir": "",
            "fullpath": "", "created": 0, "kind": "http", "mirrors": 0,
            "mirror_health": [], "adaptive": None, "retry_after_until": 0,
            "error": "", "error_code": ""})

    def many_events(route):
        frame = {"ok": True, "jobs": many, "total_speed": 0, "active": 0,
                 "rev": 7, "full": True, "removed": [],
                 "total_jobs": len(many), "turbo": False, "download_dir": ""}
        try:
            route.fulfill(status=200,
                          headers={"Content-Type": "text/event-stream",
                                   "Cache-Control": "no-store"},
                          body="event: stats\ndata: "
                               + json.dumps(frame) + "\n\n")
        except Exception:
            pass

    def many_stats(route):
        # akis koptugunda yoklama devreye girer: o da ayni listeyi versin
        frame = {"ok": True, "jobs": many, "total_speed": 0, "active": 0,
                 "rev": 7, "full": True, "removed": [],
                 "total_jobs": len(many), "turbo": False, "download_dir": ""}
        try:
            route.fulfill(body=json.dumps(frame),
                          headers={"Content-Type": "application/json"})
        except Exception:
            pass

    page.route("**/api/events", many_events)
    page.route("**/api/stats", many_stats)
    page.reload()
    page.wait_for_selector("#jobList .row", timeout=30000)
    page.wait_for_timeout(900)
    check("10k is: DOM satiri sinirli (pencere)", q("#jobList [role=row]") < 200,
          q("#jobList [role=row]"))
    page.evaluate("() => { document.querySelector('main').scrollTop = 1e9; }")
    page.wait_for_timeout(900)
    check("10k is: kaydirinca son satirlar cizilir",
          q("#jobList .row") > 0
          and "dosya-9999.bin" in page.inner_text("#jobList"))
    check("10k is: dolgu kaydirma boyunu korur", page.evaluate(
        "() => document.querySelector('#rowPadTop').offsetHeight > 0"))
    page.unroute("**/api/events", many_events)
    page.unroute("**/api/stats", many_stats)
    page.reload()
    page.wait_for_selector("#jobList .row:has-text('arsiv.zip')", timeout=15000)

    # ----------------------------------------------------- odak korunur
    btn = page.locator("#jobList .row", has_text="arsiv.zip").locator('[data-act="restart"]')
    btn.focus()
    page.wait_for_timeout(1500)          # >= 2 tarama
    check(" klavye odagi taramalarda korunur", page.evaluate(
        "document.activeElement && document.activeElement.dataset.act") == "restart"
        and "arsiv.zip" in page.evaluate("document.activeElement.closest('.row').innerText"))

    # --------------------------------------------------------- sekmeler
    page.focus("#tab-all")
    page.keyboard.press("ArrowRight")
    check(" ok tusu: sonraki sekme secilir", q('#tab-video[aria-selected="true"]') == 1
          and page.evaluate("document.activeElement.id") == "tab-video")
    check(" sekme filtreler", q("#jobList .row") == 1
          and "ornek-video.mp4" in page.inner_text("#jobList"))
    check(" panel etiketi guncellendi",
          page.get_attribute("#jobPanel", "aria-labelledby") == "tab-video")
    page.keyboard.press("Home")
    check(" Home: ilk sekme", q('#tab-all[aria-selected="true"]') == 1)
    page.keyboard.press("End")
    check(" End: son sekme", page.evaluate(
        "document.querySelector('[role=tab][aria-selected=true]').dataset.cat") == "other")
    check(" bos durum gorunur/bos metin", page.inner_text("#empty") != "")
    page.click("#tab-all")
    check(" tiklama: tum liste", q("#jobList .row") == 5)

    # ------------------------------------------------------ canli duyuru
    api("/api/add", {"url": f"{SLOW}/dosya.iso", "segments": 6,
                     "mirrors": [f"{SRV}/yok-iso.iso"]})
    page.wait_for_selector("#jobList .row:has-text('dosya.iso')", timeout=8000)
    rowx = page.locator("#jobList .row", has_text="dosya.iso")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10 and "/sn" not in rowx.inner_text():
        page.wait_for_timeout(150)
    check(" hiz gosterilir", "/sn" in rowx.inner_text(), rowx.inner_text())
    check(" is basina mini grafik", rowx.locator("canvas.spark").is_visible()
          and rowx.locator("canvas.spark").get_attribute("aria-label"))
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10 and "kaynak erişilemez" not in rowx.inner_text() \
            and jobs().get("dosya.iso", {}).get("state") == "downloading":
        page.wait_for_timeout(150)
    check(" olmayan ayna kaynak notu", "kaynak erişilemez" in rowx.inner_text(),
          rowx.inner_text())
    page.screenshot(path=f"{OUT}/01b-indiriyor.png")
    t0 = time.monotonic()
    live = ""
    while time.monotonic() - t0 < 14:
        live = page.inner_text("#live")
        if "dosya.iso" in live:
            break
        page.wait_for_timeout(200)
    check(" bitti duyurusu (aria-live)", "dosya.iso" in live and "Tamam" in live, live)
    j = wait_job("dosya.iso", ("done", "error"), 30)
    check(" aynali is bitti", j and j["state"] == "done", j)

    # --------------------------------------------- ana makine profilleri
    # Gercek komut: yol varsa satirlar, yoksa panel durum metni gosterir.
    try:
        hh = api("/api/hosts", {})
        print("  INFO  ana makine komutu: sunucuda var,",
              hh.get("count"), "profil")
    except Exception as exc:      # HTTPError 404: HTTP yolu eklenmemis
        print(f"  INFO  ana makine komutu: sunucuda YOK ({exc})")
    page.click("#hostsBtn")
    page.wait_for_selector("#hostsDlg[open]")
    check(" ana makineler kapanisi acilir",
          page.evaluate("document.querySelector('#hostsDlg').open"))
    check(" kapanis basligi bagli",
          page.get_attribute("#hostsDlg", "aria-labelledby") == "hostsTitle")
    page.wait_for_timeout(700)
    sok = page.evaluate("""() => {
        const box = document.querySelector('#hostsList');
        return { rows: box.querySelectorAll('.hostrow').length,
                 empty: !document.querySelector('#hostsEmpty').hidden,
                 status: !document.querySelector('#hostsStatus').hidden }; }""")
    check(" ana makineler: satir ya da durum metni",
          sok["rows"] > 0 or sok["empty"] or sok["status"], sok)
    page.screenshot(path=f"{OUT}/03b-ana-makineler.png")

    # Satir cizimi/yerinde guncelleme: profiller burada ogrenilmedigi icin
    # (kisa isler governor olcumu uretmez) liste route ile beslenir.
    work = [
        {"host": "a.example", "links": 6, "throttled": False, "rtt_band": "low",
         "per_link_bps": 1048576, "samples": 4, "last_seen": 1700000000},
        {"host": "b.example:8080", "links": 3, "throttled": True,
         "rtt_band": "high", "per_link_bps": 262144, "samples": 9,
         "last_seen": 1700000100}]
    sent = []

    def fake_hosts(route):
        body = json.loads(route.request.post_data or "{}") \
            if route.request.method == "POST" else {}
        if body.get("forget"):
            sent.append(body["forget"])
            work[:] = [h for h in work if h["host"] != body["forget"]]
            reply = {"ok": True, "forgotten": body["forget"]}
        elif body.get("clear"):
            sent.append("clear")
            reply = {"ok": True, "removed": len(work)}
            work.clear()
        else:
            reply = {"ok": True, "count": len(work), "hosts": work}
        route.fulfill(body=json.dumps(reply),
                      headers={"Content-Type": "application/json"})

    page.route("**/api/hosts", fake_hosts)
    page.click("#hostsClose")
    page.click("#hostsBtn")                  # panel yeniden komutu cagirir
    page.wait_for_selector("#hostsList .hostrow", timeout=5000)
    check(" profil satirlari ana makineye gore cizilir",
          page.locator("#hostsList .hostrow").count() == 2)
    htxt = page.inner_text("#hostsList")
    check(" profil metni: ana makine, baglanti, rtt, hiz",
          "a.example" in htxt and "6 bağlantı" in htxt and "düşük" in htxt
          and "1.0 MB" in htxt and "b.example:8080" in htxt, htxt)
    check(" unut dugmesinin adi ana makineyi icerir", page.evaluate("""() => {
        const r = document.querySelector('#hostsList .hostrow');
        const b = r && r.querySelector('button');
        const host = r && (r.querySelector('.hrow-text').textContent
                           || '').trim().split(/\\s+/)[0];
        return !!b && !!host
          && (b.getAttribute('aria-label') || '').includes(host); }"""))
    page.evaluate("document.querySelector('#hostsList .hostrow')"
                  ".dataset.mark = 'ilk'")
    page.locator("#hostsList .hostrow").first.locator("button").focus()
    work[0]["samples"] = 42
    page.wait_for_timeout(2600)              # >= 1 panel tazelemesi (~2 sn)
    check(" satir yerinde guncellenir (odak korunur)", page.evaluate("""() => {
        const r = document.querySelector('#hostsList .hostrow');
        return r.dataset.mark === 'ilk'
          && r.querySelector('.hrow-text').textContent.includes('42 örnek')
          && document.activeElement.closest('.hostrow') === r; }"""),
          page.inner_text("#hostsList"))
    page.locator("#hostsList .hostrow").first.locator("button").click()
    page.wait_for_timeout(500)
    check(" unut: komut govdesi {forget}", sent[:1] == ["a.example"], sent)
    check(" unut bildirimi (role=status)",
          "a.example" in page.inner_text("#notice"))
    check(" unutulan satir listeden duser",
          page.locator("#hostsList .hostrow").count() == 1)
    page.once("dialog", lambda d: d.accept())    # "Tumunu unut" onayi
    page.click("#hostsForgetAll")
    page.wait_for_timeout(600)
    check(" tumunu unut: {clear} gonderilir", "clear" in sent, sent)
    check(" liste bosalinca bos durum gorunur",
          page.is_visible("#hostsEmpty")
          and page.locator("#hostsList .hostrow").count() == 0)
    page.unroute("**/api/hosts", fake_hosts)
    page.screenshot(path=f"{OUT}/03c-profiller.png")
    page.click("#hostsClose")
    check(" ana makineler kapanisi kapanir",
          not page.evaluate("document.querySelector('#hostsDlg').open"))
    check(" odak kapanis dugmesine doner",
          page.evaluate("document.activeElement.id") == "hostsBtn",
          page.evaluate("document.activeElement.id"))

    # -------------------------------------------------- ekleme kapanisi
    page.click("#addBtn")
    check(" ekleme kapanisi acilir", page.evaluate("document.querySelector('#addDlg').open"))
    check(" odak adres alaninda", page.evaluate("document.activeElement.id") == "addUrl")
    page.fill("#addUrl", "gopher://x/y")
    page.click("#addOk")
    check(" gecersiz adres hatasi", page.is_visible("#addError")
          and page.inner_text("#addError") == "Geçerli bir http(s):// ya da ftp(s):// adresi girin.")
    page.fill("#addUrl", f"{SRV}/sarki.mp3")
    page.fill("#addChk", "zzz")
    page.click("#addOk")
    check(" gecersiz saglama hatasi", page.inner_text("#addError").startswith("geçersiz sağlama"))
    page.fill("#addChk", "md5:" + "0" * 32)
    page.fill("#addMirrors", f"{SRV}/sarki.mp3\nnot-a-url")
    page.uncheck("#addNow")
    page.screenshot(path=f"{OUT}/02-ekleme.png")
    page.click("#addOk")
    page.wait_for_timeout(600)
    check(" kapanis kapanir", not page.evaluate("document.querySelector('#addDlg').open"))
    check(" is duraklatili olarak eklendi", jobs().get("sarki.mp3", {}).get("state") == "paused",
          jobs().get("sarki.mp3"))
    check(" odak geri doner", page.evaluate("document.activeElement.id") == "addBtn")

    # --------------------------------------------------- arsiv onizleme
    page.locator("#jobList .row", has_text="arsiv.zip").locator('[data-act="zip"]').click()
    page.wait_for_selector("#zipDlg[open]")
    check(" arsiv icerigi listelenir", page.locator("#zipList [role=listitem]").count() == 12
          and "arsiv.zip" in page.inner_text("#zipTitle"))
    page.screenshot(path=f"{OUT}/03-arsiv.png")
    page.click("#zipClose")

    # ------------------------------------------- limit <-> turbo uyumu
    check(" turbo baslangicta acik", "TURBO AÇIK" in page.inner_text("#turboBtn")
          and page.get_attribute("#turboBtn", "aria-pressed") == "true")
    page.select_option("#limitSel", "2097152")
    page.wait_for_timeout(900)
    st_ = api("/api/stats")
    check(" limit secince turbo kapanir", st_["turbo"] is False and st_["speed_limit"] == 2097152)
    check(" dugme/etiket uyumlu", "TURBO AÇIK" not in page.inner_text("#turboBtn")
          and page.get_attribute("#turboBtn", "aria-pressed") == "false")
    page.click("#turboBtn")
    page.wait_for_timeout(900)
    check(" turbo yeniden acilir", api("/api/stats")["turbo"] is True
          and page.input_value("#limitSel") == "0")
    api("/api/limit", {"value": "3MB"})
    page.wait_for_timeout(900)
    check(" ozel limit (CLI) secenekte gosterilir, birim tutarli",
          page.input_value("#limitSel") == str(3 * 1024 ** 2)
          and "3.0 MB/sn" in page.inner_text("#limitSel"), page.inner_text("#limitSel"))
    page.click("#turboBtn")            # limit koyarken turbo kapansin diye
    page.wait_for_timeout(600)

    # ------------------------------------------------------- ayarlar
    page.click("#settingsBtn")
    page.wait_for_selector("#setDlg[open]")
    check(" 6 kategori satiri", page.locator("#catSched .catrow").count() == 6)
    vrow = page.locator("#catSched .catrow[data-cat=video]")
    check(" mevcut pencere yuklendi", vrow.locator('[data-w=on]').is_checked()
          and vrow.locator('[data-w=start]').input_value() == st
          and vrow.locator('[data-w=stop]').input_value() == sp)
    mrow = page.locator("#catSched .catrow[data-cat=music]")
    check(" kapali kategori: zamanlar devre disi", mrow.locator('[data-w=start]').is_disabled())
    mrow.locator('[data-w=on]').check()
    check(" isaretleyince zamanlar acilir", mrow.locator('[data-w=start]').is_enabled())
    mrow.locator('[data-w=start]').fill("03:15")
    mrow.locator('[data-w=stop]').fill("04:45")
    page.fill("#setWatchDirs", f"{WATCH_DIRS[0]}\n  \n{WATCH_DIRS[1]}")
    page.select_option("#setWatchDone", "keep")
    page.uncheck("#setSidecar")
    page.screenshot(path=f"{OUT}/04-ayarlar.png", full_page=True)
    page.click("#setSave")
    page.wait_for_timeout(800)
    cfg = api("/api/config")["config"]
    cats = cfg["schedule"]["categories"]
    check(" kategori pencereleri korundu + yeni eklendi",
          cats.get("video") == {"start": st, "stop": sp}
          and cats.get("music") == {"start": "03:15", "stop": "04:45"}, cats)
    check(" izlenen klasorler / mod / yan dosya",
          cfg["watch_dirs"] == WATCH_DIRS
          and cfg["watch_done"] == "keep" and cfg["checksum_sidecar"] is False)
    check(" genel zamanlama kaybolmadi", cfg["schedule"]["enabled"] is False)
    check(" kaydedildi bildirimi", page.is_visible("#notice")
          and page.inner_text("#notice") == "kaydedildi ✓")
    api("/api/config", {"config": {"checksum_sidecar": True}})

    # ---------------------------------------------------------- dil
    page.click("#settingsBtn")
    page.wait_for_selector("#setDlg[open]")
    page.select_option("#setLang", "en")
    page.click("#setSave")
    page.wait_for_timeout(1000)
    check(" dil: <html lang=en>", page.evaluate("document.documentElement.lang") == "en")
    check(" dil: sekmeler yeniden kuruldu", page.inner_text("#tab-all") == "All"
          and "verified: sha256" in page.inner_text("#jobList"))
    page.screenshot(path=f"{OUT}/05-liste-en.png")
    page.click("#settingsBtn")
    page.wait_for_selector("#setDlg[open]")
    page.select_option("#setLang", "ru")
    page.click("#setSave")
    page.wait_for_timeout(1000)
    check(" dil: ru", page.evaluate("document.documentElement.lang") == "ru"
          and "Все" in page.inner_text("#tab-all"))
    page.click("#settingsBtn")
    page.wait_for_selector("#setDlg[open]")
    page.select_option("#setLang", "tr")
    page.click("#setSave")
    page.wait_for_timeout(800)

    # --------------------------------------------------------- geçmiş
    page.wait_for_selector("#histList .histrow", timeout=10000)
    htxt = page.inner_text("#histList")
    check("gecmis: biten isler listelenir", "arsiv.zip" in htxt and "yok-iso.iso" in htxt)
    check("gecmis: istatistik ozeti", page.evaluate(
        "() => /[0-9]/.test(document.querySelector('#histStats').textContent)"))
    check("gecmis: yeniden indirme dugmesi odaklanabilir", page.evaluate("""() => {
        const b = document.querySelector('#histList .act');
        if (!b) return false;
        b.focus();
        return document.activeElement === b
          && (b.getAttribute('aria-label') || '').length > 0; }"""))
    page.fill("#histSearch", "arsiv")
    page.wait_for_timeout(700)
    check("gecmis: arama suzgeci", "arsiv.zip" in page.inner_text("#histList")
          and "yok-iso.iso" not in page.inner_text("#histList"))
    page.fill("#histSearch", "zzz-yok")
    page.wait_for_timeout(700)
    check("gecmis: sonuc yok mesaji", page.is_visible("#histEmpty")
          and "Eşleşen kayıt yok" in page.inner_text("#histEmpty"))
    page.fill("#histSearch", "")
    page.wait_for_timeout(700)

    # ------------------------------- fark kareleri (lane A sozlesmesi)
    # Sunucu rev/full/removed dondurdugunde arayuz yalniz degisenleri uygular:
    # silinen satir duser, degisen satir YERINDE guncellenir, digeri kalir.
    def fake_job(jid, name):
        return {
            "id": jid, "filename": name, "url": f"http://example.invalid/{jid}",
            "state": "queued", "category": "other", "size": 100, "done": 0,
            "speed": 0, "eta": -1, "segments": 0, "segments_cfg": 1,
            "segments_effective": 1, "checksum": "", "merkle": "",
            "verified": [], "sources_dead": 0, "sched_wait": False,
            "window": "", "verifying": False, "verify_done": 0,
            "verify_total": 0, "warning": "", "best_speed": 0, "dir": "",
            "fullpath": "", "created": 0, "kind": "http", "mirrors": 0,
            "mirror_health": [], "adaptive": None, "retry_after_until": 0,
            "error": "", "error_code": "",
        }

    three = [fake_job("d1", "delta-eski.bin"), fake_job("d2", "delta-silinir.bin"),
             fake_job("d3", "delta-kalir.bin")]
    # d2 guncellenir (ad degisir), d1 silinir, d3 dokunulmaz
    two = [fake_job("d2", "delta-yeni.bin"), three[2]]
    stats_calls = {"n": 0}

    def delta_stats(route):
        stats_calls["n"] += 1
        is_full = stats_calls["n"] <= 1
        body = {"ok": True, "jobs": three if is_full else two,
                "rev": 10 if is_full else 11, "full": is_full,
                "removed": [], "total_jobs": len(three if is_full else two),
                "turbo": False, "download_dir": ""}
        try:
            route.fulfill(body=json.dumps(body),
                          headers={"Content-Type": "application/json"})
        except Exception:
            pass

    def delta_events(route):
        full = {"ok": True, "jobs": three, "rev": 10, "full": True,
                "removed": [], "total_jobs": 3, "turbo": False,
                "download_dir": ""}
        delta = {"ok": True, "jobs": [two[0]], "rev": 11, "full": False,
                 "removed": ["d1"], "total_jobs": 2, "turbo": False,
                 "download_dir": ""}
        try:
            route.fulfill(status=200,
                          headers={"Content-Type": "text/event-stream",
                                   "Cache-Control": "no-store"},
                          body="".join("event: stats\ndata: " + json.dumps(f)
                                       + "\n\n" for f in (full, delta)))
        except Exception:
            pass

    page.route("**/api/events", delta_events)
    page.route("**/api/stats", delta_stats)
    page.reload()
    page.wait_for_selector("#jobList .row", timeout=15000)
    page.wait_for_timeout(900)
    dtext = page.inner_text("#jobList")
    check("fark karesi: silinen satir duser", "delta-eski.bin" not in dtext, dtext)
    check("fark karesi: degisen satir guncellenir", "delta-yeni.bin" in dtext, dtext)
    check("fark karesi: degismeyen satir kalir", "delta-kalir.bin" in dtext, dtext)
    check("fark karesi: satir sayisi", q("#jobList .row") == 2,
          q("#jobList .row"))
    page.unroute("**/api/events", delta_events)
    page.unroute("**/api/stats", delta_stats)
    page.reload()
    page.wait_for_selector("#jobList .row:has-text('arsiv.zip')", timeout=15000)

    # ------------- sayim tutmazsa CANLI akis tam kareyle yeniden kurulur
    # Istemci liste eksik gorurse rev=0 yalniz yoklamayi duzeltir; akis
    # sunucu tarafinda kendi rev'ini tutar. Bu yuzden akis KAPATILIR,
    # tam kare yoklanir ve akis yeniden acilir (resync).
    def gap_events(route):
        full = {"ok": True, "jobs": three, "rev": 41, "full": True,
                "removed": [], "total_jobs": 3, "turbo": False,
                "download_dir": ""}
        # 42. rev: kare bos ama sunucu 5 is diyor -> liste eksik kaldi
        gap = {"ok": True, "jobs": [], "rev": 42, "full": False,
               "removed": [], "total_jobs": 5, "turbo": False,
               "download_dir": ""}
        try:
            route.fulfill(status=200,
                          headers={"Content-Type": "text/event-stream",
                                   "Cache-Control": "no-store"},
                          body="".join("event: stats\ndata: " + json.dumps(f)
                                       + "\n\n" for f in (full, gap)))
        except Exception:
            pass

    def seen(prefix):
        return sum(1 for u in reqs if prefix in u)

    reqs.clear()
    page.route("**/api/events", gap_events)
    page.reload()                    # ilk yuklemede akis + yoklama esitlenir
    page.wait_for_selector("#jobList .row", timeout=15000)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 8 and not (seen("/api/events") >= 2
                                             and seen("/api/stats") >= 2):
        page.wait_for_timeout(100)
    check("resync: akis yeniden acilir", seen("/api/events") >= 2,
          (seen("/api/events"), seen("/api/stats")))
    check("resync: tam kare yoklamasi yapilir", seen("/api/stats") >= 2,
          (seen("/api/events"), seen("/api/stats")))
    page.unroute("**/api/events", gap_events)
    page.reload()
    page.wait_for_selector("#jobList .row:has-text('arsiv.zip')", timeout=15000)

    # -------------------------------------------------- azaltılmış hareket
    ctx2 = browser.new_context(viewport={"width": 1280, "height": 800},
                               reduced_motion="reduce", locale="tr-TR")
    page2 = ctx2.new_page()
    perr = []
    page2.on("pageerror", lambda e: perr.append(str(e)))
    page2.goto(web_session())
    page2.wait_for_selector("#jobList .row")
    check(" prefers-reduced-motion:Transition yok", page2.evaluate(
        "getComputedStyle(document.querySelector('.bar i')).transitionDuration") == "0s")
    check(" reduced-motion sayfasinda hata yok", not perr, perr)
    page2.set_viewport_size({"width": 700, "height": 700})
    page2.wait_for_timeout(400)
    page2.screenshot(path=f"{OUT}/06-dar-ekran.png")

    # ------------------------------------------- erisilebilir adlar (DOM)
    unnamed = page.evaluate("""() => [...document.querySelectorAll(
        'header button, #tabs button, dialog[open] button, #jobList .act:not([hidden])')]
        .filter(b => !(b.innerText.trim() || b.getAttribute('aria-label') ||
                       b.getAttribute('title'))).map(b => b.id || b.className)""")
    check(" gorunur dugmelerin erisilebilir adi var", unnamed == [], unnamed)
    check(" giris alanlarinin adi var (label / aria-label)",
          page.evaluate("""() => [...document.querySelectorAll(
              'dialog input, dialog select, dialog textarea')]
              .filter(i => !(i.labels && i.labels.length) && !i.getAttribute('aria-label'))
              .map(i => i.id)""") == [])

    browser.close()

check(" tarayici konsolunda hata yok", not errors, errors)
print()
print("WEB LIVE:", "OK" if not fails else f"FAIL ({len(fails)}): {fails}")
sys.exit(1 if fails else 0)
