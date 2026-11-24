"""metalink icinde Merkle bildirimi (ROADMAP 1.7): ayristirma, genisleme
ve kesin kaynak (explicit provenance) davranisi.

- `<metaurl mediatype="application/x-ferdim-merkle">` parca bildirimini,
  `<hash type="ferdim-merkle-root">` beklenen koku bildirir.
- Kullanicinin verdigi belgedeki bildirim `--merkle` gibi KESIN sayilir:
  dogrulama uyusmazligi isi hatayla bitirir (yan dosya gibi uyari degil).
- Belgedeki kok indirilen bildirimin kokuyla ayni olmak zorundadir.
"""
from __future__ import annotations

import hashlib
import sys
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import api, cdc, metalink
from ferdim.engine.job import JobState
from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from tests.helpers import tmp_root

PAYLOAD = hashlib.shake_256(b"ferdim-metalink-merkle").digest(300_000)
CHUNK_SHA = hashlib.sha256(PAYLOAD).hexdigest()
MAN = cdc.Manifest.of([(0, len(PAYLOAD), CHUNK_SHA)])
BAD_SHA = "0" * 64
BAD_MAN = cdc.Manifest.of([(0, len(PAYLOAD), BAD_SHA)])
OTHER_ROOT = cdc.merkle_root([hashlib.sha256(b"baska").hexdigest()])


def wait_state(job, states, timeout=90):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.state in states:
            return True
        time.sleep(0.05)
    return False


def document(base: str, *, merkle: str = "", root: str = "",
             payload: str = "/payload.bin", name: str = "paket.bin") -> str:
    """Ornek metalink (v3) belgesi: istege bagli Merkle alanlari ile."""
    extra = ""
    if merkle:
        extra += (f'\n   <metaurl mediatype="application/x-ferdim-merkle">'
                  f'{base}{merkle}</metaurl>')
    if root:
        extra += f'\n   <hash type="ferdim-merkle-root">{root}</hash>'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<metalink version="3.0" xmlns="urn:ietf:params:xml:ns:metalink">
 <file name="{name}">
  <size>{len(PAYLOAD)}</size>
  <hash type="sha-256">{CHUNK_SHA}</hash>{extra}
  <url preference="10">{base}{payload}</url>
  <url preference="20">{base}{payload}?mirror=1</url>
 </file>
</metalink>
"""


def item(urls, merkle: str = "", root: str = "",
         name: str = "paket.bin") -> dict:
    """metalink.parse ciktisinin en az alan kumesini tasiyan kayit."""
    return {"filename": name, "size": 1, "urls": list(urls),
            "sha256": CHUNK_SHA, "md5": "",
            "hashes": {"sha256": CHUNK_SHA},
            "merkle": merkle, "merkle_root": root}


class MLServer(BaseHTTPRequestHandler):
    """metalink belgesi + Range destekli yuk + parca bildirimi sunar."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        srv = self.server
        path = urllib.parse.urlsplit(self.path).path
        with srv.lock:
            srv.seen.append(path)
        if path in srv.docs:
            self._send(200, srv.docs[path].encode("utf-8"),
                       "application/metalink4+xml")
            return
        if path == "/manifest.ferdim-merkle":
            self._send(200, MAN.dumps().encode(), "application/json")
            return
        if path == "/bozuk.ferdim-merkle":
            self._send(200, BAD_MAN.dumps().encode(), "application/json")
            return
        if path == "/payload.bin":
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, b = rng[6:].split("-", 1)
                a = int(a)
                b = min(int(b) if b else len(PAYLOAD) - 1, len(PAYLOAD) - 1)
                body = PAYLOAD[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range",
                                 f"bytes {a}-{b}/{len(PAYLOAD)}")
            else:
                body = PAYLOAD
                self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", '"ml"')
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(404, b"yok")


def start_ml_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MLServer)
    srv.daemon_threads = True
    srv.lock = threading.Lock()
    srv.seen = []
    srv.docs = {}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class ParseTests(unittest.TestCase):
    def test_01_metaurl_and_root(self):
        root = "AB" * 32                      # buyuk harf: kucultulur
        xml = document("https://x.test", merkle="/m.ferdim-merkle", root=root)
        it = metalink.parse(xml)[0]
        self.assertEqual(it["merkle"], "https://x.test/m.ferdim-merkle")
        self.assertEqual(it["merkle_root"], root.lower())
        self.assertEqual(it["sha256"], CHUNK_SHA)
        self.assertEqual(it["urls"],
                         ["https://x.test/payload.bin",
                          "https://x.test/payload.bin?mirror=1"])

    def test_02_absent_fields_are_empty(self):
        it = metalink.parse(document("https://x.test"))[0]
        self.assertEqual(it["merkle"], "")
        self.assertEqual(it["merkle_root"], "")

    def test_03_unknown_media_and_bad_root_are_ignored(self):
        xml = f"""<?xml version="1.0"?>
<metalink version="3.0" xmlns="urn:ietf:params:xml:ns:metalink">
 <file name="a.bin"><size>1</size>
  <metaurl mediatype="application/x-bittorrent">https://x.test/a.torrent</metaurl>
  <metaurl mediatype="application/x-ferdim-merkle">ftp://x.test/m</metaurl>
  <hash type="ferdim-merkle-root">kisa</hash>
  <hash type="ferdim-merkle-root">{"c" * 64}</hash>
  <url>https://x.test/a.bin</url></file>
</metalink>"""
        it = metalink.parse(xml)[0]
        self.assertEqual(it["merkle"], "", "baska medya/şema kabul edilmemeli")
        self.assertEqual(it["merkle_root"], "c" * 64, "gecersiz kok yok sayilir")
        self.assertNotIn("ferdim-merkle-root", it["hashes"])

    def test_04_meta4_namespace_and_media_parameters(self):
        xml = f"""<?xml version="1.0"?>
<metalink xmlns="urn:ietf:params:xml:ns:metalink">
 <file name="a.bin"><size>1</size>
  <metaurl mediatype="Application/X-FerDim-Merkle; charset=utf-8">
   https://x.test/a.bin.ferdim-merkle</metaurl>
  <hash type="ferdim-merkle-root">{"d" * 64}</hash>
  <url>https://x.test/a.bin</url></file>
</metalink>"""
        it = metalink.parse(xml)[0]
        self.assertEqual(it["merkle"], "https://x.test/a.bin.ferdim-merkle")
        self.assertEqual(it["merkle_root"], "d" * 64)


class ExpansionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_ml_server()
        cls.b = cls.base

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.env = AppEnv(root=tmp_root(self, "ferdim-ml-merkle-"))
        self.cfg = self.env.load_config()
        self.cfg["max_concurrent"] = 2
        self.qm = QueueManager(self.env, self.cfg)
        self.addCleanup(self.qm.shutdown)
        self.ctx = {"qm": self.qm, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}

    def _doc(self, path: str, text: str) -> str:
        self.srv.docs[path] = text
        return f"{self.b}{path}"

    def _add(self, **params):
        return api.handle(self.ctx, "add", {"paused": True, **params})

    # ------------------------------------------------------------- genisleme
    def test_10_metaurl_becomes_an_explicit_merkle(self):
        url = self._doc("/doc.meta4",
                        document(self.b, merkle="/manifest.ferdim-merkle"))
        r = self._add(url=url)
        self.assertTrue(r.get("ok"), r)
        job = self.qm.get(r["id"])
        self.assertEqual(job.spec.merkle,
                         f"{self.b}/manifest.ferdim-merkle")
        self.assertEqual(job.spec.mirrors, [f"{self.b}/payload.bin?mirror=1"])
        self.assertEqual(job.spec.sha256, CHUNK_SHA)
        self.assertEqual(job.spec.filename, "paket.bin")

    def test_11_matching_root_is_accepted(self):
        url = self._doc("/doc-root.meta4",
                        document(self.b, merkle="/manifest.ferdim-merkle",
                                 root=MAN.root))
        r = self._add(url=url)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(self.qm.get(r["id"]).spec.merkle,
                         f"{self.b}/manifest.ferdim-merkle")

    def test_12_declared_root_mismatch_blocks_the_add(self):
        url = self._doc("/doc-bad.meta4",
                        document(self.b, merkle="/manifest.ferdim-merkle",
                                 root=OTHER_ROOT))
        r = self._add(url=url)
        self.assertFalse(r.get("ok"), r)
        self.assertIn("merkle", r.get("error", ""))
        self.assertEqual(len(self.qm.jobs), 0, "uyusmayan is kuyruga girmemeli")

    def test_13_unreadable_manifest_still_queues(self):
        """Bildirim simdi okunamiyorsa kuyruga girer; motor merkle_manifest
        koduyla bildirir (sessizce dogrulamasiz indirmek yanlis olurdu)."""
        url = self._doc("/doc-yok.meta4",
                        document(self.b, merkle="/yok.ferdim-merkle",
                                 root=MAN.root))
        r = self._add(url=url)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(self.qm.get(r["id"]).spec.merkle,
                         f"{self.b}/yok.ferdim-merkle")

    def test_14_root_without_a_manifest_costs_no_request(self):
        """Kok tek basina anlamsiz: bildirim yoksa ek istek yapilmaz."""
        url = self._doc("/doc-tek.meta4", document(self.b, root=MAN.root))
        with self.srv.lock:
            before = list(self.srv.seen)
        r = self._add(url=url)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(self.qm.get(r["id"]).spec.merkle, "")
        with self.srv.lock:
            new = [p for p in self.srv.seen if p not in before]
        self.assertNotIn("/manifest.ferdim-merkle", new)

    def test_15_no_manifest_means_no_extra_request(self):
        url = self._doc("/doc-duz.meta4",
                        document(self.b, merkle="/manifest.ferdim-merkle"))
        with self.srv.lock:
            before = list(self.srv.seen)
        r = self._add(url=url)
        self.assertTrue(r.get("ok"), r)
        with self.srv.lock:
            new = [p for p in self.srv.seen if p not in before]
        self.assertNotIn("/manifest.ferdim-merkle", new,
                         "kok yoksa bildirim onden indirilmemeli")

    def test_16_user_merkle_is_not_forwarded_to_each_file(self):
        """Bir belge cok dosya icerebilir: kullanicinin tek bildirimini her
        dosyaya dagitmak yanlis olurdu (belgedeki kayit gecerlidir)."""
        url = self._doc("/doc-kullanici.meta4", document(self.b))
        r = self._add(url=url, merkle="/tmp/baska.ferdim-merkle")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(self.qm.get(r["id"]).spec.merkle, "")

    def test_17_local_metalink_file(self):
        root = tmp_root(self, "ferdim-ml-local-")
        p = root / "paket.metalink"
        p.write_text(document(self.b, merkle="/manifest.ferdim-merkle",
                              root=MAN.root), "utf-8")
        r = api.handle(self.ctx, "add-file", {"path": str(p)})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(self.qm.get(r["ids"][0]).spec.merkle,
                         f"{self.b}/manifest.ferdim-merkle")

    def test_18_local_metalink_root_mismatch_blocks(self):
        root = tmp_root(self, "ferdim-ml-local2-")
        p = root / "bozuk.metalink"
        p.write_text(document(self.b, merkle="/manifest.ferdim-merkle",
                              root=OTHER_ROOT), "utf-8")
        r = api.handle(self.ctx, "add-file", {"path": str(p)})
        self.assertFalse(r.get("ok"), r)
        self.assertIn("merkle", r.get("error", ""))
        self.assertEqual(len(self.qm.jobs), 0)

    # ------------------------------------- kimlik bilgisi kapsami (guvenlik)
    def _cred_base(self, doc: str = "https://docs.test/doc.meta4") -> dict:
        return {"url": doc, "paused": True,
                "cookies": "sid=1", "user": "kullanici",
                "password": "parola", "headers": ["X-Api-Key: gizli"],
                "referer": "https://docs.test/sayfa",
                "ua": "FerDimTest/1"}

    def _add_items(self, items, base):
        r = api._add_metalink_items(self.ctx, items, base)
        self.assertTrue(r.get("ok"), r)
        return self.qm.get(r["ids"][0])

    def test_30_credentials_stay_on_the_document_origin(self):
        """Cerez/Basic/ozel baslik yalniz belgenin https kaynaginda kalir;
        ayni kaynakli olmayan ayna ve http hedef kimlik almaz."""
        job = self._add_items([item([
            "https://docs.test/a.bin",
            "https://docs.test/mirror/a.bin",
            "https://baska.test/a.bin",
            "http://docs.test/a.bin"])], self._cred_base())
        spec = job.spec
        self.assertEqual(spec.cookies, "sid=1")
        self.assertEqual(spec.user, "kullanici")
        self.assertEqual(spec.password, "parola")
        self.assertEqual(spec.headers, {"X-Api-Key": "gizli"})
        self.assertEqual(spec.referer, "https://docs.test/sayfa")
        self.assertEqual(spec.user_agent, "FerDimTest/1")
        self.assertEqual(spec.mirrors, ["https://docs.test/mirror/a.bin"])

    def test_30b_public_document_keeps_all_mirrors(self):
        """Kimlik bilgisi yoksa kaynak kapsami kisitlamaz: herkese acik
        metalinklerin cok kaynakli aynalari korunur."""
        base = {"url": "https://docs.test/doc.meta4", "paused": True,
                "ua": "FerDimTest/1"}
        job = self._add_items([item([
            "https://docs.test/a.bin", "https://baska.test/a.bin",
            "http://ucuncu.test/a.bin"])], base)
        self.assertEqual(job.spec.mirrors,
                         ["https://baska.test/a.bin",
                          "http://ucuncu.test/a.bin"])

    def test_31_cross_origin_primary_drops_credentials(self):
        job = self._add_items([item([
            "https://baska.test/a.bin",
            "https://docs.test/mirror/a.bin",
            "https://ucuncu.test/a.bin"])], self._cred_base())
        spec = job.spec
        for attr in ("cookies", "user", "password", "referer"):
            self.assertEqual(getattr(spec, attr), "", attr)
        self.assertEqual(spec.headers, {})
        self.assertEqual(spec.user_agent, "FerDimTest/1")
        self.assertEqual(spec.mirrors,
                         ["https://docs.test/mirror/a.bin",
                          "https://ucuncu.test/a.bin"],
                         "kimlik birakilinca aynalar elenmez")

    def test_32_http_origin_never_carries_credentials(self):
        job = self._add_items(
            [item(["http://docs.test/a.bin"])],
            self._cred_base("http://docs.test/doc.meta4"))
        self.assertEqual(job.spec.url, "http://docs.test/a.bin")
        self.assertEqual(job.spec.cookies, "")
        self.assertEqual(job.spec.user, "")
        self.assertEqual(job.spec.headers, {})

    def test_33_cross_origin_merkle_drops_credentials(self):
        """Bildirim baska kaynaktan geliyorsa dogrulama kaybolmasin diye
        kimlik bilgisi tumden birakilir (parola yabanci sunucuya gitmez)."""
        job = self._add_items([item(
            ["https://docs.test/a.bin", "https://baska.test/a.bin"],
            merkle="https://cdn.test/a.ferdim-merkle")], self._cred_base())
        spec = job.spec
        self.assertEqual(spec.merkle, "https://cdn.test/a.ferdim-merkle")
        self.assertEqual(spec.cookies, "")
        self.assertEqual(spec.user, "")
        self.assertEqual(spec.headers, {})
        self.assertEqual(spec.mirrors, ["https://baska.test/a.bin"])

    def test_34_preflight_fetch_is_scoped_too(self):
        """Onden kok dogrulama istegi de ayni kapsamla cikmali."""
        for merkle, want in (
                ("https://docs.test/m.ferdim-merkle", "sid=1"),
                ("https://cdn.test/m.ferdim-merkle", "")):
            seen = {}

            def fake_fetch(url, opt, **kw):
                seen.update(url=url, cookies=opt.cookies,
                            user=opt.user, headers=dict(opt.headers))
                return MAN.dumps()

            it = item(["https://docs.test/a.bin"], merkle=merkle,
                      root=MAN.root)
            with mock.patch("ferdim.engine.httpcli.fetch_small",
                            side_effect=fake_fetch):
                err = api._metalink_root_guard(
                    [it], self._cred_base(), self.cfg,
                    "https://docs.test/doc.meta4")
            self.assertEqual(err, "", err)
            self.assertEqual(seen["url"], merkle)
            self.assertEqual(seen["cookies"], want)
            self.assertEqual(seen["user"], "kullanici" if want else "")
            self.assertEqual(seen["headers"].get("X-Api-Key"),
                             "gizli" if want else None)
            self.assertEqual(seen["headers"].get("Referer"),
                             "https://docs.test/sayfa" if want else None)

    def test_35_preflight_total_deadline(self):
        """64 yavas bildirim API is parcacigini kilitlememeli: toplam sure
        dolunca kalanlar onden denenmeden kuyruga girer."""
        calls = []

        def slow(*a, **k):
            calls.append(1)
            time.sleep(0.1)
            raise OSError("ag yok")

        items = [item(["https://docs.test/a.bin"],
                      merkle="https://docs.test/m.ferdim-merkle",
                      root=MAN.root) for _ in range(api.MAX_METALINK_FILES)]
        old = api.METALINK_GUARD_SECS
        api.METALINK_GUARD_SECS = 0.3
        try:
            t0 = time.monotonic()
            with mock.patch("ferdim.engine.httpcli.fetch_small",
                            side_effect=slow):
                err = api._metalink_root_guard(
                    items, self._cred_base(), self.cfg,
                    "https://docs.test/doc.meta4")
            elapsed = time.monotonic() - t0
        finally:
            api.METALINK_GUARD_SECS = old
        self.assertEqual(err, "")
        self.assertLess(elapsed, 1.5, "toplam sure siniri asildi")
        self.assertLess(len(calls), api.MAX_METALINK_FILES,
                        "sure dolunca kalan onden denenmemeli")

    # ------------------------------------------------- kesin kaynak (motor)
    def test_20_bad_manifest_from_metalink_is_an_error(self):
        """Belgeden gelen bildirim --merkle gibidir: uyusmazlik HATA.

        Ayni uyusmazlik yan dosyadan (kendiliginden bulunan) gelseydi
        yalnizca uyari olurdu (bkz. test_cdc)."""
        url = self._doc("/doc-bozuk.meta4",
                        document(self.b, merkle="/bozuk.ferdim-merkle",
                                 root=BAD_MAN.root))
        r = self._add(url=url)
        self.assertTrue(r.get("ok"), r)
        job = self.qm.get(r["id"])
        self.assertEqual(job.spec.merkle, f"{self.b}/bozuk.ferdim-merkle")
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                        f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.ERROR, job.error)
        self.assertEqual(job.error_code, "merkle_mismatch")
        self.assertEqual(job.snapshot()["merkle"], "mismatch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
