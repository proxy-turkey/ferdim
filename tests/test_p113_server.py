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
        self.active = 0         # calisan is sayisi (hub kalp atisi karari)
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
                   "active": self.active, **self.top}
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


def wait_for(pred, timeout=20.0, step=0.01) -> bool:
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

    def block(self, timeout=20.0) -> bytes:
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

    def eof(self, timeout=20.0) -> bool:
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


class _StubSock:
    """Engellenebilir sahte soket: yaziciyi kare ortasinda durdurmak icin.

    Gercek soket tamponu boyle bir denetim vermez; tek yuvali kuyrugun
    kare ezmesi ve yavas yazici senaryolarini sinamak icin yazma adimi
    olayla bekletilir."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = bytearray()
        self.armed = threading.Event()
        self.blocked = threading.Event()
        self.release = threading.Event()

    def setsockopt(self, *_a, **_k):
        pass

    def shutdown(self, _how=None):
        pass

    def send(self, chunk):
        if self.armed.is_set() and not self.release.is_set():
            self.blocked.set()
            self.release.wait(10.0)
        with self.lock:
            self.data += bytes(chunk)
        return len(chunk)

    def frames(self):
        """Biriken SSE govdesini karelere ayirir (yarim kare atilir)."""
        with self.lock:
            text = self.data.decode("utf-8", "replace")
        out = []
        for block in text.split("\n\n"):
            data = [ln[5:].lstrip(" ") for ln in block.split("\n")
                    if ln.startswith("data:")]
            if not data:
                continue
            try:
                out.append(json.loads("\n".join(data)))
            except ValueError:      # yarim kare: henuz yazilmadi
                continue
        return out


class _DeafEvent:
    """Yalniz zaman asimi: `set()` iticiyi UYANDIRMAZ.

    Gercekte bu yaris, yuklu bir makinede `_on_change`/yeni istemci
    sinyalinin itici `_wake.clear()` cagrisiyla yutulmasiyla olusur;
    burada deterministik olarak benzetilir (sinyal her zaman kaybolur)."""

    def wait(self, timeout=None):
        time.sleep(timeout or 0)
        return False

    def set(self):        # sinyal yolda kayboldu
        pass

    def clear(self):
        pass


def apply_frames(frames) -> dict:
    """Kareleri sirayla uygular (web/app.js applyStats gibi; yinelenebilir).

    Tam kare listeyi DEGISTIRIR, fark karesi kimlik bazinda ezer, `removed`
    duser. Ayni karenin iki kez uygulanmasi sonucu degistirmez."""
    state = {}
    for frame in frames:
        if frame.get("full"):
            state.clear()
        for jid in frame.get("removed") or ():
            state.pop(jid, None)
        for job in frame.get("jobs") or ():
            state[job["id"]] = job
    return state


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
            self.assertTrue(c.offer(b"birinci", 1))
            self.assertTrue(c.offer(b"ikinci", 2))
            self.assertEqual(c.take(), (b"ikinci", 2))   # eski kare dustu
            self.assertEqual(c.take(), (b"", None))
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
            self.assertTrue(c.closed.wait(20.0), "yazma butcesi asilinca kapanmali")
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
                t.join(20.0)
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
            line = json.loads(LineReader(b).block(20.0))
            self.assertEqual(line["event"], "stats")
            self.assertEqual([j["id"] for j in line["jobs"]], ["a"])
            first = line["rev"]
            fake.upsert("b", state="paused")
            h.notify()
            seen = None
            end = time.monotonic() + 20.0
            while time.monotonic() < end:
                cand = json.loads(LineReader(b).block(20.0))
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
            first = json.loads(LineReader(fast_b).block(20.0))
            t0 = time.monotonic()
            fake.upsert("a", state="queued", note="z" * (2 << 20))
            h.notify()                       # yavas istemci hala tikali
            self.assertLess(time.monotonic() - t0, 0.5)
            second = None
            while time.monotonic() - t0 < 20.0:
                cand = json.loads(LineReader(fast_b).block(20.0))
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
        fake.active = 1           # mesgul: kalp atisi hesap YAPAR, kare YOK
        fake.upsert("a", state="queued")
        h = Hub(fake, interval=0.01, heartbeat=0.02)
        h.start()
        a, b = socket.socketpair()
        try:
            h.add_client(a, name="test")
            reader = LineReader(b)
            self.assertEqual(json.loads(reader.block(20.0))["event"], "stats")
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
            first = json.loads(reader.block(20.0))
            self.assertFalse(first.get("turbo"))
            fake.top["turbo"] = True          # is listesi ve rev AYNI
            h.notify()
            nxt = json.loads(reader.block(20.0))
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
            client = h.add_sse(a)
            frames = _SseFrames(b)
            first = frames.next()
            self.assertTrue(first["full"])
            # yazici basarili gonderimden sonra rev'i ilerletir (H1)
            self.assertTrue(wait_for(lambda: client.rev == first["rev"], 20.0))
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
            self.assertTrue(frames.wait_comment(20.0))
        finally:
            h.stop()
            a.close()
            b.close()

    # ------------------------------------------------ kare kaybi / sinirlar
    def test_08_replaced_frame_is_not_lost(self):
        """Tek yuvali kuyruk bir kareyi ezse bile degisiklik kaybolmaz.

        `since` GONDERILMIS rev'den hesaplandigi icin ezilen karenin
        degisiklikleri sonraki karede yeniden gonderilir (H1). Itici
        parcacik baslatilmaz: `_push` elle cagrilir (yaris yok)."""
        fake = FakeStats()
        fake.upsert("a", state="queued", note="n")
        h = Hub(fake, interval=0.01, heartbeat=60.0)
        stub = _StubSock()
        client = h.add_sse(stub)
        try:
            h._push()                       # kucuk tam kare (rev1)
            self.assertTrue(wait_for(lambda: client.rev is not None, 20.0))
            stub.armed.set()
            # yazici BUYUK bir fark karesiyle mesgul: kare yiyecek kadar uzun
            fake.upsert("a", state="queued", note="x" * (4 << 20))
            h._push()
            self.assertTrue(stub.blocked.wait(20.0), "yazici mesgul edilemedi")
            # yazici mesgulken iki degisiklik: ikinci teklif birinciyi EZER
            fake.upsert("b", state="done")
            h._push()
            fake.upsert("c", state="done")
            h._push()
            stub.release.set()
            self.assertTrue(wait_for(lambda: client.rev == fake.rev, 20.0))
            state = apply_frames(stub.frames())
            self.assertIn("b", state, "ezilen kare kayboldu")
            self.assertEqual(state["b"]["state"], "done")
            self.assertEqual(state["c"]["state"], "done")
            self.assertEqual(state["a"]["state"], "queued")
        finally:
            h.stop()

    def test_09_hub_restarts_after_stop(self):
        """stop() sonrasi start() yeniden calisir (L2)."""
        fake = FakeStats()
        fake.upsert("a", state="queued")
        h = Hub(fake, interval=0.01, heartbeat=0.05)
        h.start()
        self.assertTrue(h.running)
        h.stop()
        self.assertFalse(h.running)
        h.start()                       # durdurma bayragi temizlenmeli
        self.assertTrue(h.running)
        a, b = socket.socketpair()
        try:
            self.assertIsNotNone(h.add_client(a, name="test"))
            first = json.loads(LineReader(b).block(20.0))
            self.assertEqual(first["event"], "stats")
            fake.upsert("a", state="done")
            h.notify()
            end = time.monotonic() + 20.0
            seen = None
            while time.monotonic() < end:
                cand = json.loads(LineReader(b).block(20.0))
                if any(j["state"] == "done" for j in cand["jobs"]):
                    seen = cand
                    break
            self.assertIsNotNone(seen, "yeniden baslayan merkez kare uretmedi")
        finally:
            h.stop()
            a.close()
            b.close()

    def test_10_one_delta_per_push_for_many_viewers(self):
        """Farkli rev'lerdeki istemciler TEK istatistik hesabiyla beslenir.

        Fark, izleyicilerin GORDUGU en eski rev'den hesaplanir: ust kume
        alan istemci ayni kareyi yeniden uygular (yinelenebilir)."""
        fake = FakeStats()
        fake.upsert("a", state="queued")
        h = Hub(fake, interval=0.01, heartbeat=60.0)
        stub = _StubSock()
        slow = h.add_sse(stub)                 # A: yazicisi kilitlenecek
        sock_a, sock_b = socket.socketpair()
        try:
            h._push()                          # A: tam kare (rev1)
            self.assertTrue(wait_for(lambda: slow.rev is not None, 20.0))
            stub.armed.set()
            fake.upsert("b", state="queued")
            h._push()                          # A tikali: rev ilerlemez
            self.assertTrue(stub.blocked.wait(20.0))
            fast = h.add_sse(sock_a)           # B: tam kare ile GUNCEL rev
            h._push()                          # B'nin ilk karesi (tam)
            frames = _SseFrames(sock_b)
            full = frames.next()
            self.assertTrue(full["full"])
            self.assertTrue(wait_for(lambda: fast.rev == fake.rev, 20.0))
            self.assertLess(slow.rev, fast.rev)        # farkli rev'ler
            calls = fake.call_count()
            fake.upsert("c", state="done")
            h._push()                          # TEK hesap: min rev'den fark
            self.assertEqual(fake.call_count() - calls, 1)
            delta = frames.next()
            self.assertEqual(sorted(j["id"] for j in delta["jobs"]),
                             ["b", "c"])       # ikisinin de eksiği kapanir
            state_b = apply_frames([full, delta])
            self.assertEqual(state_b["b"]["state"], "queued")
            self.assertEqual(state_b["c"]["state"], "done")
            stub.release.set()                 # yavas istemci de yetisir
            self.assertTrue(wait_for(
                lambda: "c" in apply_frames(stub.frames()), 20.0))
            state_a = apply_frames(stub.frames())
            self.assertEqual(state_a["b"]["state"], "queued")
            self.assertEqual(state_a["c"]["state"], "done")
            self.assertTrue(wait_for(lambda: slow.rev == fast.rev, 20.0))
        finally:
            h.stop()
            sock_a.close()
            sock_b.close()

    # ------------------------------------------------ bos kuyruk maliyeti
    def test_11_idle_queue_skips_heartbeat_stats(self):
        """Sessiz + bos kuyrukta kalp atisi istatistik HESAPLAMAZ.

        10 000 iste `stats()` ~50 ms; 1 Hz'de tek basasi ~5 % CPU idi.
        Istemci bu sirada `: ping` yorumu alir (akis canli kalir), hesap
        yalniz notify'da ve seyrek tazelemede yapilir."""
        fake = FakeStats()               # active=0: calisan is yok
        fake.upsert("a", state="paused")
        h = Hub(fake, interval=0.01, heartbeat=0.03, idle_refresh=60.0)
        h.start()
        a, b = socket.socketpair()
        try:
            self.assertIsNotNone(h.add_client(a, name="test"))
            self.assertEqual(json.loads(LineReader(b).block(20.0))["event"],
                             "stats")
            calls = fake.call_count()
            time.sleep(0.4)              # ~13 kalp atisi
            self.assertEqual(fake.call_count(), calls,
                             "bos kuyrukta kalp atisi hesap yapmamali")
            # notify: degisiklik hemen hesaplanir (kacmaz)
            fake.upsert("a", state="paused", filename="yeni.bin")
            h.notify()
            self.assertTrue(wait_for(lambda: fake.call_count() > calls, 20.0))
        finally:
            h.stop()
            a.close()
            b.close()

    def test_12_busy_queue_keeps_heartbeat(self):
        """Calisan is varken hiz/ilerleme 1 Hz kalp atisiyla tazelenir."""
        fake = FakeStats()
        fake.active = 1
        fake.upsert("a", state="downloading")
        h = Hub(fake, interval=0.01, heartbeat=0.03, idle_refresh=60.0)
        h.start()
        a, b = socket.socketpair()
        try:
            self.assertIsNotNone(h.add_client(a, name="test"))
            LineReader(b).block(20.0)
            calls = fake.call_count()
            time.sleep(0.4)
            self.assertGreaterEqual(fake.call_count() - calls, 4,
                                    "mesgul kuyrukta kalp atisi hesap yapmali")
        finally:
            h.stop()
            a.close()
            b.close()

    def test_13_idle_refresh_keeps_time_based_fields_fresh(self):
        """Sessiz kuyrukta seyrek tazeleme kalir: zamana bagli alanlar
        (nm_connected, sched_wait) sonsuza kadar bayat kalmaz."""
        fake = FakeStats()
        fake.upsert("a", state="paused")
        h = Hub(fake, interval=0.01, heartbeat=0.02, idle_refresh=0.08)
        h.start()
        a, b = socket.socketpair()
        try:
            self.assertIsNotNone(h.add_client(a, name="test"))
            LineReader(b).block(20.0)
            calls = fake.call_count()
            self.assertTrue(wait_for(lambda: fake.call_count() > calls, 20.0))
            # seyrek: ayni pencerede onlarca kez hesaplanmaz
            time.sleep(0.12)
            self.assertLessEqual(fake.call_count() - calls, 4)
        finally:
            h.stop()
            a.close()
            b.close()

    def test_14_first_frame_for_a_new_client_is_immediate(self):
        """Bos kuyrukta yeni istemci ilk karesini beklemeden alir."""
        fake = FakeStats()
        fake.upsert("a", state="paused")
        h = Hub(fake, interval=0.01, heartbeat=0.03, idle_refresh=60.0)
        h.start()
        a, b = socket.socketpair()
        try:
            h._pushed = time.monotonic()      # uyku modunda say
            self.assertIsNotNone(h.add_client(a, name="test"))
            first = json.loads(LineReader(b).block(2.0))
            self.assertEqual(first["event"], "stats")
            self.assertEqual([j["id"] for j in first["jobs"]], ["a"])
        finally:
            h.stop()
            a.close()
            b.close()

    def test_15_first_frame_survives_a_lost_wakeup(self):
        """Uyandirma bayragi kaybolsa bile ilk tam kare gecikmez.

        Itici parcacik, yeni istemci kaydolurken sinyali `clear()` ile
        yutabilir (yuklu makinede pencere buyur). Eski kod bu durumda
        istemciye IDLE_REFRESH (15 sn) boyunca kare gondermiyordu; SSE
        testleri bu yuzden distro derleyicilerinde zaman asimi veriyordu.
        Bekleyen is artik `_seq` sayacindan ve `Client.primed`ten okunur."""
        fake = FakeStats()                 # active=0: bos ve sessiz kuyruk
        fake.upsert("a", state="paused")
        h = Hub(fake, interval=0.01, heartbeat=0.05, idle_refresh=60.0)
        h.start()
        warm, warm_peer = socket.socketpair()
        a, b = socket.socketpair()
        try:
            # 1) bir istemci isinmis olsun: hub "bos + sessiz" durumuna gecer
            self.assertIsNotNone(h.add_client(warm, name="warm"))
            self.assertEqual(
                json.loads(LineReader(warm_peer).block(20.0))["event"], "stats")
            self.assertTrue(wait_for(lambda: not h._busy, 20.0))
            # 2) sinyaller artik kaybolsun (yuklu makinede olan sey)
            h._wake = _DeafEvent()
            # 3) yeni istemcinin ILK karesi yine de hemen gelir
            self.assertIsNotNone(h.add_client(a, name="test"))
            first = json.loads(LineReader(b).block(20.0))
            self.assertEqual(first["event"], "stats")
            self.assertEqual([j["id"] for j in first["jobs"]], ["a"])
        finally:
            h.stop()
            warm.close()
            warm_peer.close()
            a.close()
            b.close()

    def test_16_client_caps(self):
        """Eszamanli istemci siniri: SSE ve soket izleyicisi ayri sayilir."""
        fake = FakeStats()
        h = Hub(fake, interval=10.0, heartbeat=10.0,
                max_sse=1, max_watchers=1)
        pairs = [socket.socketpair() for _ in range(4)]
        socks = [p[0] for p in pairs]
        peers = [p[1] for p in pairs]
        try:
            first = h.add_sse(socks[0])
            self.assertIsNotNone(first)
            self.assertIsNone(h.add_sse(socks[1]))      # sinir: SSE
            self.assertIsNotNone(h.add_client(socks[2], name="w1"))
            self.assertIsNone(h.add_client(socks[3], name="w2"))  # izleyici
            self.assertEqual(h.count(), 2)
            # yuva bosalinca yeni istemci kabul edilir
            h.remove_client(first)
            self.assertIsNotNone(h.add_sse(socks[1]))
        finally:
            h.stop()
            for sock in socks + peers:
                sock.close()


class _SseFrames:
    """SSE bloklarini ayristiran okuyucu (event/data alanlari)."""

    def __init__(self, sock):
        self.reader = LineReader(sock, sep=b"\n\n")

    def next(self, timeout=20.0) -> dict:
        end = time.monotonic() + timeout
        while True:
            block = self.reader.block(max(0.01, end - time.monotonic()))
            data = []
            for line in block.decode("utf-8").split("\n"):
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
            if data:
                return json.loads("\n".join(data))

    def wait_comment(self, timeout=20.0) -> bool:
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
            client.settimeout(20.0)
            client.connect(str(root / "ferdim.sock"))
            client.sendall(b'{"cmd":"watch"}\n')
            frames = LineReader(client)
            first = json.loads(frames.block(20.0))
            self.assertEqual(first["event"], "stats")
            self.assertEqual([j["id"] for j in first["jobs"]], ["a"])
            # is parcacigi gibi davran: dinleyiciyi cagir (bildirim)
            fake.upsert("b", state="done")
            qm.listeners[0]()
            end = time.monotonic() + 20.0
            while time.monotonic() < end:
                cand = json.loads(frames.block(20.0))
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
            client.settimeout(20.0)
            client.connect(str(root / "ferdim.sock"))
            client.sendall(b'{"cmd":"watch"}\n')
            self.assertTrue(wait_for(lambda: ctx["hub"].count() == 1))
        finally:
            client.close()
        # istemci gitti: istek is parcacigi izleyiciyi merkezden dusurur
        self.assertTrue(wait_for(lambda: ctx["hub"].count() == 0, 20.0))
        srv.shutdown()
        srv.server_close()

    def test_03_watcher_cap_sends_error_line(self):
        """Izleyici siniri doluysa JSON hata satiri; baglanti acik kalir."""
        root = tmp_root(self, "ferdim-p113-cap-")
        fake = FakeStats()
        fake.upsert("a")
        hub = Hub(fake, interval=0.01, heartbeat=0.05, max_watchers=1)
        ctx = {"hub": hub, "qm": FakeQM(), "env": object(), "cfg": {}}
        srv = SockServer(str(root / "ferdim.sock"), ctx)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            first.settimeout(20.0)
            first.connect(str(root / "ferdim.sock"))
            first.sendall(b'{"cmd":"watch"}\n')
            self.assertEqual(json.loads(LineReader(first).block(20.0))["event"],
                             "stats")
            second.settimeout(20.0)
            second.connect(str(root / "ferdim.sock"))
            second.sendall(b'{"cmd":"watch"}\n')
            reply = json.loads(LineReader(second).block(20.0))
            self.assertEqual(reply.get("code"), "too_many")
            # reddedilen baglanti kapanmadi: RPC hala kullanilabilir
            second.sendall(b'{"cmd":"version","req_id":7}\n')
            msg = json.loads(LineReader(second).block(20.0))
            self.assertEqual(msg.get("req_id"), 7)
            self.assertTrue(msg.get("ok"))
        finally:
            first.close()
            second.close()
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

    hub_kwargs = {"interval": 0.01, "heartbeat": 0.05}

    def setUp(self):
        self.root = tmp_root(self, "ferdim-p113-web-")
        self.env = AppEnv(root=self.root)
        self.fake = FakeStats()
        self.fake.upsert("a", state="downloading")
        self.hub = Hub(self.fake, **self.hub_kwargs)
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
        head = LineReader(sock, sep=b"\r\n\r\n").block(20.0)
        self.assertIn(b" 401 ", head.split(b"\r\n")[0])
        # akis acilmaz: govde JSON hatadir (event-stream basligi yok)
        self.assertNotIn(b"text/event-stream", head)

    def test_02_streams_full_then_delta_and_ends_on_shutdown(self):
        sock = self._open()
        header = LineReader(sock, sep=b"\r\n\r\n").block(20.0)
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
        self.assertTrue(LineReader(sock, sep=b"\n\n").eof(20.0))

    def test_03_no_token_in_url(self):
        # token yalniz basliktadir: sorgu dizesi kimlik dogrulamaz
        sock = self._open(token=None, path=f"/api/events?token={TOKEN}")
        head = LineReader(sock, sep=b"\r\n\r\n").block(20.0)
        self.assertIn(b" 401 ", head.split(b"\r\n")[0])

    def test_04_unknown_host_rejected(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(_http(self.port, "/api/events", TOKEN, "evil.example:1"))
        head = LineReader(sock, sep=b"\r\n\r\n").block(20.0)
        self.assertIn(b" 421 ", head.split(b"\r\n")[0])


class SseCapTests(_WebCase):
    """Eszamanli akis siniri: ikinci sekme 503 + JSON hata alir (L4)."""

    hub_kwargs = {"interval": 0.01, "heartbeat": 0.05, "max_sse": 1}

    def test_01_second_stream_rejected(self):
        first = self._open()
        head = LineReader(first, sep=b"\r\n\r\n").block(20.0)
        self.assertIn(b" 200 ", head.split(b"\r\n")[0])
        _SseFrames(first).next()               # kayit oturdu
        self.assertEqual(self.hub.count(), 1)
        second = self._open()
        rd2 = LineReader(second, sep=b"\r\n\r\n")
        head2 = rd2.block(20.0)
        self.assertIn(b" 503 ", head2.split(b"\r\n")[0])
        self.assertNotIn(b"text/event-stream", head2)
        # baslik ve govde tek pakette gelebilir: okuyucunun artigi govdedir
        body = rd2.buf
        while True:
            part = second.recv(65536)
            if not part:
                break
            body += part
        self.assertIn(b"too_many", body)
        # reddedilen istemci merkeze KAYDEDILMEZ (yuva kapanmaz, tikanmaz)
        self.assertEqual(self.hub.count(), 1)


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

    def test_05_stream_gap_triggers_resync(self):
        """Kare boslugu sessizce yutulmaz: akis TAM kareyle yeniden kurulur.

        `rev=0` yalniz yoklamayi duzeltir; canli akis sunucu tarafinda kendi
        rev'ini tutar. Sayim tutmazsa, kare cozulemezse ya da kare siniri
        asarsa akis kapatilip yeniden baglanilir (ilk kare tamdir)."""
        self.assertIn("function resync()", self.js)
        self.assertIn("FRAME_MAX", self.js)
        for head, need in (("function applyStats", "resync()"),
                           ("function handleFrame", "resync()"),
                           ("async function readFrames", "resync()")):
            start = self.js.index(head)
            body = self.js[start:self.js.index("\n}\n", start)]
            self.assertIn(need, body, head)
        # sessiz sifirlama kalmadi: yalniz baslangic atamasi kaldi
        self.assertEqual(self.js.count('buf = ""'), 1, "sessiz sifirlama olmamali")

    def test_06_hosts_panel_refreshes_on_its_own_timer(self):
        """Panel tazelemesi istatistik karelerine BAGLANMAZ.

        Sunucu degisiklik yokken kare gondermez (bosta trafik yok); panel
        yine tazelenmeli. 1.13 tumlesmesinde tazeleme kare akisina
        baglanmisti: bos kuyrukta panel donuyordu."""
        self.assertNotIn("maybeHosts", self.js)
        self.assertIn("hostsTimer", self.js)
        self.assertIn("setTimeout(hostsPoll, 2000)", self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
