"""LAN esbaz onbellegi (ROADMAP 2.x): indeks, sunucu, istemci, uctan uca.

Ag gerekmez: sunucular 127.0.0.1 uzerinde, multicast KAPALI; esbazlar
dogrudan enjekte edilir. Isler gercek motorla kosar (paralel araliklar) ve
kaynak istek sayisi tests.helpers sayaciyla dogrulanir.
"""
from __future__ import annotations

import hashlib
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import cdc, peer
from ferdim.engine import httpcli
from ferdim.engine import job as jobmod
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.paths import AppEnv
from tests.helpers import DATA, REQUEST_COUNTS, start_server, tmp_root

SMALL_DATA = hashlib.shake_256(b"ferdim-peer-small").digest(1 << 20)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wait_state(job, states, timeout=60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.state in states:
            return True
        time.sleep(0.05)
    return False


def prepare_source(root: Path, data: bytes = DATA):
    """Verilen baytlar + Merkle bildirimi (servis edilen dosyayla ayni)."""
    src = root / "veri.bin"
    src.write_bytes(data)
    man = cdc.make_manifest(src)
    man_path = cdc.write_manifest(man, root / "veri.bin.ferdim-merkle")
    return src, man, man_path


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.root = tmp_root(self, "ferdim-peer-idx-")
        self.src, self.man, self.man_path = prepare_source(
            self.root, SMALL_DATA)

    def test_scan_builds_hash_map_and_roots(self):
        idx = peer.PeerIndex(dirs=[self.root])
        idx.refresh(force=True)
        self.assertEqual(idx.roots(), [self.man.root])
        for off, ln, hx in self.man.chunks:
            self.assertEqual(idx.lookup(hx), (str(self.src), off, ln))
        self.assertIsNone(idx.lookup("0" * 64))

    def test_lookup_rejects_non_hash(self):
        idx = peer.PeerIndex(dirs=[self.root])
        idx.refresh(force=True)
        self.assertIsNone(idx.lookup("../../etc/passwd"))
        self.assertIsNone(idx.lookup("kisa"))

    def test_add_verified_persists_cache(self):
        cache = self.root / "cache" / "peer-index.json"
        idx = peer.PeerIndex(cache_path=cache)
        self.assertTrue(idx.add(self.man, self.src))
        self.assertTrue(cache.is_file())
        again = peer.PeerIndex(cache_path=cache)
        again.refresh(force=True)
        off, ln, hx = self.man.chunks[0]
        self.assertEqual(again.lookup(hx), (str(self.src), off, ln))
        self.assertIn(self.man.root, again.roots())

    def test_missing_data_file_is_not_served(self):
        self.src.unlink()
        idx = peer.PeerIndex(dirs=[self.root])
        idx.refresh(force=True)
        self.assertEqual(idx.roots(), [])

    def test_subtract_ranges(self):
        self.assertEqual(
            peer.subtract_ranges([(0, 99)], [(20, 49)]),
            [(0, 19), (50, 99)])
        self.assertEqual(
            peer.subtract_ranges([(0, 99)], [(0, 99)]), [])
        self.assertEqual(
            peer.subtract_ranges([(0, 9), (20, 29)], [(5, 24)]),
            [(0, 4), (25, 29)])
        # bekleyen araligin disindaki doldurma dokunmaz
        self.assertEqual(
            peer.subtract_ranges([(0, 9)], [(50, 59)]), [(0, 9)])


class NodeTests(unittest.TestCase):
    def _cfg(self, env, port, dirs=()):
        cfg = env.load_config()
        cfg["peer_cache"] = True
        cfg["peer_port"] = port
        cfg["peer_dirs"] = list(dirs)
        return cfg

    def test_start_stop_and_verified_index(self):
        root = tmp_root(self, "ferdim-peer-node-")
        src, man, _ = prepare_source(root, SMALL_DATA)
        env = AppEnv(root=root / "env")
        node = peer.PeerNode(env, multicast=False, host="127.0.0.1")
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.assertTrue(node.start(self._cfg(env, port, [root])))
        try:
            self.assertIsNotNone(node.server)
            self.assertEqual(node.server.port, port)
            self.assertTrue(node.add_verified(man, src))
            self.assertEqual(node.index.roots(), [man.root])
        finally:
            node.stop()
            self.assertIsNone(node.server)

    def test_start_is_noop_when_disabled(self):
        root = tmp_root(self, "ferdim-peer-off-")
        env = AppEnv(root=root)
        node = peer.PeerNode(env, multicast=False, host="127.0.0.1")
        self.assertFalse(node.start(env.load_config()))
        self.assertIsNone(node.server)


class BeaconTests(unittest.TestCase):
    def test_parse_valid_beacon(self):
        raw = (b'{"v":1,"node_id":"abc","port":8379,'
               b'"roots":["' + b"a" * 64 + b'"]}')
        b = peer.parse_beacon(raw, own_id="baska")
        self.assertEqual(b["port"], 8379)
        self.assertEqual(b["roots"], ["a" * 64])

    def test_rejects_bad_or_own_beacon(self):
        self.assertIsNone(peer.parse_beacon(b"{}"))
        self.assertIsNone(peer.parse_beacon(b'{"v":2,"node_id":"x","port":1}'))
        self.assertIsNone(peer.parse_beacon(
            b'{"v":1,"node_id":"x","port":0}'))
        own = b'{"v":1,"node_id":"ben","port":8379,"roots":[]}'
        self.assertIsNone(peer.parse_beacon(own, own_id="ben"))

    def test_roots_capped_and_deduplicated(self):
        roots = ",".join(['"' + "a" * 64 + '"'] * 5)
        raw = ('{"v":1,"node_id":"x","port":8379,"roots":[' + roots + ']}')
        b = peer.parse_beacon(raw.encode())
        self.assertEqual(b["roots"], ["a" * 64])


class ServerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.root = tmp_root(self, "ferdim-peer-srv-")
        self.src, self.man, _ = prepare_source(self.root, SMALL_DATA)
        self.idx = peer.PeerIndex(dirs=[self.root])
        self.idx.refresh(force=True)
        self.srv = peer.PeerServer(self.idx, host="127.0.0.1", port=0)
        self.srv.start()
        self.base = f"http://127.0.0.1:{self.srv.port}"
        self.addCleanup(self.srv.stop)

    def _status(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_serves_only_known_chunk_hash(self):
        off, ln, hx = self.man.chunks[0]
        code, body = self._status(f"/chunk/{hx}")
        self.assertEqual(code, 200)
        self.assertEqual(body, SMALL_DATA[off:off + ln])
        self.assertEqual(self._status(f"/chunk/{'f' * 64}")[0], 404)

    def test_refuses_traversal_and_non_hash(self):
        for path in ("/chunk/../../etc/passwd", "/chunk/kisa",
                     "/chunk/" + "z" * 64, "/chunk/", "/", "/liste"):
            with self.subTest(path=path):
                self.assertIn(self._status(path)[0], (400, 404))

    def test_public_source_is_refused(self):
        self.assertTrue(peer.is_lan_ip("192.168.1.20"))
        self.assertTrue(peer.is_lan_ip("10.0.0.5"))
        self.assertTrue(peer.is_lan_ip("127.0.0.1"))
        self.assertTrue(peer.is_lan_ip("fd00::1"))
        self.assertFalse(peer.is_lan_ip("8.8.8.8"))
        self.assertFalse(peer.is_lan_ip("2001:4860:4860::8888"))
        self.assertTrue(self.srv.source_ok("192.168.1.20"))
        self.assertFalse(self.srv.source_ok("8.8.8.8"))

    def test_handler_denies_when_source_check_fails(self):
        srv = peer.PeerServer(self.idx, host="127.0.0.1", port=0)
        srv.source_ok = lambda ip: False          # genel IP enjeksiyonu
        srv._httpd.peer_source_ok = srv.source_ok
        srv.start()
        try:
            url = f"http://127.0.0.1:{srv.port}/chunk/{self.man.chunks[0][2]}"
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(url, timeout=5)
            self.assertEqual(cm.exception.code, 403)
        finally:
            srv.stop()


class BadPeer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bad_len = 1024
    hits = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        if not urlsplit(self.path).path.startswith("/chunk/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        type(self).hits += 1
        body = b"\x00" * type(self).bad_len
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):
            pass


def start_bad_peer(ln):
    BadPeer.bad_len = ln
    BadPeer.hits = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), BadPeer)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class SlowPeer(BaseHTTPRequestHandler):
    """Gecerli parcalari gecikmeyle sunan sayacli esbaz (zamanlama testi)."""

    protocol_version = "HTTP/1.1"
    chunks: dict = {}
    delay = 0.0
    hits = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        hx = path[len("/chunk/"):] if path.startswith("/chunk/") else ""
        type(self).hits += 1
        if type(self).delay:
            time.sleep(type(self).delay)
        data = type(self).chunks.get(hx)
        if data is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionResetError, BrokenPipeError):
            pass


def start_slow_peer(data, man, delay=0.0):
    SlowPeer.chunks = {hx: data[off:off + ln] for off, ln, hx in man.chunks}
    SlowPeer.delay = delay
    SlowPeer.hits = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), SlowPeer)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class PrefillGuardTests(unittest.TestCase):
    """Esbaz onbellegi yalniz acik bildirimle, kosu kimligiyle ve kisa
    butceyle calisir; pause/restart hazirligi bozmaz."""

    def _env_job(self, root, cfg_mut=None):
        env = AppEnv(root=root / "env")
        cfg = env.load_config()
        cfg["peer_cache"] = True
        cfg["checksum_sidecar"] = False
        if cfg_mut:
            cfg_mut(cfg)
        return env, cfg

    def _open_part(self, job):
        job.target_dir.mkdir(parents=True, exist_ok=True)
        job._fd = os.open(job.part_path, os.O_RDWR | os.O_CREAT, 0o644)
        os.ftruncate(job._fd, job.total)
        self.addCleanup(lambda: job._close_fd())

    def test_auto_manifest_never_uses_peers(self):
        root = tmp_root(self, "ferdim-peer-auto-")
        _, man, _ = prepare_source(root, SMALL_DATA)
        env, cfg = self._env_job(root)
        bad_srv, bad_port = start_bad_peer(man.chunks[0][1])
        self.addCleanup(bad_srv.server_close)
        self.addCleanup(bad_srv.shutdown)
        node = peer.open_peer(env)
        node.discovery.enabled = False
        node.set_peers([("127.0.0.1", bad_port)])
        job = DownloadJob(env, cfg, JobSpec(url="http://ornek.test/f.bin"))
        job.kind = "http"
        job.total = man.size
        job._merkle = man
        job._merkle_auto = True       # kendiliginden bulunan yan dosya
        job._pending = [(0, man.size - 1)]
        self._open_part(job)
        job._peer_prefill(job.spec.options(cfg))
        self.assertEqual(BadPeer.hits, 0, "auto bildirim esbazi kullanilmamali")
        self.assertEqual(job._pending, [(0, man.size - 1)])
        self.assertEqual(job.done, 0)
        self.assertNotIn("peer_chunks", job.extra)

    def test_explicit_prefill_rebases_speed_mark(self):
        root = tmp_root(self, "ferdim-peer-mark-")
        _, man, man_path = prepare_source(root, SMALL_DATA)
        srv, port = start_slow_peer(SMALL_DATA, man)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        env, cfg = self._env_job(root)
        node = peer.open_peer(env)
        node.discovery.enabled = False
        node.set_peers([("127.0.0.1", port)])
        job = DownloadJob(env, cfg, JobSpec(url="http://ornek.test/f.bin",
                                           merkle=str(man_path)))
        job.kind = "http"
        job.total = man.size
        job._merkle = man
        job._merkle_auto = False
        job._pending = [(0, man.size - 1)]
        job._speed_done_mark = 0
        self._open_part(job)
        job._peer_prefill(job.spec.options(cfg))
        self.assertEqual(job.done, man.size)
        self.assertEqual(job._pending, [])
        self.assertEqual(job._speed_done_mark, job.done,
                         "esbaz baytlari hiz ornegine sicramamali")
        self.assertEqual(job.extra.get("peer_chunks"), len(man.chunks))

    def test_old_run_result_is_dropped(self):
        """Eski hazirlik, yeni kosu baslarken `_pending`/`done` yazamaz."""
        root = tmp_root(self, "ferdim-peer-run-")
        _, man, man_path = prepare_source(root, SMALL_DATA)
        env, cfg = self._env_job(root)
        job = DownloadJob(env, cfg, JobSpec(url="http://ornek.test/f.bin",
                                           merkle=str(man_path)))
        job.kind = "http"
        job.total = man.size
        job._merkle = man
        job._merkle_auto = False
        job._pending = [(0, man.size - 1)]
        self._open_part(job)

        class FakeNode:
            def fetch_chunks(self, man, pending, fd, stop=None, budget=None,
                             write=None):
                off, ln, _ = man.chunks[0]
                wrote = write(off, b"x" * ln)     # hala eski kosu
                self.job._run += 1                # bu arada yeni kosu basladi
                return [(off, ln)] if wrote else []

        fake = FakeNode()
        fake.job = job
        with mock.patch("ferdim.peer.open_peer", return_value=fake):
            job._peer_prefill(job.spec.options(cfg))
        self.assertEqual(job._pending, [(0, man.size - 1)],
                         "eski kosunun araliklari uygulanmamali")
        self.assertEqual(job.done, 0)
        self.assertNotIn("peer_chunks", job.extra)

    def test_prefill_budget_is_below_pause_wait(self):
        self.assertLessEqual(jobmod.PEER_PREFILL_BUDGET, 4.0)
        self.assertLess(jobmod.PEER_PREFILL_BUDGET, 8.0)


class PrefillLifecycleTests(unittest.TestCase):
    """Hazirlik icindeki pause/restart: is duraklamis kalir ve devam eder."""

    @classmethod
    def setUpClass(cls):
        cls.origin, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.origin.shutdown()

    def _started_job(self, root, man_path, port):
        env = AppEnv(root=root / "env")
        cfg = env.load_config()
        cfg["peer_cache"] = True
        cfg["checksum_sidecar"] = False
        cfg["max_concurrent"] = 4
        node = peer.open_peer(env)
        node.discovery.enabled = False
        node.set_peers([("127.0.0.1", port)])
        job = DownloadJob(env, cfg, JobSpec(
            url=f"{self.base}/file.bin", segments=2, merkle=str(man_path)))
        starter = threading.Thread(target=job.start, daemon=True)
        starter.start()
        self.assertTrue(self._wait(lambda: SlowPeer.hits > 0, 20),
                        "esbaz taramasi baslamadi")
        return job, starter

    @staticmethod
    def _wait(pred, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.02)
        return bool(pred())

    def test_pause_during_prefill_folds_and_resume_completes(self):
        root = tmp_root(self, "ferdim-peer-pause-")
        _, man, man_path = prepare_source(root)
        srv, port = start_slow_peer(DATA, man, delay=0.25)
        try:
            job, starter = self._started_job(root, man_path, port)
            t0 = time.monotonic()
            job.pause()
            self.assertLess(time.monotonic() - t0, 8.0,
                            "prefill pause penceresini asti")
            self.assertEqual(job.state, JobState.PAUSED,
                             f"pause dondu ama is {job.state}")
            starter.join(10)
            job.resume()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR),
                                       60), f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(sha(job.final_path.read_bytes()), sha(DATA))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_restart_during_prefill_ends_with_correct_file(self):
        root = tmp_root(self, "ferdim-peer-restart-")
        _, man, man_path = prepare_source(root)
        srv, port = start_slow_peer(DATA, man, delay=0.25)
        try:
            job, starter = self._started_job(root, man_path, port)
            job.cancel(delete_files=False)
            starter.join(10)
            self.assertFalse(starter.is_alive(), "eski kosu bitmedi")
            # kuyruk 'restart' yolu: yarim dosya silinir, is sifirlanir
            job.part_path.unlink(missing_ok=True)
            job.state = JobState.QUEUED
            job.error = ""
            job.total, job.done = -1, 0
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR),
                                       60), f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(sha(job.final_path.read_bytes()), sha(DATA),
                             "restart sonrasi dosya bozuk (eski kosu sizdi)")
        finally:
            srv.shutdown()
            srv.server_close()


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.origin.shutdown()

    def _cfg(self, env):
        cfg = env.load_config()
        cfg["peer_cache"] = True
        cfg["checksum_sidecar"] = False      # yalnizca aralik istekleri sayilsin
        cfg["max_concurrent"] = 4
        return cfg

    def test_peer_fills_every_chunk_origin_only_probed(self):
        root = tmp_root(self, "ferdim-peer-e2e-")
        src, man, man_path = prepare_source(root)
        # B dugumu: parcalari sunar (multicast kapali)
        env_b = AppEnv(root=root / "b")
        node_b = peer.PeerNode(env_b, multicast=False, host="127.0.0.1")
        self.assertTrue(node_b.index.add(man, src))
        srv_b = peer.PeerServer(node_b.index, host="127.0.0.1", port=0)
        srv_b.start()
        try:
            env_a = AppEnv(root=root / "a")
            node_a = peer.open_peer(env_a)
            node_a.discovery.enabled = False
            node_a.set_peers([("127.0.0.1", srv_b.port)])
            job = DownloadJob(
                env_a, self._cfg(env_a),
                JobSpec(url=f"{self.base}/file.bin", segments=2,
                        merkle=str(man_path)))
            port = int(self.base.rsplit(":", 1)[1])
            before = REQUEST_COUNTS.get(port, 0)
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(sha(job.final_path.read_bytes()), sha(DATA))
            self.assertEqual(job.extra.get("peer_chunks"), len(man.chunks))
            self.assertEqual(job.extra.get("peer_bytes"), len(DATA))
            # kaynaktan yalnizca probe geldi (bayt araligi istenmedi)
            self.assertEqual(REQUEST_COUNTS.get(port, 0) - before, 1)
            # dogrulanmis dosya esbaz indeksine yazildi
            self.assertTrue(node_a.index.has_root(man.root))
        finally:
            srv_b.stop()

    def test_bad_peer_chunk_is_rejected_and_origin_used(self):
        root = tmp_root(self, "ferdim-peer-bad-")
        _, man, man_path = prepare_source(root)
        bad_srv, bad_port = start_bad_peer(man.chunks[0][1])
        try:
            env = AppEnv(root=root / "a")
            node = peer.open_peer(env)
            node.discovery.enabled = False
            node.set_peers([("127.0.0.1", bad_port)])
            job = DownloadJob(
                env, self._cfg(env),
                JobSpec(url=f"{self.base}/file.bin", segments=4,
                        merkle=str(man_path)))
            port = int(self.base.rsplit(":", 1)[1])
            before = REQUEST_COUNTS.get(port, 0)
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(sha(job.final_path.read_bytes()), sha(DATA))
            # bozuk esbaz parcasi yazilmadi: tum baytlar kaynaktan geldi
            self.assertNotIn("peer_chunks", job.extra)
            self.assertGreater(REQUEST_COUNTS.get(port, 0) - before, 1)
            self.assertGreater(BadPeer.hits, 0)
        finally:
            bad_srv.shutdown()
            bad_srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
