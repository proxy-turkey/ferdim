"""DownloadJob: dinamik segmentli indirme isi.

- N is parcasi; biten isci en buyuk kalan araligin ikinci yarisini calar
  (dinamik segmentasyon + is calma).
- Her is .part dosyasina os.pwrite ile yazar; meta JSON'u periyodik diske
  dokulur, boylece elektrik kesilse bile kaldigi yerden devam eder.
- If-Range ile ETag/Last-Modified dogrulamasi: kaynak degismisse bastan baslar.
"""
from __future__ import annotations

import http.client
import os
import threading
import time
import urllib.error
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import util
from ..paths import AppEnv
from ..ratelimit import TokenBucket
from . import httpcli, hls

CHUNK = 1 << 16            # 64 KB okuma blogu
MIN_SPLIT = 1 << 20        # 1 MB'dan kucuk aralik bolunmez
CHECKPOINT_INTERVAL = 2.0  # sn


class JobState:
    QUEUED = "queued"
    CONNECTING = "connecting"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    DONE = "done"
    ERROR = "error"
    CANCELED = "canceled"


@dataclass
class JobSpec:
    url: str
    filename: str = ""
    directory: str = ""
    segments: int = 8
    headers: dict = field(default_factory=dict)
    cookies: str = ""
    user_agent: str = ""
    referer: str = ""
    user: str = ""
    password: str = ""
    proxy: str = ""
    insecure: bool = False
    limit: int = 0  # is basina bayt/sn

    def options(self, cfg: dict) -> httpcli.DlOptions:
        h = dict(self.headers or {})
        if self.referer:
            h.setdefault("Referer", self.referer)
        return httpcli.DlOptions(
            user_agent=self.user_agent or cfg.get("user_agent", ""),
            headers=h,
            cookies=self.cookies or "",
            user=self.user, password=self.password,
            proxy=self.proxy or "",
            insecure=self.insecure or not cfg.get("verify_tls", True),
            timeout=int(cfg.get("timeout", 30)),
            retries=int(cfg.get("retries", 5)),
        )


class DownloadJob:
    def __init__(self, env: AppEnv, cfg: dict, spec: JobSpec,
                 job_id: str | None = None, on_change=None):
        self.env = env
        self.cfg = cfg
        self.spec = spec
        self.id = job_id or uuid.uuid4().hex[:10]
        self.state = JobState.QUEUED
        self.on_change = on_change or (lambda job: None)

        self.url = spec.url
        self.total = -1
        self.done = 0
        self.speed = 0.0
        self.error = ""
        self.kind = "http"
        self.created = time.time()
        self.final_filename = spec.filename

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        # isci havuzu sunucunun izin verdigi kadarina adaptive
        # kuculur; limit asimlari isi HATA ile bitirmez.
        self._allowed = max(1, min(int(spec.segments or 8), 32))
        self._active = 0          # su an aralik isleyen isci sayisi
        self._alive: set[int] = set()
        self._spawned_n = 0       # park edilenler dahil toplam isci
        self._limit_events = 0
        # --- akilli guclendirici: hedef = hat VEYA sunucu tavani ---
        self._seg_max = self._allowed       # kesif ust siniri
        self._server_floor = 1              # sunucu cezasi tabani
        self._last_limit_ts = 0.0
        self._gov = {
            "ema": 0.0, "best": 0.0, "best_workers": self._allowed,
            "t": time.monotonic(), "done": 0, "probe": False, "dir": 0,
            "ref_allowed": 0, "ref_speed": 0.0,
            "cooldown": 0.0, "next_up": 0.0, "next_down": 15.0,
        }
        self._threads: list[threading.Thread] = []
        self._monitor: threading.Thread | None = None
        self._fd: int | None = None
        self._pending: list[tuple[int, int]] = []   # kalan araliklar
        self._etag = ""
        self._last_modified = ""
        self._restart_needed = False
        self._bucket = TokenBucket(spec.limit or cfg.get("speed_limit", 0))
        # Turbo: daha derin okuma blogu + daha kucuk bolme esigi = daha
        # agresif paralellik; hat doana kadar calisir.
        self._turbo = bool(cfg.get("turbo_mode"))
        self._chunk = 1 << 18 if self._turbo else CHUNK       # 256KB / 64KB
        self._min_split = 1 << 18 if self._turbo else MIN_SPLIT  # 256KB / 1MB
        self._last_checkpoint = 0.0
        self._speed_done_mark = 0
        self._speed_mark_ts = time.monotonic()
        self._meta: dict = {}
        self.extra: dict = {}

    # ------------------------------------------------------------------ path
    @property
    def meta_path(self) -> Path:
        return self.env.jobs_dir / f"{self.id}.json"

    @property
    def part_path(self) -> Path:
        # part dosyasi hedef dizinde tutulur (ayni dosya sistemi,
        # bitiste ucuz rename; kotada indirilecek dizinin tastigi gecer)
        return self.target_dir / f".ferdim-{self.id}.part"

    @property
    def target_dir(self) -> Path:
        return Path(self.spec.directory) if self.spec.directory else self.env.download_dir

    @property
    def final_path(self) -> Path:
        name = self.final_filename or util.filename_from_url(self.url)
        return self.target_dir / name

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id, "url": self.url, "state": self.state,
                "filename": self.final_filename or util.filename_from_url(self.url),
                "dir": str(self.target_dir),
                "fullpath": str(self.final_path),
                "size": self.total, "done": self.done,
                "speed": int(self.speed),
                "eta": ((self.total - self.done) / self.speed)
                       if self.total > 0 and self.speed > 1 else -1,
                "kind": self.kind, "error": self.error,
                "category": util.category_for(
                    self.final_filename or util.filename_from_url(self.url)),
                "created": self.created,
                "segments": self._active,          # canli baglanti sayisi
                "segments_cfg": self.spec.segments,
                "segments_effective": self._allowed,
                "adaptive": self.extra.get("adaptive"),
                "best_speed": int(self._gov["best"]),
            }

    def _changed(self):
        self.on_change(self)

    # ----------------------------------------------------------------- start
    def start(self):
        with self._lock:
            if self.state in (JobState.DOWNLOADING, JobState.CONNECTING):
                return
            self._stop.clear()
            self.error = ""
            self.state = JobState.CONNECTING
        self._changed()
        try:
            self._prepare()
        except Exception as e:
            self._fail(str(e))
            return
        self.state = JobState.DOWNLOADING
        self._changed()
        self._spawn()

    def _prepare(self):
        opt = self.spec.options(self.cfg)
        self.target_dir.mkdir(parents=True, exist_ok=True)
        # HLS mi?
        resume_meta = util.read_json(self.meta_path) or {}
        if self.kind == "hls" or httpcli.is_hls(self.url, "") or \
           resume_meta.get("kind") == "hls":
            self.kind = "hls"
            if not self.final_filename:
                self.final_filename = util.filename_from_url(self.url) + ".ts"
            name = self.final_filename = _ensure_ext(self.final_filename)
            self._fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT, 0o644)
            self._meta = resume_meta
            return

        # sunucu limitliyse probe bile 429/503 donebilir: pes etme, bekle
        p = None
        last_err = None
        for attempt in range(6):
            try:
                p = httpcli.probe(self.url, opt)
                break
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 503):
                    time.sleep(min(1.5 * (attempt + 1), 10))
                    continue
                raise
        if p is None:
            raise last_err
        if not self.final_filename:
            # RFC 5987 filename* one cikar; sonra get_filename(), sonra URL
            cand = (util.parse_content_disposition(p.content_disposition)
                    or (util.sanitize_filename(p.filename) if p.filename else ""))
            self.final_filename = cand or util.filename_from_url(
                p.final_url or self.url)
        self.total = p.total
        self._etag, self._last_modified = p.etag, p.last_modified

        if resume_meta.get("url") == self.url and resume_meta.get("etag") == p.etag \
                and resume_meta.get("last_modified") == p.last_modified \
                and self.part_path.exists() and resume_meta.get("ranges"):
            self._pending = [tuple(r) for r in resume_meta["ranges"]]
            self.done = max(0, self.total - sum(e - s + 1 for s, e in self._pending)) \
                if self.total > 0 else int(resume_meta.get("done", 0))
            # sunucunun kabul ettigi isci sayisini hatirla
            if resume_meta.get("allowed"):
                self._allowed = max(1, min(int(resume_meta["allowed"]),
                                           self._allowed))
                self.extra["adaptive"] = self._allowed
        else:
            self._fresh_start(p)
        self._fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT, 0o644)
        if self.total > 0:
            os.ftruncate(self._fd, self.total)
        self._meta = resume_meta

    def _fresh_start(self, p: httpcli.ProbeResult):
        if self.total > 0:
            self._pending = [(0, self.total - 1)]
            self.done = 0
        else:
            self._pending = [(0, 1 << 62)]  # bilinmeyen boyut: akis
            self.done = 0
        if not p.accept_ranges and self.total > 0:
            # Range'siz sunucu: tek akis, aralik BOLUNMEMELI (yoksa
            # start>0 isterken 200 doner ve surekli restart dongusune girer)
            self.spec.segments = 1
            self._allowed = 1
            self._min_split = float("inf")
            self.extra["no_ranges"] = True

    # ---------------------------------------------------------------- workers
    def _spawn(self):
        n = max(1, min(int(self.spec.segments), 32))
        for i in range(n):
            t = threading.Thread(target=self._worker, args=(i,), daemon=True,
                                 name=f"ferdim-{self.id}-{i}")
            t.start()
            self._threads.append(t)
        with self._lock:
            self._spawned_n = n
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()

    def _take_range(self) -> tuple[int, int] | None:
        with self._lock:
            if not self._pending:
                return None
            self._pending.sort(key=lambda r: r[1] - r[0])
            s, e = self._pending.pop()
            if self.total > 0 and e - s + 1 > 2 * self._min_split:
                # DINAMIK aralik boyutu: isci basi hiza gore ~8 saniyelik
                # lokma; hizli sunucuda baglanti israfini azaltir, yavasta
                # paralelligi artirir.
                target = self._target_size_locked()
                if e - s + 1 > 2 * target:
                    cut = max(s, e - int(target))
                    if s < cut:
                        self._pending.append((s, cut - 1))
                        return (cut, e)
                mid = (s + e + 1) // 2
                self._pending.append((s, mid - 1))
                return (mid, e)
            return (s, e)

    # ------------------------------------------------------------- governor
    def _target_size_locked(self) -> int:
        """Isci basi ~8 saniyelik is; emilen tavana gore kendini ayarlar."""
        per = self._gov["best"] / max(1, self._allowed)
        floor = 1 << 20 if self._min_split == float("inf") else self._min_split
        return int(max(floor, min(per * 8.0, 32 << 20)))

    def _pending_bytes_locked(self) -> int:
        return sum(e - s + 1 for s, e in self._pending)

    def _set_allowed(self, n: int) -> None:
        with self._cond:
            n = max(1, min(int(n), self._seg_max))
            if n != self._allowed:
                self._allowed = n
                if n < self._seg_max:
                    self.extra["adaptive"] = n
                else:
                    self.extra.pop("adaptive", None)
                self._cond.notify_all()

    def _start_probe(self, direction: int, ema: float, now: float) -> None:
        g = self._gov
        g["probe"] = True
        g["dir"] = direction
        g["ref_allowed"] = self._allowed
        g["ref_speed"] = max(ema, 1.0)
        self._set_allowed(self._allowed + direction)
        if direction > 0:
            g["next_up"] = now + (25.0 if ema < g["best"] * 0.8 else 45.0)
        else:
            g["next_down"] = now + 20.0

    def _governor_tick(self):
        """2.5 sn'de bir: toplam hizi olc, baglanti sayisini kalibre et.

        Yukari kesif: +1 baglanti hizi artiriyor mu? (sunucu is-basi
        kisitliyorsa toplam hiz boyle buyur). Asagi kesif: hiz tavanda
        sabitken fazla baglantiyi birak (hat tavani). Sunucu cezasi
        (429/503) sonrasi 60 sn kesif yok; taban korunur.
        """
        g = self._gov
        now = time.monotonic()
        if now - g["t"] < 2.5:
            return
        with self._lock:
            if self.kind != "http" or self.total <= 0 \
                    or self._bucket.rate > 0 \
                    or self.extra.get("no_ranges") \
                    or self.state != JobState.DOWNLOADING \
                    or self._stop.is_set():
                g["t"] = now
                g["done"] = self.done
                return
            dt = max(0.1, now - g["t"])
            sample = max(0.0, (self.done - g["done"]) / dt)
            g["t"] = now
            g["done"] = self.done
            if sample <= 0 and self._active == 0:
                return
            g["ema"] = sample if not g["ema"] else 0.6 * g["ema"] + 0.4 * sample
            ema = g["ema"]
            if ema > g["best"]:
                g["best"] = ema
                g["best_workers"] = self._allowed

            if g["probe"]:
                g["probe"] = False
                # esnek esik: anlik dalgalanmalar (CPU/wifi) keşfi yanlis
                # reddetmesin; gercek kayip belirginse geri alinir
                accept = sample > 0 and ema >= g["ref_speed"] * 0.85
                if accept:
                    g["best"] = max(g["best"], ema)
                    g["best_workers"] = self._allowed
                    if g["dir"] > 0:
                        g["next_up"] = now + 5.0    # tirmanmaya devam
                        g["next_down"] = now + 30.0
                    else:
                        g["next_down"] = now + 8.0  # vermeye devam
                else:
                    self._set_allowed(g["ref_allowed"])
                    g["cooldown"] = now + 15.0
                    if g["dir"] > 0:
                        g["next_up"] = now + 30.0
                return

            if now < g["cooldown"]:
                return
            limit_free = (now - self._last_limit_ts) > 60.0
            allowed = self._allowed
            if allowed < self._seg_max and limit_free and (
                    ema < g["best"] * 0.8 or now >= g["next_up"]):
                self._start_probe(+1, ema, now)
            elif (ema >= g["best"] * 0.9
                  and allowed > max(2, self._server_floor)
                  and now >= g["next_down"]
                  and self._pending_bytes_locked()
                  > self._target_size_locked() * allowed * 2):
                self._start_probe(-1, ema, now)

    # ------------------------------------------------- adaptif isci havuzu
    def _acquire(self, wid: int) -> tuple[int, int] | None:
        """Slot alir. Fazla iscalar OLMEZ, park edilir: governor buyudugunde
        aninda devreye girerler (baglanti sayisi aktif slotlarla sinirli)."""
        with self._cond:
            while True:
                if self._stop.is_set():
                    return None
                if not self._pending:
                    if self._active == 0:
                        return None  # her sey bitti
                    self._cond.wait(0.3)
                    continue
                if self._active < self._allowed:
                    self._active += 1
                    rng = self._take_range()
                    if rng is None:  # yaris sarti
                        self._active -= 1
                        return None
                    return rng
                self._cond.wait(0.3)

    def _release(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def _shrink(self, reason: str) -> None:
        """Sunucu limitine uyum: izin verilen isci sayisini yariya indir."""
        with self._cond:
            self._last_limit_ts = time.monotonic()
            self._gov["probe"] = False
            if self._allowed > 1:
                self._allowed = max(1, self._allowed // 2)
                self._server_floor = self._allowed
                self.extra["adaptive"] = self._allowed
                self.extra["adaptive_reason"] = reason
                self._cond.notify_all()

    def _crowded(self) -> bool:
        with self._lock:
            return self._active > 1 or self._allowed > 1

    def _maybe_donate(self, cursor: int, e: int) -> int:
        """SON OYUN: park halinde isci varsa kalan araligin sonundan bagisla."""
        with self._cond:
            if self._spawned_n - self._active <= 0:
                return e
            target = self._target_size_locked()
            if e - cursor + 1 > 2 * target:
                cut = cursor + int(target)
                self._pending.append((cut, e))
                self._cond.notify_all()
                return cut - 1
        return e

    def _worker(self, wid: int):
        if self.kind == "hls":
            return  # HLS kendi havuzunu yonetir
        opt = self.spec.options(self.cfg)
        while not self._stop.is_set():
            rng = self._acquire(wid)
            if rng is None:
                return
            cooldown = 0.0
            exit_worker = False
            try:
                cooldown, exit_worker = self._process_range(rng, opt)
            finally:
                self._release()
            if cooldown:
                time.sleep(min(cooldown, 30.0))
            if exit_worker:
                return

    def _process_range(self, rng, opt) -> tuple[float, bool]:
        """Bir aralik isler. (cooldown, cikis) dondurur.

        cooldown: sunucu limiti nedeniyle bekleme (slot birakildi).
        cikis: isci sonlanmali (hata/restart/durdurma).
        """
        s, e = rng
        cursor = s
        attempt = 0
        limit_hits = 0
        last_donate = time.monotonic()
        while cursor <= e and not self._stop.is_set():
            try:
                resp = httpcli.open_range(
                    self.url, opt, cursor, e,
                    self._etag, self._last_modified)
            except urllib.error.HTTPError as err:
                if err.code in (429, 503):
                    # sunucu limiti -> kucul, bekle, devam et
                    limit_hits += 1
                    self._limit_events += 1
                    if limit_hits > 40 or self._limit_events > 200:
                        with self._lock:
                            self._pending.append((cursor, e))
                        self._fail(f"sunucu surekli limit donduruyor "
                                   f"(HTTP {err.code})")
                        return (0.0, True)
                    ra = ""
                    try:
                        ra = err.headers.get("Retry-After", "") or ""
                    except Exception:
                        pass
                    wait = 3.0
                    if ra.replace(".", "", 1).isdigit():
                        wait = float(ra)
                    self._shrink(f"HTTP {err.code}")
                    with self._lock:
                        self._pending.append((cursor, e))
                    return (min(wait, 30.0), False)
                with self._lock:
                    self._pending.append((cursor, e))
                if err.code in (401, 403, 404, 410) or attempt >= opt.retries:
                    self._fail(f"HTTP {err.code}")
                    return (0.0, True)
                attempt += 1
                time.sleep(min(2 ** attempt, 15))
                continue
            except httpcli.RangeInvalid:
                with self._lock:
                    self._restart_needed = True
                    self._pending.append((cursor, e))
                return (0.0, True)
            except (ConnectionRefusedError, ConnectionResetError,
                    BrokenPipeError, http.client.RemoteDisconnected) as ex:
                if self._crowded():
                    # sunucu eszamanli baglantiyi kisitliyor olabilir
                    self._shrink("baglanti reddedildi (limit?)")
                    with self._lock:
                        self._pending.append((cursor, e))
                    return (2.0, False)
                with self._lock:
                    self._pending.append((cursor, e))
                if attempt >= opt.retries:
                    self._fail(f"baglanti hatasi: {ex}")
                    return (0.0, True)
                attempt += 1
                time.sleep(min(2 ** attempt, 15))
                continue
            except Exception as ex:
                with self._lock:
                    self._pending.append((cursor, e))
                if attempt >= opt.retries:
                    self._fail(f"baglanti hatasi: {ex}")
                    return (0.0, True)
                attempt += 1
                time.sleep(min(2 ** attempt, 15))
                continue
            try:
                while True:
                    remaining = e - cursor + 1
                    if remaining <= 0:
                        # bagis sonrasi kisalanan aralik bitti; sunucunun
                        # gonderecegi fazlaligi okumadan cikiyoruz
                        return (0.0, False)
                    chunk = resp.read(min(self._chunk, remaining))
                    if not chunk:
                        break
                    if self._stop.is_set():
                        with self._lock:
                            self._pending.append((cursor, e))
                        return (0.0, True)
                    self._bucket.take(len(chunk))
                    with self._lock:
                        os.pwrite(self._fd, chunk, cursor)
                        cursor += len(chunk)
                        self.done += len(chunk)
                    self._maybe_checkpoint()
                    now = time.monotonic()
                    if now - last_donate >= 1.0:
                        last_donate = now
                        e = self._maybe_donate(cursor, e)
                if self.total < 0:
                    # bilinmeyen boyut: EOF dosyanin gercek sonudur
                    with self._lock:
                        self.total = cursor
                    self._checkpoint(force=True)
                    return (0.0, True)
                if cursor <= e:
                    raise IOError("erken EOF")
                self._checkpoint(force=True)
                return (0.0, False)
            except httpcli.RangeInvalid:
                with self._lock:
                    self._restart_needed = True
                    self._pending.append((cursor, e))
                return (0.0, True)
            except (ConnectionResetError, ConnectionAbortedError,
                    http.client.RemoteDisconnected,
                    http.client.IncompleteRead) as ex:
                if self._crowded():
                    self._shrink("aktarim kesildi (limit?)")
                    with self._lock:
                        self._pending.append((cursor, e))
                    return (1.5, False)
                attempt += 1
                if attempt > opt.retries:
                    with self._lock:
                        self._pending.append((cursor, e))
                    self._fail(f"okuma hatasi: {ex}")
                    return (0.0, True)
                continue
            except Exception as ex:
                attempt += 1
                if attempt > opt.retries:
                    with self._lock:
                        self._pending.append((cursor, e))
                    self._fail(f"okuma hatasi: {ex}")
                    return (0.0, True)
                time.sleep(min(2 ** attempt, 15))
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        # stop geldi veya dongu bitti
        return (0.0, False)

    # --------------------------------------------------------------- monitor
    def _monitor_loop(self):
        if self.kind == "hls":
            t = threading.Thread(target=self._run_hls, daemon=True)
            t.start()
            t.join()
            self._after_run()
            return
        while True:
            time.sleep(0.2)
            if self._stop.is_set():
                self._fold_threads()
                return
            self._governor_tick()
            with self._lock:
                alive = any(t.is_alive() for t in self._threads)
                empty = not self._pending
                restart = self._restart_needed
            if restart:
                self._do_restart()
                return
            if not alive and empty:
                self._finish()
                return

    def _run_hls(self):
        opt = self.spec.options(self.cfg)
        meta = util.read_json(self.meta_path) or {}
        meta.setdefault("url", self.url)
        meta["kind"] = "hls"
        try:
            def prog(n):
                with self._lock:
                    self.done += n

            hls.download_hls(self.url, opt, self._fd, meta, self._stop,
                             self._bucket, prog, self.extra,
                             meta_path=self.meta_path)
            self.total = self.done = int(meta.get("tail", 0))
            util.atomic_write_json(self.meta_path, meta)
        except Exception as ex:
            self._fail(str(ex))

    def _after_run(self):
        if self.state == JobState.ERROR:
            return
        if self._stop.is_set():
            return
        self._finalize_file()

    def _fold_threads(self):
        for t in self._threads:
            t.join(timeout=3)
        self._checkpoint(force=True)
        if self.state in (JobState.DOWNLOADING, JobState.CONNECTING):
            self.state = JobState.PAUSED
        self._changed()

    def _do_restart(self):
        self._fold_threads()
        with self._lock:
            self._restart_needed = False
            self._threads = []
            if self.total > 0:
                self._pending = [(0, self.total - 1)]
                self.done = 0
                if self._fd is not None:
                    os.ftruncate(self._fd, self.total)
        if self._stop.is_set():
            return
        self.state = JobState.DOWNLOADING
        self._changed()
        self._spawn()

    def _finish(self):
        with self._lock:
            if self.total >= 0 and self.done < self.total:
                self._fail("eksik veri")
                return
        self._finalize_file()

    def _finalize_file(self):
        try:
            if self._fd is not None:
                os.fsync(self._fd)
                os.close(self._fd)
                self._fd = None
            dest = util.unique_path(self.target_dir,
                                    self.final_filename or "download.bin")
            self.target_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(self.part_path, dest)
            except OSError:  # nadiren baska FS: kopyala+sil
                import shutil
                shutil.move(str(self.part_path), str(dest))
            self.final_filename = dest.name
            self.meta_path.unlink(missing_ok=True)
            self.state = JobState.DONE
            self.speed = 0
        except Exception as ex:
            self._fail(str(ex))
        self._changed()

    def _fail(self, msg: str):
        with self._lock:
            if self.state == JobState.CANCELED:
                return
            self.state = JobState.ERROR
            self.error = msg
            self.speed = 0
        self._stop.set()  # diger iscalar kalan araliklari katlasin
        self._checkpoint(force=True)
        self._changed()

    # -------------------------------------------------------------- control
    def pause(self):
        self._stop.set()
        self._changed()

    def resume(self):
        if self.state in (JobState.PAUSED, JobState.ERROR, JobState.QUEUED):
            self.start()

    def cancel(self, delete_files: bool = False):
        self._stop.set()
        if self._monitor:
            self._monitor.join(timeout=5)
        for t in self._threads:
            t.join(timeout=2)
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        except Exception:
            pass
        self.state = JobState.CANCELED
        self.meta_path.unlink(missing_ok=True)
        if delete_files:
            self.part_path.unlink(missing_ok=True)
        self._changed()

    # ------------------------------------------------------------- checkpoint
    def _maybe_checkpoint(self):
        now = time.monotonic()
        if now - self._last_checkpoint >= CHECKPOINT_INTERVAL:
            self._checkpoint(force=True)

    def _checkpoint(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_checkpoint < CHECKPOINT_INTERVAL:
            return
        self._last_checkpoint = now
        if self.kind == "hls" or self._fd is None:
            return
        with self._lock:
            util.atomic_write_json(self.meta_path, {
                "url": self.url,
                "filename": self.final_filename,
                "total": self.total,
                "etag": self._etag,
                "last_modified": self._last_modified,
                "ranges": [list(r) for r in self._pending],
                "done": self.done,
                "kind": self.kind,
                "allowed": self._allowed,
            })

    def update_speed(self):
        """QueueManager ticker'i cagirir."""
        now = time.monotonic()
        with self._lock:
            dt = now - self._speed_mark_ts
            if dt >= 0.5:
                self.speed = (self.done - self._speed_done_mark) / dt
                self._speed_done_mark = self.done
                self._speed_mark_ts = now


def _ensure_ext(name: str) -> str:
    return name if name.lower().endswith((".ts", ".mp4", ".mkv")) else name + ".ts"
