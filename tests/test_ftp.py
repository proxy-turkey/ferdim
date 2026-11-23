"""FTP/FTPS motor testleri: yerel is parcacikli FTP sunucusu (stdlib).

Sunucu yalnizca testin ihtiyaci olan komutlari bilir:
USER/PASS/TYPE/SIZE/PASV/EPSV/REST/RETR/QUIT.
"""
from __future__ import annotations

import ftplib
import errno
import hashlib
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import api, util
from ferdim.engine import ftp as ftp_mod
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from ferdim.ratelimit import TokenBucket

DATA = os.urandom(768 * 1024 + 321)
PAUSE_DATA = os.urandom(1536 * 1024)


def wait_until(pred, timeout: float = 20.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _Handler(socketserver.StreamRequestHandler):
    def _reply(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8") + b"\r\n")
        self.wfile.flush()

    def _pasv(self) -> None:
        self._close_pasv()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        s.settimeout(10)
        self._listener = s

    def _close_pasv(self) -> None:
        if getattr(self, "_listener", None) is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None

    def setup(self) -> None:
        super().setup()
        self._listener = None
        self._rest = 0
        self._user = ""
        self._logged = False

    def finish(self) -> None:
        self._close_pasv()
        super().finish()

    def handle(self) -> None:
        self._reply("220 ferdim test FTP")
        while True:
            try:
                line = self.rfile.readline()
            except (ConnectionError, OSError):
                return
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            cmd, _, arg = text.partition(" ")
            try:
                if not self._dispatch(cmd.upper(), arg):
                    return
            except (ConnectionError, OSError):
                return

    def _dispatch(self, cmd: str, arg: str) -> bool:
        srv = self.server
        if cmd == "USER":
            self._user = arg
            with srv.lock:
                srv.users.append(arg)
            self._reply("331 password required")
        elif cmd == "PASS":
            self._logged = srv.user is None or (
                self._user == srv.user and arg == srv.password)
            self._reply("230 logged in" if self._logged
                        else "530 login incorrect")
        elif cmd == "TYPE":
            self._reply("200 type set")
        elif cmd == "SIZE":
            if arg not in srv.files:
                self._reply("550 no such file")
            elif not srv.support_size:
                self._reply("502 SIZE not implemented")
            else:
                self._reply(f"213 {len(srv.files[arg])}")
        elif cmd == "MDTM":
            if arg not in srv.files:
                self._reply("550 no such file")
            elif not srv.support_mdtm:
                self._reply("502 MDTM not implemented")
            else:
                self._reply(f"213 {srv.mtimes.get(arg, '20260101000000')}")
        elif cmd in ("PASV", "EPSV"):
            self._pasv()
            port = self._listener.getsockname()[1]
            if cmd == "PASV":
                self._reply(f"227 Entering Passive Mode (127,0,0,1,"
                            f"{port >> 8},{port & 255})")
            else:
                self._reply(f"229 Entering Extended Passive Mode (|||{port}|)")
        elif cmd == "REST":
            if not srv.support_rest:
                self._reply("501 restart not implemented")
                return True
            try:
                self._rest = int(arg)
            except ValueError:
                self._reply("501 bad offset")
                return True
            with srv.lock:
                srv.rest_calls.append(self._rest)
            self._reply("350 restart accepted")
        elif cmd == "RETR":
            if not self._logged:
                self._reply("530 login required")
                return True
            if arg not in srv.files:
                self._reply("550 no such file")
                return True
            with srv.lock:
                n = srv.retr_counts.get(arg, 0) + 1
                srv.retr_counts[arg] = n
            if n == 1 and srv.fail_first_retr:
                self._close_pasv()
                self._reply(f"{srv.fail_first_retr} gecici hata")
                return False
            if self._listener is None:
                self._reply("425 use PASV first")
                return True
            try:
                data, _ = self._listener.accept()
            except (socket.timeout, OSError):
                self._close_pasv()
                self._reply("425 data connection failed")
                return True
            self._close_pasv()
            self._reply("150 opening data connection")
            body = srv.files[arg][self._rest:]
            self._rest = 0
            cut = srv.abort_first_after \
                if (n <= srv.abort_count and srv.abort_first_after) else None
            stall_cut = srv.stall_first_after \
                if (n == 1 and srv.stall_first_after) else None
            aborted = False
            try:
                if stall_cut is not None:
                    data.sendall(body[:stall_cut])
                    time.sleep(srv.stall_seconds)
                elif cut is not None:
                    data.sendall(body[:cut])
                else:
                    for i in range(0, len(body), 32768):
                        data.sendall(body[i:i + 32768])
            except (ConnectionError, OSError):
                aborted = True
            finally:
                try:
                    data.close()
                except OSError:
                    pass
            if stall_cut is not None:
                return False                # kontrolu kapat, 226 yok
            if cut is not None:
                self._reply("426 transfer aborted")
            elif srv.drop_control_first and n == 1:
                return False                # son yanit gelmez (EOFError)
            elif aborted:
                try:
                    self._reply("426 transfer aborted")
                except (ConnectionError, OSError):
                    return False
            else:
                self._reply("226 transfer complete")
        elif cmd == "QUIT":
            self._reply("221 bye")
            return False
        elif cmd == "NOOP":
            self._reply("200 ok")
        else:
            self._reply("502 not implemented")
        return True


class FtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, files: dict[str, bytes], user: str | None = None,
                 password: str = "", support_size: bool = True,
                 support_rest: bool = True, support_mdtm: bool = True,
                 fail_first_retr: int = 0, abort_first_after: int = 0,
                 abort_count: int = 1, stall_first_after: int = 0,
                 stall_seconds: float = 7.0,
                 drop_control_first: bool = False):
        self.files = files
        self.user = user
        self.password = password
        self.support_size = support_size
        self.support_rest = support_rest
        self.support_mdtm = support_mdtm
        self.fail_first_retr = fail_first_retr
        self.abort_first_after = abort_first_after
        self.abort_count = abort_count
        self.stall_first_after = stall_first_after
        self.stall_seconds = stall_seconds
        self.drop_control_first = drop_control_first
        self.mtimes: dict[str, str] = {}
        self.retr_counts: dict[str, int] = {}
        self.rest_calls: list[int] = []
        self.users: list[str] = []
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def handle_error(self, request, client_address):
        pass                     # istemci iptali test ciktisini kirletmesin

    @property
    def base(self) -> str:
        return f"ftp://127.0.0.1:{self.server_address[1]}"

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class FtpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FtpServer({"/test dosya.bin": DATA, "/pause.bin": PAUSE_DATA},
                            user="kullanici", password="giz li")
        cls.base = cls.srv.base
        cls.auth_base = cls.srv.base.replace(
            "ftp://", "ftp://kullanici:giz%20li@")

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def setUp(self):
        self.env = AppEnv(root=Path(tempfile.mkdtemp(prefix="ferdim-ftp-")))

    def _job(self, url: str, **kw) -> DownloadJob:
        return DownloadJob(self.env, self.env.load_config(),
                           JobSpec(url=url, **kw))

    def _job_cfg(self, url: str, **cfgkw) -> DownloadJob:
        cfg = self.env.load_config()
        cfg.update(cfgkw)
        return DownloadJob(self.env, cfg, JobSpec(url=url))

    def _wait(self, job, states, timeout=30) -> bool:
        ok = wait_until(lambda: job.state in states, timeout)
        self.assertTrue(ok, f"durum: {job.state} {job.error}")
        return ok

    def test_01_full_download_with_credentials(self):
        url = self.base.replace("ftp://", "ftp://kullanici:giz%20li@") \
            + "/test%20dosya.bin"
        job = self._job(url)
        self.addCleanup(job.cancel)
        job.start()
        self._wait(job, (JobState.DONE, JobState.ERROR))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.kind, "ftp")
        self.assertEqual(job.total, len(DATA))
        self.assertEqual(job.done, len(DATA))
        self.assertEqual(job.final_filename, "test dosya.bin")
        self.assertEqual(sha(job.final_path), hashlib.sha256(DATA).hexdigest())
        self.assertIn("kullanici", self.srv.users)

    def test_02_resume_from_part_offset(self):
        offset = len(DATA) // 3
        job = self._job(f"{self.auth_base}/test%20dosya.bin")
        self.addCleanup(job.cancel)
        job.part_path.parent.mkdir(parents=True, exist_ok=True)
        job.part_path.write_bytes(DATA[:offset])
        util.atomic_write_json(job.meta_path, {
            "url": job.url, "kind": "ftp", "total": len(DATA),
            "done": offset, "ftp_mtime": "20260101000000"})
        job.start()
        self._wait(job, (JobState.DONE, JobState.ERROR))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.done, len(DATA))
        self.assertEqual(sha(job.final_path), hashlib.sha256(DATA).hexdigest())
        self.assertIn(offset, self.srv.rest_calls,
                      "REST ile devam edilmedi")

    def test_03_pause_resume(self):
        job = self._job(f"{self.auth_base}/pause.bin", limit=64 * 1024)
        self.addCleanup(job.cancel)
        job.start()
        self.assertTrue(wait_until(lambda: job.done >= 100_000, 20),
                        f"ilerleme yok: {job.done}")
        job.pause()
        self.assertEqual(job.state, JobState.PAUSED)
        mid = job.done
        self.assertGreater(mid, 0)
        time.sleep(0.3)
        self.assertEqual(job.done, mid, "duraklamadan sonra ilerleme var")
        self.assertEqual(job.part_path.stat().st_size, mid)
        job._bucket.set_rate(0)          # devami hizli bitir
        job.resume()
        self._wait(job, (JobState.DONE, JobState.ERROR))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(PAUSE_DATA).hexdigest())
        self.assertIn(mid, self.srv.rest_calls)

    def test_04_missing_path_is_job_error(self):
        job = self._job(f"{self.auth_base}/yok.bin")
        self.addCleanup(job.cancel)
        job.start()
        self._wait(job, (JobState.ERROR,))
        self.assertEqual(job.state, JobState.ERROR)
        self.assertIn("bulunamadi", job.error)
        self.assertIn("FTP 550", job.error)
        self.assertEqual(job.error_code, "not_found")

    def test_05_anonymous_when_no_userinfo(self):
        srv = FtpServer({"/anon.bin": DATA[:4096]})
        self.addCleanup(srv.close)
        job = self._job(f"{srv.base}/anon.bin")
        self.addCleanup(job.cancel)
        job.start()
        self._wait(job, (JobState.DONE, JobState.ERROR))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(DATA[:4096]).hexdigest())
        self.assertIn("anonymous", srv.users)

    def test_06_size_unsupported_streams_to_eof(self):
        srv = FtpServer({"/nosize.bin": DATA[:8192]}, support_size=False)
        self.addCleanup(srv.close)
        job = self._job(f"{srv.base}/nosize.bin")
        self.addCleanup(job.cancel)
        job.start()
        self._wait(job, (JobState.DONE, JobState.ERROR))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.total, 8192)     # EOF gercek sondur
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(DATA[:8192]).hexdigest())

    def test_08_cancel_stops_transfer(self):
        job = self._job(f"{self.auth_base}/pause.bin", limit=64 * 1024)
        self.addCleanup(job.cancel)
        job.start()
        self.assertTrue(wait_until(lambda: job.done >= 100_000, 20),
                        f"ilerleme yok: {job.done}")
        job.cancel(delete_files=True)
        self.assertEqual(job.state, JobState.CANCELED)
        self.assertFalse(job.part_path.exists())
        mid = job.done
        time.sleep(0.3)
        self.assertEqual(job.done, mid, "iptalden sonra ilerleme var")

    def test_09_rest_unsupported_restarts_from_zero(self):
        srv = FtpServer({"/norest.bin": DATA[:9000]}, support_rest=False)
        self.addCleanup(srv.close)
        job = self._job(f"{srv.base}/norest.bin")
        self.addCleanup(job.cancel)
        job.part_path.parent.mkdir(parents=True, exist_ok=True)
        job.part_path.write_bytes(DATA[:3000])
        job.start()
        self._wait(job, (JobState.DONE, JobState.ERROR))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(DATA[:9000]).hexdigest())
        self.assertEqual(srv.rest_calls, [])

    def test_10_partial_426_without_size_is_not_done(self):
        """SIZE yokken 426 ile kesilen aktarim DONE'a donusmemeli."""
        srv = FtpServer({"/trunc.bin": DATA[:16384]}, support_size=False,
                        abort_first_after=4096)
        self.addCleanup(srv.close)
        job = self._job_cfg(f"{srv.base}/trunc.bin", retries=0)
        self.addCleanup(job.cancel)
        job.start()
        self._wait(job, (JobState.DONE, JobState.ERROR))
        self.assertEqual(job.state, JobState.ERROR)
        self.assertNotEqual(job.state, JobState.DONE)
        self.assertIn("426", job.error)
        self.assertEqual(job.error_code, "network")
        self.assertEqual(job.done, 4096)
        self.assertEqual(job.total, -1)

    def test_11_partial_426_retries_and_resumes(self):
        """Gecici 426: REST ile kaldigi yerden devam, sonunda DONE."""
        srv = FtpServer({"/trunc2.bin": DATA[:20000]}, support_size=False,
                        abort_first_after=4096)
        self.addCleanup(srv.close)
        job = self._job(f"{srv.base}/trunc2.bin")
        self.addCleanup(job.cancel)
        with mock.patch("ferdim.engine.job.backoff_delay", return_value=0.0):
            job.start()
            self._wait(job, (JobState.DONE, JobState.ERROR), timeout=30)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.total, 20000)
        self.assertEqual(job.done, 20000)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(DATA[:20000]).hexdigest())
        self.assertIn(4096, srv.rest_calls)
        self.assertEqual(srv.retr_counts["/trunc2.bin"], 2)

    def test_12_transient_421_retries(self):
        """Ilk RETR 421 (gecici): yeni baglantiyla yeniden denenir."""
        srv = FtpServer({"/flaky.bin": DATA[:5000]}, fail_first_retr=421)
        self.addCleanup(srv.close)
        job = self._job(f"{srv.base}/flaky.bin")
        self.addCleanup(job.cancel)
        with mock.patch("ferdim.engine.job.backoff_delay", return_value=0.0):
            job.start()
            self._wait(job, (JobState.DONE, JobState.ERROR), timeout=30)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(srv.retr_counts["/flaky.bin"], 2)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(DATA[:5000]).hexdigest())

    def test_13_spec_credentials_fallback(self):
        """URL'de kimlik yoksa spec.user/password kullanilir."""
        job = self._job(f"{self.base}/test%20dosya.bin",
                        user="kullanici", password="giz li")
        self.addCleanup(job.cancel)
        job.start()
        self._wait(job, (JobState.DONE, JobState.ERROR))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path), hashlib.sha256(DATA).hexdigest())
        self.assertIn("kullanici", self.srv.users)

    def test_14_snapshot_redacts_passwords(self):
        url = "ftp://kullanici:gizli@example.org/x.bin"
        job = self._job(url, mirrors=["http://m:secret@example.org/m.bin"])
        snap = job.snapshot()
        self.assertEqual(snap["url"], "ftp://kullanici@example.org/x.bin")
        self.assertNotIn("gizli", snap["url"])
        self.assertEqual(snap["mirror_health"],
                         [{"url": "http://m@example.org/m.bin",
                           "state": "ok", "code": ""}])
        self.assertEqual(job.url, url)          # ic kayit degismedi
        self.assertEqual(job.spec.url, url)
        bad = util.redact_url("ftp://u:gizli@example.org:bozuk/x")
        self.assertNotIn("gizli", bad)
        self.assertEqual(bad, "ftp://u@example.org/x")

    def test_15_disk_error_maps_to_disk(self):
        srv = FtpServer({"/disk.bin": DATA[:4096]})
        self.addCleanup(srv.close)
        fd, path = tempfile.mkstemp(prefix="ferdim-ftp-disk-")
        try:
            with mock.patch("ferdim.engine.ftp.os.pwrite",
                            side_effect=OSError(errno.ENOSPC, "dolu")):
                with self.assertRaises(ftp_mod.FtpError) as cm:
                    ftp_mod.download(
                        f"{srv.base}/disk.bin", fd, 0,
                        stop=threading.Event(), bucket=TokenBucket(0),
                        progress=lambda n: None, stalled=5)
            self.assertEqual(cm.exception.code, "disk")
            self.assertFalse(cm.exception.transient)
        finally:
            os.close(fd)
            Path(path).unlink(missing_ok=True)

    def test_16_mdtm_change_restarts_from_zero(self):
        srv = FtpServer({"/mdtm.bin": DATA[:12000]})
        srv.mtimes["/mdtm.bin"] = "20260101000000"
        self.addCleanup(srv.close)
        job = self._job(f"{srv.base}/mdtm.bin")
        self.addCleanup(job.cancel)
        job.part_path.parent.mkdir(parents=True, exist_ok=True)
        job.part_path.write_bytes(DATA[:4000])
        util.atomic_write_json(job.meta_path, {
            "url": job.url, "kind": "ftp", "total": 12000,
            "done": 4000, "ftp_mtime": "20200101000000"})
        job.start()
        self._wait(job, (JobState.DONE, JobState.ERROR))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(DATA[:12000]).hexdigest())
        self.assertEqual(srv.rest_calls, [])    # bastan indirildi

    def test_17_restart_reprobes_size(self):
        with mock.patch.object(QueueManager, "_prober_loop", lambda self: None):
            qm = QueueManager(self.env, self.env.load_config())
        self.addCleanup(qm.shutdown)
        r = api.handle({"qm": qm, "env": self.env, "cfg": qm.cfg,
                        "set_cfg": self.env.save_config},
                       "add", {"url": f"{self.base}/test%20dosya.bin",
                               "paused": True})
        self.assertTrue(r["ok"], r)
        job = qm.get(r["id"])
        qm._size_probed.add(job.id)
        job.total = 123
        qm.control(job.id, "restart")
        self.assertNotIn(job.id, qm._size_probed)
        self.assertEqual(job.total, -1)
        mode = (self.env.state_dir / "queue.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_18_553_is_unknown(self):
        err = ftp_mod._wrap(ftplib.error_perm("553 gecersiz ad"))
        self.assertEqual(err.code, "unknown")
        self.assertFalse(err.transient)

    def test_19_stall_is_retried_and_resumed(self):
        """RETR sirasinda akis durursa stall gecici sayilir; REST ile devam."""
        srv = FtpServer({"/stall.bin": DATA[:16384]}, stall_first_after=4096,
                        stall_seconds=7.0)
        self.addCleanup(srv.close)
        job = self._job_cfg(f"{srv.base}/stall.bin", stall_timeout=5,
                            timeout=5)
        self.addCleanup(job.cancel)
        with mock.patch("ferdim.engine.job.backoff_delay", return_value=0.0):
            job.start()
            self._wait(job, (JobState.DONE, JobState.ERROR), timeout=40)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(DATA[:16384]).hexdigest())
        self.assertIn(4096, srv.rest_calls)
        self.assertEqual(srv.retr_counts["/stall.bin"], 2)

    def test_20_control_drop_after_data_is_retried(self):
        """Veri bitti ama kontrol kanali son yaniti vermeden kapandi."""
        srv = FtpServer({"/drop.bin": DATA[:5000]}, drop_control_first=True)
        self.addCleanup(srv.close)
        job = self._job(f"{srv.base}/drop.bin")
        self.addCleanup(job.cancel)
        with mock.patch("ferdim.engine.job.backoff_delay", return_value=0.0):
            job.start()
            self._wait(job, (JobState.DONE, JobState.ERROR), timeout=30)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(srv.retr_counts["/drop.bin"], 2)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(DATA[:5000]).hexdigest())

    def test_21_attempt_budget_resets_on_progress(self):
        """retries=1 ama her deneme ilerliyor: butce sifirlanir, is biter."""
        srv = FtpServer({"/hickup.bin": DATA[:20000]},
                        abort_first_after=4096, abort_count=2)
        self.addCleanup(srv.close)
        job = self._job_cfg(f"{srv.base}/hickup.bin", retries=1)
        self.addCleanup(job.cancel)
        with mock.patch("ferdim.engine.job.backoff_delay", return_value=0.0):
            job.start()
            self._wait(job, (JobState.DONE, JobState.ERROR), timeout=30)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(srv.retr_counts["/hickup.bin"], 3)
        self.assertEqual(job.done, 20000)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(DATA[:20000]).hexdigest())

    def test_22_550_permission_denied_is_forbidden(self):
        denied = ftp_mod._wrap(ftplib.error_perm("550 Permission denied"))
        self.assertEqual(denied.code, "forbidden")
        self.assertIn("erisim engellendi", str(denied))
        missing = ftp_mod._wrap(ftplib.error_perm("550 No such file"))
        self.assertEqual(missing.code, "not_found")
        self.assertIn("bulunamadi", str(missing))
        self.assertEqual(
            ftp_mod._wrap(ftplib.error_perm("551 Permission denied")).code,
            "forbidden")

    def test_23_atomic_write_json_is_private(self):
        with tempfile.TemporaryDirectory(prefix="ferdim-state-") as td:
            p = Path(td) / "job.json"
            util.atomic_write_json(p, {"url": "ftp://u:gizli@h/x"})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            # eski/cokmus gecici dosya yazimi kilitlememeli
            p.with_suffix(".json.tmp").write_text("bozuk", "utf-8")
            util.atomic_write_json(p, {"a": 1})
            self.assertEqual(util.read_json(p), {"a": 1})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_07_api_accepts_ftp_schemes(self):
        cfg = self.env.load_config()
        qm = QueueManager(self.env, cfg)
        self.addCleanup(qm.shutdown)
        ctx = {"qm": qm, "env": self.env, "cfg": cfg,
               "set_cfg": self.env.save_config}
        for scheme in ("ftp", "ftps"):
            r = api.handle(ctx, "add",
                           {"url": f"{scheme}://127.0.0.1:1/x.bin",
                            "paused": True})
            self.assertTrue(r["ok"], (scheme, r))
            self.assertTrue(qm.get(r["id"]).spec.url.startswith(scheme))
        self.assertFalse(api.handle(ctx, "add",
                                    {"url": "file:///etc/passwd"})["ok"])


if __name__ == "__main__":
    unittest.main()
