"""Olcum paketleri: motor, parcalayici, kuyruk, istatistik, bosta daemon.

Her paket kendi gecici kokunu kurar ve finally'de siler; geride surec,
soket ya da /tmp/ferdim-bench-* birakmaz. Ag yalnizca loopback'tir.

Motor olcumu: Range destekli yerel ThreadingHTTPServer seyrek (sparse)
bir dosyayi servis eder; indirme gercek DownloadJob + AppEnv(root=...)
ile yapilir. Kuyruk olcumu duraklatilmis isler kullanir: ne yoklayici
ne de zamanlayici aga cikar (prober yalniz KUYRUK'taki isleri yoklar).
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ferdim import cdc
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_PREFIX = "ferdim-bench-"

ENGINE_FULL = 256 << 20
ENGINE_QUICK = 64 << 20
CDC_FULL = 64 << 20
CDC_QUICK = 16 << 20
QUEUE_FULL = 10_000
QUEUE_QUICK = 2_000
STATS_CALLS = 9             # 10 000 isli stats() pahali: en iyi tur icin 9 yeter
IDLE_FULL = 10.0
IDLE_QUICK = 5.0

# sabit kalibrasyon isi: her makinede ayni is, hiz puani = is/saniye.
# Bu is yuku DEGISIRSE BASELINE_VERSION artirilmali: eski tabanlardaki
# kalibrasyon puani yeni isle karsilastirilamaz.
_CALIB_BLOCK = hashlib.shake_256(b"ferdim-bench-calibration").digest(1 << 20)


def calibration(rounds: int = 5) -> float:
    """Makine hizi puani (yuksek = hizli). Hiz metrikleri buna bolunur.

    Sabit is: 4 MiB sha256 + 200 000 adimlik saf Python dongusu. En iyi
    tur alinir (en kisa sure): tek seferlik gecikmeler puani dusurmesin.
    """
    best = float("inf")
    for _ in range(max(1, rounds)):
        t0 = time.perf_counter()
        h = hashlib.sha256()
        for _ in range(4):
            h.update(_CALIB_BLOCK)
        acc = int.from_bytes(h.digest()[:8], "little")
        for i in range(200_000):        # saf Python dongusu
            acc = (acc * 31 + i) & 0xFFFFFFFF
        best = min(best, time.perf_counter() - t0)
    return 1.0 / best if best > 0 else 0.0


# ------------------------------------------------------------------- motor
class _BenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, addr, payload: Path, size: int):
        self.payload = payload
        self.size = size
        super().__init__(addr, _RangeHandler)


class _RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head: bool):
        if urlsplit(self.path).path != "/bench.bin":
            self.send_error(404)
            return
        size = self.server.size
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            first, last = rng[6:].split("-", 1)
            start = int(first) if first else 0
            end = int(last) if last else size - 1
            end = min(end, size - 1)
            if start >= size or start > end:
                self.send_error(416)
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            start, end = 0, size - 1
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"ferdim-bench"')
        self.end_headers()
        if head:
            return
        try:
            with open(self.server.payload, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(256 << 10, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError:
            pass                          # istemci kapatti: olcum surer


def _wait_job(job, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.state in (JobState.DONE, JobState.ERROR, JobState.CANCELED):
            return
        time.sleep(0.01)
    raise RuntimeError("is zaman asimi: durum %s" % job.state)


def _download(job, size: int) -> float:
    """Indirmeyi calistirir; TRANSFER suresini (sn) doner.

    Sure, tum baytlar `.part` dosyasina yazildigi anda durur: bitisteki
    fsync/rename ve onu izleyen dogrulama disarida kalir. Boylece olcum
    aktarim hizidir; depolama birimi hizi (CI kosucusunda disk, burada
    tmpfs) metrigi kaydirmaz. Is yine DONE'a kadar beklenir ve
    dogrulanir.
    """
    t0 = time.perf_counter()
    job.start()
    deadline = time.monotonic() + 180.0
    transfer = None
    while time.monotonic() < deadline:
        if size > 0 and job.done >= size:
            transfer = time.perf_counter() - t0
            break
        if job.state in (JobState.DONE, JobState.ERROR, JobState.CANCELED):
            transfer = time.perf_counter() - t0
            break
        time.sleep(0.005)
    _wait_job(job)
    if transfer is None:
        raise RuntimeError("is zaman asimi: durum %s" % job.state)
    return transfer


def _serve(payload, size, ready) -> None:
    """Alt surecte calisan sunucu: GIL'i istemci is parcaciklariyla
    paylasmaz; olcum sunucu degil istemci motorunu yansitir.
    """
    srv = _BenchServer(("127.0.0.1", 0), Path(payload), size)
    ready.put(srv.server_address[1])
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


def bench_engine(quick: bool) -> dict:
    """Yerel sunucudan 8 baglantili indirme: MiB/s (en iyi tur)."""
    size = ENGINE_QUICK if quick else ENGINE_FULL
    reps = 2 if quick else 3
    payload_root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX))
    proc = None
    try:
        payload = payload_root / "payload.bin"
        with open(payload, "wb") as f:
            f.truncate(size)              # seyrek dosya: disk/RAM harcamaz
        ctx = multiprocessing.get_context()
        ready = ctx.Queue()
        proc = ctx.Process(target=_serve, args=(str(payload), size, ready),
                           daemon=True)
        proc.start()
        try:
            port = ready.get(timeout=30)
        except queue.Empty:
            raise RuntimeError("yerel sunucu baslamadi")
        finally:
            ready.close()
        url = "http://127.0.0.1:%d/bench.bin" % port
        samples = []
        for _ in range(reps):
            root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX))
            job = None
            try:
                env = AppEnv(root=root)
                cfg = env.load_config()
                cfg.update({"speed_limit": 0, "retries": 1,
                            "stall_timeout": 15, "learn_hosts": False,
                            "checksum_sidecar": False})
                job = DownloadJob(env, cfg, JobSpec(
                    url=url, filename="bench.bin", segments=8))
                elapsed = _download(job, size)
                if job.state != JobState.DONE or job.done != size:
                    raise RuntimeError(
                        "indirme bitmedi: durum=%s done=%s/%s (%.3f sn)"
                        % (job.state, job.done, size, elapsed))
                if job.final_path.stat().st_size != size:
                    raise RuntimeError("hedef dosya boyu yanlis")
                samples.append((size / (1 << 20)) / elapsed)
            finally:
                if job is not None and job.state in (JobState.CONNECTING,
                                                     JobState.DOWNLOADING):
                    job.pause()
                shutil.rmtree(root, ignore_errors=True)
        return {"engine_mib_s": max(samples)}
    finally:
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(10)
            if proc.is_alive():
                proc.kill()
                proc.join(10)
        shutil.rmtree(payload_root, ignore_errors=True)


# -------------------------------------------------------------- parcalayici
def _write_pattern(path: Path, size: int) -> None:
    """Belirlenimci sahte-rastgele icerik (1 MiB blok, sabit tohum)."""
    block = hashlib.shake_256(b"ferdim-bench-cdc").digest(1 << 20)
    with open(path, "wb") as f:
        for _ in range(size >> 20):
            f.write(block)


def bench_chunker(quick: bool) -> dict:
    """FastCDC + sha256 (make_manifest): MiB/s (en iyi tur)."""
    size = CDC_QUICK if quick else CDC_FULL
    reps = 2 if quick else 3
    root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX))
    try:
        path = root / "chunk.bin"
        _write_pattern(path, size)
        samples = []
        for _ in range(reps):
            t0 = time.perf_counter()
            man = cdc.make_manifest(path)
            elapsed = time.perf_counter() - t0
            if man.size != size or not man.chunks:
                raise RuntimeError("parcalama sonucu tutarsiz")
            samples.append((size / (1 << 20)) / elapsed)
        return {"cdc_mib_s": max(samples)}
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ kuyruk
def _seed_paused(qm: QueueManager, count: int) -> None:
    """Duraklatilmis isleri kuyruga topluca koyar (ag yok).

    add() her cagrida TUM kuyrugu yazip fsync eder: 10 000 kez add()
    O(n^2) olurdu. Tohumlama bu yuzden tek kilitta toplu yapilir; kalici
    yazim _persist() ile (tek cagri, tum kuyrugu yazar) olculur/garanti
    edilir.
    """
    jobs = []
    for i in range(count):
        job = DownloadJob(qm.env, qm.cfg, JobSpec(
            url="http://127.0.0.1:1/ferdim-bench-%d.bin" % i,
            filename="bench-%d.bin" % i), bucket=qm.bucket)
        job.state = JobState.PAUSED
        jobs.append(job)
    with qm._lock:
        qm.jobs.extend(jobs)


def _kuyruk_dosyasi(qm: QueueManager) -> Path:
    """Kalici kuyruk dosyasi (queue.json): bicim diske yazim sozlesmesidir."""
    return Path(qm.env.state_dir) / "queue.json"


def _zaman_damgasi(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _is_diskte(path: Path, job_id: str) -> bool:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, list) and any(
        isinstance(r, dict) and r.get("id") == job_id for r in data)


def _persist(qm: QueueManager, spec: JobSpec) -> float:
    """Bir isi kuyruga ekler; DISKE yazilma suresini (sn) doner.

    add() bugun kuyrugun tamamini eszamanli yazar; kuyruk ileride
    tembellestirilmis/artimli yazima gecse de olcum dogru kalir: sure,
    queue.json degisip isi icerdigi ana kadar gecen zamandir. Boylece
    "10 000 isli kuyrugu kaydetme" gercekten kalicilik hizini olcer.
    """
    path = _kuyruk_dosyasi(qm)
    before = _zaman_damgasi(path)
    t0 = time.perf_counter()
    job = qm.add(spec, paused=True)
    deadline = t0 + 10.0
    while True:
        now = time.perf_counter()
        current = _zaman_damgasi(path)
        if current != before:
            elapsed = now - t0
            if _is_diskte(path, job.id):    # icerik dogrulamasi (sure disi)
                return elapsed
            before = current                # baska bir yazim: sonrakini bekle
        if now > deadline:
            raise RuntimeError("kuyruk diske yazilmadi (%.1f sn)"
                               % (now - t0))
        time.sleep(0.002)


def bench_queue(quick: bool) -> dict:
    """10 000 (quick: 2 000) duraklatilmis is: kaydet/yukle hizi + stats()."""
    count = QUEUE_QUICK if quick else QUEUE_FULL
    # 5 tur (quick: 2): yukleme olcumu en gurultulu metrik; en iyi tur
    # arka plan yukunun denk geldigi yavassiz pencereyi yakalar
    reps = 2 if quick else 5
    root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX))
    try:
        env = AppEnv(root=root)
        cfg = env.load_config()
        qm = QueueManager(env, cfg)
        try:
            _seed_paused(qm, count)
            saves = []
            for i in range(reps):
                saves.append(_persist(qm, JobSpec(
                    url="http://127.0.0.1:1/ferdim-bench-save-%d.bin" % i,
                    filename="save-%d.bin" % i)))
            jobs = len(qm.jobs)
        finally:
            qm.shutdown()
        loads = []
        stats_samples = []
        for i in range(reps):
            t0 = time.perf_counter()
            qm2 = QueueManager(env, cfg)
            loads.append(time.perf_counter() - t0)
            try:
                if len(qm2.jobs) != jobs:
                    raise RuntimeError("yuklenen is sayisi yanlis: %d != %d"
                                       % (len(qm2.jobs), jobs))
                if i == reps - 1:         # son kopya: stats() gecikmesi
                    for _ in range(STATS_CALLS):
                        t1 = time.perf_counter()
                        snap = qm2.stats()
                        stats_samples.append(time.perf_counter() - t1)
                    if len(snap["jobs"]) != jobs:
                        raise RuntimeError("stats() is sayisi yanlis")
            finally:
                qm2.shutdown()
        return {
            # en iyi tur: arka plan yuku (baska testler) ortalamayi bozar,
            # depolanan taban ile ayni yontem karsilastirilir
            "queue_save_jobs_s": jobs / min(saves),
            "queue_load_jobs_s": jobs / min(loads),
            "stats_calls_s": 1.0 / min(stats_samples),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------ bosta daemon
_HZ = 100.0
try:
    _HZ = float(os.sysconf("SC_CLK_TCK"))
except (AttributeError, ValueError, OSError):
    pass


def _cpu_ticks(pid: int) -> int:
    text = Path("/proc", str(pid), "stat").read_text()
    rest = text[text.rfind(")") + 2:].split()
    return int(rest[11]) + int(rest[12])     # utime + stime


def _rss_mib(pid: int) -> float:
    for line in Path("/proc", str(pid), "status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024.0
    return 0.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _xdg_paths(root: Path) -> dict:
    return {"state": root / "state", "config": root / "config",
            "data": root / "data", "home": root / "home",
            "downloads": root / "downloads"}


def _write_daemon_config(root: Path) -> dict:
    paths = _xdg_paths(root)
    for d in paths.values():
        d.mkdir(parents=True, exist_ok=True)
    cfg_dir = paths["config"] / "ferdim"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "web_enabled": True,
        "web_port": _free_port(),          # sanitize [1024,65535] ister
        "download_dir": str(paths["downloads"]),
        "schedule": {"enabled": False, "start": "02:00", "stop": "08:00"},
        "clipboard_watch": False,
        "watch_dirs": [],
        "coexist": False,
        "peer_cache": False,
        "learn_hosts": False,
    }
    (cfg_dir / "config.json").write_text(json.dumps(cfg, indent=2) + "\n",
                                         "utf-8")
    return paths


def _seed_daemon_queue(root: Path, count: int) -> None:
    """Daemon'un okuyacagi durum dizinine dolu kuyruk yazar.

    Kuyruk bicimi QueueManager'in kendisinden uretilir (elle JSON yok):
    XDG degiskenleri gecici koke cevrilir, AppEnv() daemon ile ayni
    dizinleri gorur.
    """
    keys = ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["XDG_STATE_HOME"] = str(root / "state")
    os.environ["XDG_CONFIG_HOME"] = str(root / "config")
    os.environ["XDG_DATA_HOME"] = str(root / "data")
    try:
        env = AppEnv()
        cfg = env.load_config()
        qm = QueueManager(env, cfg)
        try:
            _seed_paused(qm, count)
            _persist(qm, JobSpec(url="http://127.0.0.1:1/ferdim-bench-seed.bin",
                                 filename="seed.bin"))
        finally:
            qm.shutdown()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _wait_daemon(state_dir: Path, proc, err_path: Path) -> None:
    """pid + unix soketi + web portu hazir olana dek bekle.

    Hazir olma olcutu TCP dinleme + soket dosyasidir; HTTP denemesi token/
    peer kapilarina takilip olcumu geciktirmesin. Basarisizlikta daemon'in
    kendi logu ve stderr kuyrugu hataya eklenir (teshis icin).
    """
    deadline = time.monotonic() + 30.0
    last = "bekleniyor"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            try:
                err = err_path.read_text("utf-8", "replace")[-2000:]
            except OSError:
                err = ""
            raise RuntimeError("daemon erken cikti (rc=%s): %s"
                               % (proc.returncode, err.strip()))
        try:
            pid = int((state_dir / "ferdim.pid").read_text().strip())
            port = int((state_dir / "port").read_text().strip())
        except (OSError, ValueError) as exc:
            last = "pid/port dosyasi yok (%s)" % exc
            time.sleep(0.05)
            continue
        if pid != proc.pid:
            last = "pid dosyasi baska sureci gosteriyor: %s" % pid
            time.sleep(0.05)
            continue
        if not (state_dir / "ferdim.sock").exists():
            last = "unix soketi yok"
            time.sleep(0.05)
            continue
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError as exc:
            last = "web portu %d dinlemiyor (%s)" % (port, exc)
        time.sleep(0.1)
    try:
        tail = err_path.read_text("utf-8", "replace")[-800:]
    except OSError:
        tail = ""
    try:
        log = (state_dir / "ferdim.log").read_text("utf-8", "replace")[-800:]
    except OSError:
        log = ""
    raise RuntimeError("daemon hazir olmadi (30 sn): %s | stderr: %s | log: %s"
                       % (last, tail.strip(), log.strip()))


def _daemon_idle(seconds: float, count: int, prefix: str) -> dict:
    """Daemon'u izole XDG kokunde baslat; bosta CPU/RSS olc; temiz kapat."""
    root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX))
    proc = None
    try:
        paths = _write_daemon_config(root)
        if count:
            _seed_daemon_queue(root, count)
        env = dict(os.environ)
        env.update({
            "XDG_STATE_HOME": str(paths["state"]),
            "XDG_CONFIG_HOME": str(paths["config"]),
            "XDG_DATA_HOME": str(paths["data"]),
            "HOME": str(paths["home"]),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
        })
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)   # bildirim yolu kapali
        err_path = root / "daemon.stderr"
        with open(err_path, "wb") as err:
            proc = subprocess.Popen(
                [sys.executable, "-m", "ferdim.daemon"],
                cwd=str(REPO_ROOT), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err)
        state_dir = paths["state"] / "ferdim"
        _wait_daemon(state_dir, proc, err_path)
        time.sleep(1.5)                    # acilis gecisi (ilk kuyruk yazimi)
        pid = proc.pid
        t0 = time.perf_counter()
        ticks0 = _cpu_ticks(pid)
        time.sleep(seconds)
        ticks1 = _cpu_ticks(pid)
        elapsed = time.perf_counter() - t0
        rss = 0.0
        for _ in range(3):
            rss = max(rss, _rss_mib(pid))
            time.sleep(0.2)
        return {
            prefix + "_cpu_pct": (ticks1 - ticks0) / _HZ / elapsed * 100.0,
            prefix + "_rss_mib": rss,
        }
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()                # SIGTERM: daemon temiz kapanir
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        shutil.rmtree(root, ignore_errors=True)


def bench_idle(quick: bool) -> dict:
    """Bosta CPU (% tek cekirdek) ve RSS; bos + dolu kuyruk varyanti."""
    if not Path("/proc/self/stat").exists():       # /proc yok: olcum yok
        return {}
    seconds = IDLE_QUICK if quick else IDLE_FULL
    count = QUEUE_QUICK if quick else QUEUE_FULL
    out = {}
    out.update(_daemon_idle(seconds, 0, "idle"))
    out.update(_daemon_idle(seconds, count, "idle_loaded"))
    return out
