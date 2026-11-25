"""Olcum paketleri: motor, parcalayici, kuyruk, stats karesi, bosta daemon.

Her paket `(metrics, samples)` dondurur: `metrics` metrik adi -> deger,
`samples` ayni ad -> ham tur listesi (--json ciktisinda sacilim olarak
gorunur). Her paket kendi gecici kokunu kurar ve finally'de siler;
geride surec, soket ya da /tmp/ferdim-bench-* birakmaz. Ag yalnizca
loopback'tir; kuyruk olcumleri duraklatilmis ya da (yerel, reddeden
adreslere bakan) kuyruk isleri kullanir.

Zamanlama yontemi:
- motor / parcalayici: en iyi tur (throughput gurultusune karsi).
- kuyruk kaydet/yukle, stats(), stats karesi: MEDYAN (gecikme/olcek
  olcumleri icin tipik deger; tek yavas tur medyani kaydirmaz).
- bosta CPU: pencereli ortalama; ham pencere yuzdeleri samples'ta.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue as queue_mod
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ferdim import cdc
from ferdim.engine import job as job_mod
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from ferdim.server import hub as hub_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_PREFIX = "ferdim-bench-"

# Motor: 512 MiB ~1.3 sn (bu sinif makinede) ve 0.2 sn'lik nokta araligiyla
# 5-6 checkpoint gecirir. Nokta araligi normalde 2 sn'dir; olcumun bu yolu
# GERCEKTEN gecmesi icin bilincli bir gecersiz kilma (override) kullanilir.
ENGINE_FULL = 512 << 20
ENGINE_QUICK = 128 << 20
ENGINE_CHECKPOINT_INTERVAL = 0.2
ENGINE_MIN_CHECKPOINTS = 3      # tam kosu; quick: 1
CDC_FULL = 64 << 20
CDC_QUICK = 16 << 20
QUEUE_FULL = 10_000
QUEUE_QUICK = 2_000
BULK_FULL = 500                 # bulk_save icindeki add sayisi
BULK_QUICK = 100
QUEUE_LOADS_FULL = 5
QUEUE_LOADS_QUICK = 2
STATS_SAMPLES = 9               # stats() gecikmesi ve stats karesi icin
IDLE_FULL = 8.0
IDLE_QUICK = 4.0
IDLE_SETTLE = 1.5               # acilis gecisi (ilk kuyruk yazimi vb.)
SSE_SETTLE = 2.0                # SSE ilk tam kareyi alana kadar
QUEUED_FULL = 10_000            # KUYRUK'ta tutulan is sayisi (bkz. docs):
                                # yoklama adaylari O(1) ve kapali
                                # pencerede yoklama beklendigi icin
                                # tum kuyruk KUYRUK'ta tutulabilir
QUEUED_QUICK = 100
IDLE_WINDOW = 0.5               # CPU ornekleme penceresi (sn)
IO_BYTES = 2_000_000            # I/O kalibrasyonu: ~15 MiB JSON

# sabit CPU kalibrasyon isi: her makinede ayni is, hiz puani = is/saniye.
# Bu is yuku DEGISIRSE BASELINE_VERSION artirilmali: eski tabanlardaki
# kalibrasyon puani yeni isle karsilastirilamaz.
_CALIB_BLOCK = hashlib.shake_256(b"ferdim-bench-calibration").digest(1 << 20)
_IO_PAYLOAD: bytes | None = None


def calibration(rounds: int = 5) -> float:
    """CPU hizi puani (yuksek = hizli). CPU'ya bagli metrikler buna bolunur.

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


def _io_payload() -> bytes:
    """I/O kalibrasyonu icin belirlenimci, buyuk JSON govdesi (tembel)."""
    global _IO_PAYLOAD
    if _IO_PAYLOAD is None:
        _IO_PAYLOAD = json.dumps(list(range(IO_BYTES)),
                                 separators=(",", ":")).encode("utf-8")
    return _IO_PAYLOAD


def io_calibration(rounds: int = 3) -> float:
    """Depolama/cekirdek yolu hizi puani: yaz+fsync + JSON ayristir.

    CPU'ya bagli olmayan metrikler (loopback aktarim, fsync'li kuyruk
    yazimi) bu puanla olceklenir; saf CPU kalibrasyonu onlari yanlis
    olceklerdi. Is: ~15 MiB JSON'u yaz + fsync + geri oku + ayristir.
    """
    payload = _io_payload()
    root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX))
    try:
        path = root / "io.json"
        best = float("inf")
        for _ in range(max(1, rounds)):
            t0 = time.perf_counter()
            with open(path, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            json.loads(path.read_bytes())
            best = min(best, time.perf_counter() - t0)
        return 1.0 / best if best > 0 else 0.0
    finally:
        shutil.rmtree(root, ignore_errors=True)


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


def _download(job, size: int, count_checkpoints=None) -> tuple[float, int]:
    """Indirmeyi calistirir; (TRANSFER suresi, checkpoint sayisi) doner.

    Sure, tum baytlar `.part` dosyasina yazildigi anda durur: bitisteki
    fsync/rename ve onu izleyen dogrulama disarida kalir. Boylece olcum
    aktarim hizidir; `count_checkpoints` verilirse transfer boyunca
    checkpoint yolunun kac kez gecildigi de bildirilir. Is yine DONE'a
    kadar beklenir ve dogrulanir.
    """
    c0 = count_checkpoints() if count_checkpoints else 0
    t0 = time.perf_counter()
    job.start()
    deadline = time.monotonic() + 180.0
    transfer = None
    cps = 0
    while time.monotonic() < deadline:
        if size > 0 and job.done >= size:
            transfer = time.perf_counter() - t0
            if count_checkpoints:
                cps = count_checkpoints() - c0
            break
        if job.state in (JobState.DONE, JobState.ERROR, JobState.CANCELED):
            transfer = time.perf_counter() - t0
            if count_checkpoints:
                cps = count_checkpoints() - c0
            break
        time.sleep(0.005)
    _wait_job(job)
    if transfer is None:
        raise RuntimeError("is zaman asimi: durum %s" % job.state)
    return transfer, cps


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


def bench_engine(quick: bool) -> tuple[dict, dict]:
    """Yerel sunucudan 8 baglantili indirme: MiB/s (en iyi tur).

    CHECKPOINT_INTERVAL gecici olarak kisaltilir; 512 MiB'lik aktarim
    boylece checkpoint (fsync + meta yazimi) yolunu birkac kez gecer.
    Sayac, yolun gercekten gecildigini dogrular.
    """
    size = ENGINE_QUICK if quick else ENGINE_FULL
    reps = 2 if quick else 3
    need = 1 if quick else ENGINE_MIN_CHECKPOINTS
    orig_ck = DownloadJob._checkpoint
    counter = {"n": 0}

    def counted(self, *a, **kw):
        counter["n"] += 1
        return orig_ck(self, *a, **kw)

    old_interval = job_mod.CHECKPOINT_INTERVAL
    payload_root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX))
    proc = None
    try:
        job_mod.CHECKPOINT_INTERVAL = ENGINE_CHECKPOINT_INTERVAL
        DownloadJob._checkpoint = counted
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
        except queue_mod.Empty:
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
                elapsed, cps = _download(job, size,
                                         lambda: counter["n"])
                if job.state != JobState.DONE or job.done != size:
                    raise RuntimeError(
                        "indirme bitmedi: durum=%s done=%s/%s (%.3f sn)"
                        % (job.state, job.done, size, elapsed))
                if job.final_path.stat().st_size != size:
                    raise RuntimeError("hedef dosya boyu yanlis")
                if cps < need:
                    raise RuntimeError(
                        "checkpoint yolu yeterince gecmedi (%d < %d)"
                        % (cps, need))
                samples.append((size / (1 << 20)) / elapsed)
            finally:
                if job is not None and job.state in (JobState.CONNECTING,
                                                     JobState.DOWNLOADING):
                    job.pause()
                shutil.rmtree(root, ignore_errors=True)
        return {"engine_mib_s": max(samples)}, {"engine_mib_s": samples}
    finally:
        job_mod.CHECKPOINT_INTERVAL = old_interval
        DownloadJob._checkpoint = orig_ck
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


def bench_chunker(quick: bool) -> tuple[dict, dict]:
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
        return {"cdc_mib_s": max(samples)}, {"cdc_mib_s": samples}
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ kuyruk
def _seed_paused(qm: QueueManager, count: int) -> None:
    """Duraklatilmis isleri kuyruga topluca koyar (ag yok).

    add() her cagrida kalici yazimi tetikler: 10 000 kez add() O(n^2)
    olurdu. Tohumlama bu yuzden tek kilitta toplu yapilir; kalicilik
    qm.flush() ile (tek yazim, tum kuyruk) saglanir.
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


def _seed_queued(qm: QueueManager, total: int, queued: int) -> None:
    """Ilk `queued` is KUYRUK'ta, gerisi duraklatilmis (ag yok).

    Kuyruk islerinin URL'si 127.0.0.1:1'e bakar (baglanti reddedilir):
    boyut yoklayicisi calisir ama disari cikmaz. Dosya adlari .mp4:
    daemon yapilandirmasindaki kapali 'video' penceresi islerin
    baslamasini engeller, tick yine tum kuyrugu tarar.
    """
    jobs = []
    for i in range(total):
        job = DownloadJob(qm.env, qm.cfg, JobSpec(
            url="http://127.0.0.1:1/ferdim-bench-%d.mp4" % i,
            filename="bench-%d.mp4" % i), bucket=qm.bucket)
        job.state = JobState.QUEUED if i < queued else JobState.PAUSED
        jobs.append(job)
    with qm._lock:
        qm.jobs.extend(jobs)


def _bulk_add(qm: QueueManager, tag: str, count: int) -> float:
    """`count` isi tek bulk_save() icinde ekler; yazim suresini (sn) doner.

    bulk_save icinde add() yalniz kirli bayragi isaretler; cikista TEK
    dosya yazimi (flush) yapilir. Olculen sey toplu ice aktarma
    verimidir: is olusturma + tek kalici yazim.
    """
    t0 = time.perf_counter()
    with qm.bulk_save():
        for i in range(count):
            qm.add(JobSpec(
                url="http://127.0.0.1:1/ferdim-bench-%s-%d.bin" % (tag, i),
                filename="%s-%d.bin" % (tag, i)), paused=True)
    return time.perf_counter() - t0


def _stats_frame(qm: QueueManager) -> int:
    """Tam stats karesi: qm.stats() + hub.sock_frame JSON kodlamasi."""
    frame = hub_mod.sock_frame({"ok": True, **qm.stats()})
    return len(frame)


def bench_queue(quick: bool) -> tuple[dict, dict]:
    """10 000 is: toplu ekleme, yukleme, stats() ve tam kare maliyeti."""
    count = QUEUE_QUICK if quick else QUEUE_FULL
    bulk = BULK_QUICK if quick else BULK_FULL
    # toplu ekleme olcumu makine yukunden en cok etkilenen metrik: 5 tur
    reps = 2 if quick else 5
    loads = QUEUE_LOADS_QUICK if quick else QUEUE_LOADS_FULL
    root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX))
    try:
        env = AppEnv(root=root)
        cfg = env.load_config()
        stats_samples = []
        frame_samples = []
        qm = QueueManager(env, cfg)
        try:
            _seed_paused(qm, count)
            # stats() ve tam kare maliyeti TAM 10 000 iste olculur
            for _ in range(STATS_SAMPLES):
                t1 = time.perf_counter()
                snap = qm.stats()
                stats_samples.append(time.perf_counter() - t1)
                if len(snap["jobs"]) != count:
                    raise RuntimeError("stats() is sayisi yanlis: %d != %d"
                                       % (len(snap["jobs"]), count))
                t1 = time.perf_counter()
                if _stats_frame(qm) <= 0:
                    raise RuntimeError("bos stats karesi")
                frame_samples.append(time.perf_counter() - t1)
        finally:
            qm.shutdown()
        # yukleme olcumu TEMIZ 10 000 islik dosyada yapilir
        jobs = count
        load_samples = []
        for _ in range(loads):
            t0 = time.perf_counter()
            qm2 = QueueManager(env, cfg)
            load_samples.append(time.perf_counter() - t0)
            try:
                if len(qm2.jobs) != jobs:
                    raise RuntimeError("yuklenen is sayisi yanlis: %d != %d"
                                       % (len(qm2.jobs), jobs))
            finally:
                qm2.shutdown()
        # toplu ekleme ayri bir kopyada: kuyrugu buyutur, yukleme
        # olcumunu (ve is sayisini) etkilemez
        bulk_samples = []
        qm3 = QueueManager(env, cfg)
        try:
            if len(qm3.jobs) != jobs:
                raise RuntimeError("toplu ekleme kopyasi yanlis yuklendi")
            for r in range(reps):
                dt = _bulk_add(qm3, "bulk%d" % r, bulk)
                bulk_samples.append(bulk / dt)
        finally:
            qm3.shutdown()
        metrics = {
            "queue_bulk_add_jobs_s": statistics.median(bulk_samples),
            "queue_load_jobs_s": jobs / statistics.median(load_samples),
            "stats_calls_s": 1.0 / statistics.median(stats_samples),
            "stats_frame_ms": statistics.median(frame_samples) * 1000.0,
        }
        samples = {
            "queue_bulk_add_jobs_s": bulk_samples,
            "queue_load_jobs_s": [jobs / dt for dt in load_samples],
            "stats_calls_s": [1.0 / dt for dt in stats_samples],
            "stats_frame_ms": [dt * 1000.0 for dt in frame_samples],
        }
        return metrics, samples
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


def _idle_cpu(pid: int, seconds: float) -> tuple[float, list[float]]:
    """CPU yuzdesi (tek cekirdek) + pencere basina ham ornekler."""
    t0 = time.perf_counter()
    c0 = _cpu_ticks(pid)
    last_t, last_c = t0, c0
    windows = []
    while True:
        remain = seconds - (time.perf_counter() - t0)
        if remain <= 0:
            break
        time.sleep(min(IDLE_WINDOW, remain))
        now = time.perf_counter()
        c = _cpu_ticks(pid)
        if now - last_t >= IDLE_WINDOW * 0.5:
            windows.append((c - last_c) / _HZ / (now - last_t) * 100.0)
            last_t, last_c = now, c
    now = time.perf_counter()
    c = _cpu_ticks(pid)
    total = (c - c0) / _HZ / (now - t0) * 100.0
    return total, windows


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _xdg_paths(root: Path) -> dict:
    return {"state": root / "state", "config": root / "config",
            "data": root / "data", "home": root / "home",
            "downloads": root / "downloads"}


def _write_daemon_config(root: Path, closed_category: str = "") -> dict:
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
    if closed_category:
        # 00:00-00:00 araligi her zaman KAPALI: kuyruk isleri baslamaz
        cfg["schedule"]["categories"] = {
            closed_category: {"start": "00:00", "stop": "00:00"}}
    (cfg_dir / "config.json").write_text(json.dumps(cfg, indent=2) + "\n",
                                         "utf-8")
    return paths


def _seed_daemon_queue(root: Path, total: int, queued: int) -> None:
    """Daemon'un okuyacagi durum dizinine kuyrugu yazar.

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
            if queued:
                _seed_queued(qm, total, queued)
            else:
                _seed_paused(qm, total)
            qm.flush()
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


class _SseClient:
    """Tek SSE istemcisi: /api/events'e baglanir, kareleri arka planda okur.

    Kareler okunmazsa cekirdek tamponu dolar ve merkez istemciyi kapatir;
    okuma parcacigi bunu onler ve gercekten kare geldigini dogrular.
    """

    def __init__(self, port: int, token: str):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            req = ("GET /api/events HTTP/1.1\r\n"
                   "Host: 127.0.0.1:%d\r\n"
                   "X-FerDim-Token: %s\r\n"
                   "Accept: text/event-stream\r\n\r\n" % (port, token))
            self.sock.sendall(req.encode("ascii"))
            head = b""
            while b"\r\n\r\n" not in head:
                part = self.sock.recv(4096)
                if not part:
                    raise RuntimeError("SSE baglantisi kapandi (basliklar)")
                head += part
            if b" 200 " not in head.split(b"\r\n", 1)[0]:
                raise RuntimeError("SSE akisi acilmadi: %r"
                                   % head.split(b"\r\n", 1)[0])
        except Exception:
            self.sock.close()
            raise
        self.received = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True,
                                        name="ferdim-bench-sse")
        self._thread.start()

    def _drain(self) -> None:
        try:
            while not self._stop.is_set():
                data = self.sock.recv(65536)
                if not data:
                    break
                self.received += len(data)
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(2.0)


def _daemon_idle(seconds: float, total: int, queued: int, sse: bool,
                 prefix: str) -> tuple[dict, dict]:
    """Daemon'u izole XDG kokunde baslat; bosta CPU/RSS olc; temiz kapat."""
    root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX))
    proc = None
    sse_client = None
    try:
        paths = _write_daemon_config(root, "video" if queued else "")
        if total:
            _seed_daemon_queue(root, total, queued)
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
        time.sleep(IDLE_SETTLE)
        if sse:
            port = int((state_dir / "port").read_text().strip())
            token = (state_dir / "token").read_text().strip()
            sse_client = _SseClient(port, token)
            time.sleep(SSE_SETTLE)         # ilk tam kare yazilsin
        pid = proc.pid
        cpu, windows = _idle_cpu(pid, seconds)
        rss_samples = []
        for _ in range(3):
            rss_samples.append(_rss_mib(pid))
            time.sleep(0.2)
        if sse_client is not None:
            if sse_client.received <= 0:
                raise RuntimeError("SSE istemcisine kare gelmedi")
            sse_client.close()
            sse_client = None
        metrics = {prefix + "_cpu_pct": cpu,
                   prefix + "_rss_mib": max(rss_samples)}
        samples = {prefix + "_cpu_pct": windows,
                   prefix + "_rss_mib": rss_samples}
        return metrics, samples
    finally:
        if sse_client is not None:
            sse_client.close()
        if proc is not None and proc.poll() is None:
            proc.terminate()                # SIGTERM: daemon temiz kapanir
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        shutil.rmtree(root, ignore_errors=True)


def bench_idle(quick: bool) -> tuple[dict, dict]:
    """Bosta CPU (% tek cekirdek) ve RSS: bos, duraklatilmis, kuyruk, SSE."""
    if not Path("/proc/self/stat").exists():      # /proc yok: olcum yok
        return {}, {}
    seconds = IDLE_QUICK if quick else IDLE_FULL
    total = QUEUE_QUICK if quick else QUEUE_FULL
    queued = QUEUED_QUICK if quick else QUEUED_FULL
    metrics: dict = {}
    samples: dict = {}
    for prefix, count, live, sse in (
            ("idle", 0, 0, False),
            ("idle_loaded", total, 0, False),
            ("idle_queued", total, queued, False),
            ("idle_sse", total, queued, True)):
        m, s = _daemon_idle(seconds, count, live, sse, prefix)
        metrics.update(m)
        samples.update(s)
    return metrics, samples
