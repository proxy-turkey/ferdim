"""1.13 sunucu katmani: sinirli kuyruklu yayin merkezi + SSE + web arayuzu.

Kapsanan sozlesmeler:
- `Hub.notify()` yalnizca bayrak kaldirir: yavas bir istemci is
  parcaciklarini ya da diger istemcileri BLOKLAMAZ.
- Istemci kuyrugu tek yuvalidir (en yeni kare eskisini ezer).
- Yazma butcesi asan istemci kapatilir, itici parcacik takilmaz.
- Istemci yokken kalp atisi istatistik HESAPLAMAZ (bosta CPU).
- Soket izleyicisi tel bicimi degismedi: `{"event":"stats", ...}\\n`.
- /api/events token ister, ilk kare tamdir, degisiklikte fark karesi
  gonderir ve sunucu kapaninca akis biter.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ferdim import api  # noqa: E402
from ferdim.i18n import LANGS, S  # noqa: E402
from ferdim.paths import AppEnv  # noqa: E402
from ferdim.server import web  # noqa: E402
from ferdim.server.hub import Client, Hub  # noqa: E402
from ferdim.server.sock import SockServer  # noqa: E402
from tests.helpers import tmp_root  # noqa: E402

TOKEN = "p113token"


# ------------------------------------------------------------------ yardimci
class FakeStats:
    """rev ureten sahte istatistik: `since` verilirse yalniz degisen isler.

    Gercek kuyruk (api.handle -> qm.stats) ayni sozlesmeyi saglar; burada
    cagri sayisi ve parametreler gozlenebilir olsun diye sahte kullanilir.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.rev = 1
        self.jobs = {}          # kimlik -> (rev, snapshot)
        self.removed = []       # (rev, kimlik)
        self.calls = []         # [(params)]
        self.top = {}           # rev ARTIRMAYAN ust alanlar (turbo gibi)
        self.delay = 0.0        # yavas istatistik benzetimi

    def upsert(self, job_id: str, **fields) -> None:
        with self.lock:
            self.rev += 1
            self.jobs[job_id] = (self.rev, {"id": job_id, **fields})

    def drop(self, job_id: str) -> None:
        with self.lock:
            self.rev += 1
            self.jobs.pop(job_id, None)
            self.removed.append((self.rev, job_id))

    def call_count(self) -> int:
        with self.lock:
            return len(self.calls)

    def __call__(self, params):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.calls.append(dict(params))
            since = params.get("since")
            out = {"ok": True, "rev": self.rev, "total_jobs": len(self.jobs),
                   **self.top}
            if since is None:
                out["full"] = True
                out["jobs"] = [snap for _r, snap in
                               sorted(self.jobs.values(), key=lambda x: x[0])]
                out["removed"] = []
            else:
                out["full"] = False
                out["jobs"] = [snap for r, snap in
                               sorted(self.jobs.values(), key=lambda x: x[0])
                               if r > since]
                out["removed"] = [i for r, i in self.removed if r > since]
            return out


def wait_for(pred, timeout=5.0, step=0.01) -> bool:
    """Olay/kosul beklemesi (sabit sleep yok, sinirli sure)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


class LineReader:
    """Soketten satir/blok okuyan kucuk yardimci (sinirli zaman asimi)."""

    def __init__(self, sock: socket.socket, sep=b"\n"):
        self.sock = sock
        self.sep = sep
        self.buf = b""

    def block(self, timeout=5.0) -> bytes:
        end = time.monotonic() + timeout
        while self.sep not in self.buf:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("blok gelmedi")
            self.sock.settimeout(left)
            part = self.sock.recv(65536)
            if not part:
                raise EOFError("akis kapandi")
            self.buf += part
        head, _, self.buf = self.buf.partition(self.sep)
        return head

    def eof(self, timeout=5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.sock.settimeout(max(0.01, end - time.monotonic()))
            try:
                part = self.sock.recv(65536)
            except TimeoutError:
                return False
            except OSError:
                return True
            if not part:
                return True
        return False


class FakeQM:
    """Yalnizca dinleyici defteri: soket sunucusu bunu kullanir."""

    def __init__(self):
        self.listeners = []
        self.lock = threading.Lock()

    def add_listener(self, fn):
        with self.lock:
            self.listeners.append(fn)

    def remove_listener(self, fn):
        with self.lock:
            if fn in self.listeners:
                self.listeners.remove(fn)


# ------------------------------------------------------- istemci kuyrugu
class ClientQueueTests(unittest.TestCase):
    def test_01_coalescing_keeps_latest_frame(self):
        a, b = socket.socketpair()
        try:
            c = Client(a)             # yazici baslatilmaz: kuyruk elle denetlenir
            self.assertTrue(c.offer(b"birinci"))
            self.assertTrue(c.offer(b"ikinci"))
            self.assertEqual(c.take(), b"ikinci")   # eski kare dustu
            self.assertEqual(c.take(), b"")
        finally:
            a.close()
            b.close()

    def test_02_stuck_client_never_blocks_and_gets_closed(self):
        """Okuyucu hic okumaz: offer() beklemez, butce dolunca baglanti kapanir."""
        a, b = socket.socketpair()
        c = Client(a, send_timeout=0.05, frame_timeout=0.2)
        c.start()
        big = b"x" * (8 << 20)        # soket tamponundan buyuk kare
        try:
            t0 = time.monotonic()
            for _ in range(20):
                self.assertTrue(c.offer(big))
            # 20 teklif toplamda milisaniyeler surer (yazma baska parcacikta)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertTrue(c.closed.wait(5.0), "yazma butcesi asilinca kapanmali")
        finally:
            c.close()
            a.close()
            b.close()

    def test_03_close_wakes_waiters(self):
        a, b = socket.socketpair()
        try:
            c = Client(a)
            done = threading.Event()

            def waiter():
                c.wait_closed()
                done.set()

            threading.Thread(target=waiter, daemon=True).start()
            c.close()
            self.assertTrue(done.wait(2.0))
            self.assertFalse(c.offer(b"x"))     # kapali istemciye kare birakilmaz
        finally:
            a.close()
            b.close()


# --------------------------------------------------------------- merkez
class HubTests(unittest.TestCase):
    def test_01_empty_hub_does_no_stats_work(self):
        """Istemci yokken kalp atisi istatistik hesaplamaz (bosta CPU sifir)."""
        fake = FakeStats()
        h = Hub(fake, interval=0.01, heartbeat=0.02)
        h.start()
        try:
            time.sleep(0.15)                  # ~7 kalp atisi
            self.assertEqual(fake.call_count(), 0)
        finally:
            h.stop()

    def test_02_notify_never_computes_and_never_blocks(self):
        """Is parcacigi bildirimi yalniz bayrak kaldirir: cagri aninda doner."""
        fake = FakeStats()
        h = Hub(fake, interval=0.1, heartbeat=0.1)
        try:
            t0 = time.monotonic()
            threads = [threading.Thread(target=lambda: [h.notify() for _ in range(200)])
                       for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5.0)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(fake.call_count(), 0)   # parcacik yok: hesap yok
        finally:
            h.stop()

    def test_03_sock_watcher_wire_format_and_change_push(self):
        """Soket izleyicisi ayni satir bicimini alir; degisiklik kare uretir."""
        fake = FakeStats()
        fake.upsert("a", state="queued")
        h = Hub(fake, interval=0.01, heartbeat=0.05)
        h.start()
        a, b = socket.socketpair()
        try:
            h.add_client(a, name="test")
            line = json.loads(LineReader(b).block(5.0))
            self.assertEqual(line["event"], "stats")
            self.assertEqual([j["id"] for j in line["jobs"]], ["a"])
            first = line["rev"]
            fake.upsert("b", state="paused")
            h.notify()
            seen = None
            end = time.monotonic() + 5.0
            while time.monotonic() < end:
                cand = json.loads(LineReader(b).block(5.0))
                if cand["rev"] > first:
                    seen = cand
                    break
            self.assertIsNotNone(seen)
            self.assertEqual(sorted(j["id"] for j in seen["jobs"]), ["a", "b"])
        finally:
            h.stop()
            a.close()
            b.close()

    def test_04_slow_client_does_not_starve_a_fast_one(self):
        """Yavas istemci soket tamponunda tikanir; digeri kare almaya devam eder."""
        fake = FakeStats()
        # kareyi buyut: yavas istemcinin tamponu dolsun (unix soket ~200 KB)
        fake.upsert("a", state="queued", note="y" * (2 << 20))
        h = Hub(fake, interval=0.01, heartbeat=0.02)
        h.start()
        slow_a, slow_b = socket.socketpair()
        fast_a, fast_b = socket.socketpair()
        slow = Client(slow_a, send_timeout=1.0, frame_timeout=60.0)
        slow.delta = False
        fast = Client(fast_a)
        try:
            with h._lock:
                h._clients.extend([slow, fast])
            slow.start()
            fast.start()
            h.notify()
            first = json.loads(LineReader(fast_b).block(5.0))
            t0 = time.monotonic()
            fake.upsert("a", state="queued", note="z" * (2 << 20))
            h.notify()                       # yavas istemci hala tikali
            self.assertLess(time.monotonic() - t0, 0.5)
            second = None
            while time.monotonic() - t0 < 5.0:
                cand = json.loads(LineReader(fast_b).block(5.0))
                if cand["rev"] >= first["rev"]:
                    second = cand
                    break
            self.assertIsNotNone(second)
            self.assertFalse(slow.closed.is_set(), "yavas istemci butcesi dolmadi")
        finally:
            h.stop()
            slow_a.close()
            slow_b.close()
            fast_a.close()
            fast_b.close()

    def test_04b_sock_watcher_gets_no_frame_when_nothing_changed(self):
        """Ayni kare iki kez uretilmez: kalp atislari bos trafik yaratmaz."""
        fake = FakeStats()
        fake.upsert("a", state="queued")
        h = Hub(fake, interval=0.01, heartbeat=0.02)
        h.start()
        a, b = socket.socketpair()
        try:
            h.add_client(a, name="test")
            reader = LineReader(b)
            self.assertEqual(json.loads(reader.block(5.0))["event"], "stats")
            # birkac kalp atisi gecti: kare DEGISMEDIGI icin yenisi gelmez
            with self.assertRaises(TimeoutError):
                reader.block(0.4)
            self.assertGreater(fake.call_count(), 1)   # yoklama devam etti
        finally:
            h.stop()
            a.close()
            b.close()

    def test_04c_config_only_change_reaches_watcher(self):
        """rev artmasa da turbo/limit gibi ust alanlar izleyiciye ulasir."""
        fake = FakeStats()
        fake.upsert("a", state="queued")
        h = Hub(fake, interval=0.01, heartbeat=0.02)
        h.start()
        a, b = socket.socketpair()
        try:
            h.add_client(a, name="test")
            reader = LineReader(b)
            first = json.loads(reader.block(5.0))
            self.assertFalse(first.get("turbo"))
            fake.top["turbo"] = True          # is listesi ve rev AYNI
            h.notify()
            nxt = json.loads(reader.block(5.0))
            self.assertEqual(nxt["rev"], first["rev"])
            self.assertTrue(nxt["turbo"])
        finally:
            h.stop()
            a.close()
            b.close()

    def test_05_sse_delta_after_change_and_full_first(self):
        """SSE: ilk kare tam, sonra yalniz degisenler + removed."""
        fake = FakeStats()
        fake.upsert("a", state="queued")
        fake.upsert("b", state="done")
        h = Hub(fake, interval=0.01, heartbeat=0.05)
        h.start()
        a, b = socket.socketpair()
        try:
            h.add_sse(a)
            frames = _SseFrames(b)
            first = frames.next()
            self.assertTrue(first["full"])
            self.assertEqual(sorted(j["id"] for j in first["jobs"]), ["a", "b"])
            fake.drop("a")
            fake.upsert("b", state="done", checksum="ok")
            h.notify()
            delta = frames.next()
            self.assertFalse(delta["full"])
            self.assertEqual([j["id"] for j in delta["jobs"]], ["b"])
            self.assertEqual(delta["removed"], ["a"])
            self.assertEqual(fake.calls[-1].get("since"), first["rev"])
            # arada degisiklik yok: yeni kare gelmez (yalniz ayni rev)
            with self.assertRaises(TimeoutError):
                frames.reader.block(0.3)
        finally:
            h.stop()
            a.close()
            b.close()

    def test_06_missing_rev_falls_back_to_full_frames(self):
        """rev yoksa (eski sunucu) her kare tamdir: fark uydurulmaz."""
        calls = []

        def stats(params):
            calls.append(dict(params))
            return {"ok": True, "jobs": [{"id": "a"}], "total_jobs": 1}

        h = Hub(stats, interval=0.01, heartbeat=0.02)
        h.start()
        a, b = socket.socketpair()
        try:
            h.add_sse(a)
            frames = _SseFrames(b)
            first = frames.next()
            self.assertEqual(first["jobs"], [{"id": "a"}])
            h.notify()
            second = frames.next()
            self.assertEqual(second["jobs"], [{"id": "a"}])
            # ilk kareden sonra `since` denenmez: rev bilinmiyor
            self.assertTrue(all("since" not in c for c in calls))
        finally:
            h.stop()
            a.close()
            b.close()

    def test_07_keepalive_comment_does_not_eat_frames(self):
        """SSE kalp atisi yorum satiridir; bekleyen kareyi ezmez."""
        fake = FakeStats()
        fake.upsert("a")
        # kalp atisi firsati olsun: itici uzun araliklarla calisir
        h = Hub(fake, interval=60.0, heartbeat=60.0)
        h.start()
        a, b = socket.socketpair()
        try:
            c = Client(a, keepalive=0.05)
            c.delta = True
            with h._lock:
                h._clients.append(c)
            c.start()
            h.notify()
            frames = _SseFrames(b)
            self.assertEqual([j["id"] for j in frames.next()["jobs"]], ["a"])
            # kare gelmeden kalp atisi: yorum satiri (veri degil)
            self.assertTrue(frames.wait_comment(5.0))
        finally:
            h.stop()
            a.close()
            b.close()


class _SseFrames:
    """SSE bloklarini ayristiran okuyucu (event/data alanlari)."""

    def __init__(self, sock):
        self.reader = LineReader(sock, sep=b"\n\n")

    def next(self, timeout=5.0) -> dict:
        end = time.monotonic() + timeout
        while True:
            block = self.reader.block(max(0.01, end - time.monotonic()))
            data = []
            for line in block.decode("utf-8").split("\n"):
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
            if data:
                return json.loads("\n".join(data))

    def wait_comment(self, timeout=5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                block = self.reader.block(max(0.01, end - time.monotonic()))
            except TimeoutError:
                return False
            if block.lstrip().startswith(b":"):
                return True
        return False


# --------------------------------------------------- soket sunucusu ucu
class SockServerTests(unittest.TestCase):
    def test_01_watch_stream_and_listener_wiring(self):
        root = tmp_root(self, "ferdim-p113-sock-")
        fake = FakeStats()
        fake.upsert("a", state="queued")
        qm = FakeQM()
        ctx = {"hub": Hub(fake, interval=0.01, heartbeat=0.05), "qm": qm}
        srv = SockServer(str(root / "ferdim.sock"), ctx)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # kuyruk dinleyicisi merkeze bagli: is parcacigi bildirimi
            self.assertEqual(len(qm.listeners), 1)
            client.settimeout(5.0)
            client.connect(str(root / "ferdim.sock"))
            client.sendall(b'{"cmd":"watch"}\n')
            frames = LineReader(client)
            first = json.loads(frames.block(5.0))
            self.assertEqual(first["event"], "stats")
            self.assertEqual([j["id"] for j in first["jobs"]], ["a"])
            # is parcacigi gibi davran: dinleyiciyi cagir (bildirim)
            fake.upsert("b", state="done")
            qm.listeners[0]()
            end = time.monotonic() + 5.0
            while time.monotonic() < end:
                cand = json.loads(frames.block(5.0))
                if any(j["id"] == "b" for j in cand["jobs"]):
                    break
            else:
                self.fail("degisiklik karesi gelmedi")
        finally:
            client.close()
            srv.shutdown()
            srv.server_close()
        # kapanista dinleyici cozulur (sarkan baglanti kalmaz)
        self.assertEqual(qm.listeners, [])

    def test_02_watcher_disconnect_is_cleaned_up(self):
        root = tmp_root(self, "ferdim-p113-sock2-")
        fake = FakeStats()
        fake.upsert("a")
        ctx = {"hub": Hub(fake, interval=0.01, heartbeat=0.02), "qm": FakeQM()}
        srv = SockServer(str(root / "ferdim.sock"), ctx)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(5.0)
            client.connect(str(root / "ferdim.sock"))
            client.sendall(b'{"cmd":"watch"}\n')
            self.assertTrue(wait_for(lambda: ctx["hub"].count() == 1))
        finally:
            client.close()
        # istemci gitti: istek is parcacigi izleyiciyi merkezden dusurur
        self.assertTrue(wait_for(lambda: ctx["hub"].count() == 0, 5.0))
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------ /api/events
def _http(port: int, path: str, token: str | None, host: str) -> bytes:
    lines = [f"GET {path} HTTP/1.1", "Connection: close", f"Host: {host}"]
    if token is not None:
        lines.append(f"X-FerDim-Token: {token}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class _WebCase(unittest.TestCase):
    """Yaltilmis HTTP sunucusu: sahte stats + gercek make_server."""

    def setUp(self):
        self.root = tmp_root(self, "ferdim-p113-web-")
        self.env = AppEnv(root=self.root)
        self.fake = FakeStats()
        self.fake.upsert("a", state="downloading")
        self.hub = Hub(self.fake, interval=0.01, heartbeat=0.05)
        self.ctx = {"env": self.env, "cfg": {}, "qm": FakeQM(),
                    "hub": self.hub, "web_access": api.WebAccess(TOKEN)}
        self.httpd = web.make_server(self.env, self.ctx, 0)
        self.port = self.httpd.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.hub.stop)

    def _open(self, token=TOKEN, path="/api/events"):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(_http(self.port, path, token, self.host))
        return sock


class EventsEndpointTests(_WebCase):
    def test_01_requires_token(self):
        sock = self._open(token=None)
        head = LineReader(sock, sep=b"\r\n\r\n").block(5.0)
        self.assertIn(b" 401 ", head.split(b"\r\n")[0])
        # akis acilmaz: govde JSON hatadir (event-stream basligi yok)
        self.assertNotIn(b"text/event-stream", head)

    def test_02_streams_full_then_delta_and_ends_on_shutdown(self):
        sock = self._open()
        header = LineReader(sock, sep=b"\r\n\r\n").block(5.0)
        self.assertIn(b" 200 ", header.split(b"\r\n")[0])
        self.assertIn(b"text/event-stream", header)
        frames = _SseFrames(sock)
        first = frames.next()
        self.assertTrue(first["full"])
        self.assertEqual([j["id"] for j in first["jobs"]], ["a"])

        # degisiklik: fark karesi (yalniz degisen is + sayim alanlari)
        self.fake.upsert("b", state="queued")
        self.hub.notify()
        delta = frames.next()
        self.assertFalse(delta["full"])
        self.assertEqual([j["id"] for j in delta["jobs"]], ["b"])
        self.assertEqual(delta["total_jobs"], 2)
        self.assertEqual(self.fake.calls[-1].get("since"), first["rev"])

        # sunucu kapanisi akisi bitirir (istek is parcacigi sizmaz)
        self.hub.stop()
        self.assertTrue(LineReader(sock, sep=b"\n\n").eof(5.0))

    def test_03_no_token_in_url(self):
        # token yalniz basliktadir: sorgu dizesi kimlik dogrulamaz
        sock = self._open(token=None, path=f"/api/events?token={TOKEN}")
        head = LineReader(sock, sep=b"\r\n\r\n").block(5.0)
        self.assertIn(b" 401 ", head.split(b"\r\n")[0])

    def test_04_unknown_host_rejected(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(_http(self.port, "/api/events", TOKEN, "evil.example:1"))
        head = LineReader(sock, sep=b"\r\n\r\n").block(5.0)
        self.assertIn(b" 421 ", head.split(b"\r\n")[0])


class StatsQueryTests(_WebCase):
    """GET /api/stats?since=&limit=&offset= parametreleri api.handle'a gider."""

    def test_stats_query_params_reach_api(self):
        seen = []

        def fake_handle(ctx, cmd, params):
            seen.append((cmd, dict(params)))
            return {"ok": True, "jobs": [], "rev": 1, "full": True}

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        with mock.patch.object(web.api, "handle", fake_handle):
            sock.sendall(_http(self.port,
                               "/api/stats?since=5&limit=20&offset=40",
                               TOKEN, self.host))
            body = b""
            while True:
                part = sock.recv(65536)
                if not part:
                    break
                body += part
        self.assertIn(b" 200 ", body.split(b"\r\n")[0])
        self.assertEqual(seen, [("stats", {"since": 5, "limit": 20,
                                           "offset": 40})])

    def test_stats_without_params_stays_full(self):
        seen = []

        def fake_handle(ctx, cmd, params):
            seen.append((cmd, dict(params)))
            return {"ok": True, "jobs": []}

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        with mock.patch.object(web.api, "handle", fake_handle):
            sock.sendall(_http(self.port, "/api/stats", TOKEN, self.host))
            while sock.recv(65536):
                pass
        self.assertEqual(seen, [("stats", {})])


class StatsParamsTests(unittest.TestCase):
    def test_only_known_numeric_params_pass(self):
        self.assertEqual(web.stats_params("since=7&limit=20&offset=40"),
                         {"since": 7, "limit": 20, "offset": 40})
        self.assertEqual(web.stats_params("since=abc&q=1"), {})
        self.assertEqual(web.stats_params("since=+5"), {"since": 5})
        self.assertEqual(web.stats_params(""), {})


# ------------------------------------------------------- web arayuzu (statik)
class WebUiStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "web" / "app.js").read_text("utf-8")
        cls.html = (ROOT / "web" / "index.html").read_text("utf-8")
        cls.css = (ROOT / "web" / "style.css").read_text("utf-8")

    def test_01_no_innerhtml_and_eventsource_free(self):
        self.assertNotIn("innerHTML", self.js)
        # EventSource baslik gonderemez: akis fetch + okuyucu ile okunur
        self.assertNotIn("new EventSource", self.js)
        self.assertIn("/api/events", self.js)
        self.assertIn("ReadableStream" if "ReadableStream" in self.js
                      else "getReader", self.js)
        self.assertIn("X-FerDim-Token", self.js)
        # token adrese KONMAZ
        self.assertNotIn("/api/events?token", self.js)

    def test_02_visibility_and_fallback(self):
        for needle in ("visibilitychange", "document.hidden", "since=",
                       "removed", "total_jobs", "fallbackLoop"):
            self.assertIn(needle, self.js)
        # 500 ms yoklama dongusu kalkti; akis koptugunda yedege dusulur
        self.assertNotIn("setInterval(poll", self.js)

    def test_03_windowed_list_and_load_more(self):
        for needle in ("VIRTUAL_MIN", "rowPadTop", "rowPadBot", "OVERSCAN"):
            self.assertIn(needle, self.js)
        self.assertIn(".pad{", self.css)
        self.assertIn('id="histMore"', self.html)
        self.assertIn("hist_more", self.js + self.html)
        self.assertIn("HIST_PAGE", self.js)
        # gecmis sayfalama: offset gonderilir
        self.assertIn("offset", self.js)

    def test_04_new_keys_in_all_languages(self):
        for key in ("hist_more", "retry_at"):
            self.assertIn(key, S, key)
            self.assertEqual(set(S[key]), set(LANGS), key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
