"""QueueManager: eszamanli is siniri, zamanlama penceresi, kalici kuyruk."""
from __future__ import annotations

import threading
import time

from .. import util
from ..paths import AppEnv
from .job import DownloadJob, JobSpec, JobState

QUEUE_FILE = "queue.json"
STATES_RUNNING = (JobState.CONNECTING, JobState.DOWNLOADING)


class QueueManager:
    def __init__(self, env: AppEnv, cfg: dict):
        self.env = env
        self.cfg = cfg
        self.jobs: list[DownloadJob] = []
        from ..ratelimit import TokenBucket
        self.bucket = TokenBucket(cfg.get("speed_limit", 0))
        self._lock = threading.RLock()
        self._listeners: list = []       # degisiklik bildirimi (soket push)
        self._running = False
        self._ticker: threading.Thread | None = None
        self.last_nm_ping = 0.0
        self._load()
        self._running = True
        self._size_probed: set[str] = set()
        self._prober = threading.Thread(target=self._prober_loop, daemon=True)
        self._prober.start()
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True)
        self._ticker.start()

    # ------------------------------------------------------------- listeners
    def add_listener(self, fn):
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn):
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _notify(self, job=None):
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(job.snapshot() if job else None)
            except Exception:
                pass

    # ----------------------------------------------------------------- jobs
    def add(self, spec: JobSpec, job_id: str | None = None,
            paused: bool = False) -> DownloadJob:
        """paused=True: is kuyruga 'durdurulmus' girer, tick baslatmaz."""
        job = DownloadJob(self.env, self.cfg, spec, job_id=job_id,
                          on_change=lambda j: self._notify(j))
        if paused:
            job.state = JobState.PAUSED
        with self._lock:
            self.jobs.append(job)
            self._save()
        self._notify(job)
        return job

    def get(self, job_id: str) -> DownloadJob | None:
        with self._lock:
            for j in self.jobs:
                if j.id == job_id:
                    return j
        return None

    def control(self, job_id: str, action: str, delete_files=False) -> bool:
        job = self.get(job_id)
        if not job:
            # "all" destegi
            if job_id == "all":
                with self._lock:
                    jobs = list(self.jobs)
                for j in jobs:
                    self.control(j.id, action, delete_files)
                return True
            return False
        if action == "pause":
            job.pause()
        elif action == "resume":
            job.resume()
        elif action == "cancel":
            job.cancel(delete_files)
            if not self.cfg.get("keep_canceled", True):
                self.remove(job.id)
        elif action == "restart":
            job.cancel(delete_files=False)
            # bastan indirme: yarim .part (FTP'de resume noktasi) silinir
            job.part_path.unlink(missing_ok=True)
            job.state = JobState.QUEUED
            job.error = ""
            job.total, job.done = -1, 0
            with self._lock:
                self._size_probed.discard(job.id)   # yeniden yoklanacak
        return True

    def remove(self, job_id: str, delete_files=False) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        if job.state in STATES_RUNNING:
            job.cancel(delete_files)
        elif delete_files:
            try:
                job.final_path.unlink(missing_ok=True)
            except Exception:
                pass
        with self._lock:
            self.jobs.remove(job)
            self._save()
        self._notify()
        return True

    def clear_finished(self):
        with self._lock:
            for j in list(self.jobs):
                if j.state in (JobState.DONE, JobState.ERROR, JobState.CANCELED):
                    self.jobs.remove(j)
            self._save()

    # ------------------------------------------------------------- schedule
    def _schedule_active(self) -> bool:
        """Genel gece penceresi (schedule.enabled); kapaliysa hep acik."""
        sch = self.cfg.get("schedule")
        if not (isinstance(sch, dict) and sch.get("enabled")):
            return True
        return util.window_open(sch.get("start", "02:00"),
                                sch.get("stop", "08:00"))

    def _in_job_window(self, job) -> bool:
        """Kategoriye ozgu pencere (schedule.categories): o kategoriden bir
        is yalniz kendi penceresinde baslar; digerleri kisitlanmaz."""
        try:
            sch = self.cfg.get("schedule") or {}
            cats = sch.get("categories") if isinstance(sch, dict) else None
            w = cats.get(job.category) if isinstance(cats, dict) else None
        except Exception:
            return True
        if not w:
            return True
        return util.window_open(w.get("start", ""), w.get("stop", ""))

    # ---------------------------------------------------------------- ticker
    def _tick_loop(self):
        while self._running:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(0.5)

    def _tick(self):
        with self._lock:
            running = [j for j in self.jobs if j.state in STATES_RUNNING]
            queued = [j for j in self.jobs if j.state == JobState.QUEUED]
        limit = int(self.cfg.get("max_concurrent", 3))
        started = False
        if self._schedule_active():
            slots = max(0, limit - len(running))
            # kategori penceresi kapali olan isler siralarini bekler
            queued = [j for j in queued if self._in_job_window(j)]
            if self.cfg.get("smart_queue", True) and slots:
                # kucuk dosya once: boyutu bilinmeyenleri yoklayici
                # isaretleyene kadar beklet, sonra kucukten buyuge basla
                ready = [j for j in queued if j.id in self._size_probed]
                if ready:
                    ready.sort(key=lambda j: (j.total if j.total > 0
                                              else float("inf"), j.created))
                    for job in ready[:slots]:
                        job.start()
                        started = True
            else:
                for job in queued[:slots]:
                    job.start()
                    started = True
        for j in self.jobs:
            j.update_speed()
        # durum degisimlerini kalici kaydet (biten/iptal edilen isler)
        sig = [(j.id, j.state) for j in self.jobs]
        if sig != getattr(self, "_last_sig", None):
            self._last_sig = sig
            self._save()

    def set_speed_limit(self, bytes_per_sec: int):
        self.cfg["speed_limit"] = int(bytes_per_sec)
        self.bucket.set_rate(int(bytes_per_sec))
        # aktif islerin kendi bucket'larini de guncelle
        with self._lock:
            for j in self.jobs:
                if j.spec.limit == 0:
                    j._bucket.set_rate(int(bytes_per_sec))

    # ------------------------------------------------------------------ turbo
    def set_turbo(self, on: bool) -> bool:
        """Azami guc: limitsiz hiz + 32 baglanti + sisirilmis eszamanlilik.

        Onceki hiz limiti/eszamanlilik degeri 'turbo_prev' icinde saklanir;
        kapatilinca geri yuklenir. Etkisi yeni islerde tam, aktif islerde
        hiz limiti aninda kalkar.
        """
        on = bool(on)
        with self._lock:
            jobs = list(self.jobs)
        if on:
            if "turbo_prev" not in self.cfg:
                self.cfg["turbo_prev"] = {
                    "speed_limit": self.cfg.get("speed_limit", 0),
                    "max_concurrent": self.cfg.get("max_concurrent", 3),
                }
            self.cfg["turbo_mode"] = True
            self.cfg["speed_limit"] = 0
            self.cfg["max_concurrent"] = max(
                int(self.cfg.get("max_concurrent", 3)),
                int(self.cfg.get("turbo_concurrency", 8)))
            self.bucket.set_rate(0)
            for j in jobs:
                j._turbo = True
                j._chunk = 1 << 18
                j._min_split = 1 << 18
                if j.spec.limit == 0:
                    j._bucket.set_rate(0)
        else:
            self.cfg["turbo_mode"] = False
            prev = self.cfg.pop("turbo_prev", None) or {}
            self.cfg["speed_limit"] = prev.get(
                "speed_limit", self.cfg.get("speed_limit", 0))
            self.cfg["max_concurrent"] = prev.get(
                "max_concurrent", self.cfg.get("max_concurrent", 3))
            self.bucket.set_rate(int(self.cfg.get("speed_limit", 0)))
            for j in jobs:
                j._turbo = False
                j._chunk = 1 << 16
                j._min_split = 1 << 20
                if j.spec.limit == 0:
                    j._bucket.set_rate(int(self.cfg.get("speed_limit", 0)))
        self.env.save_config(self.cfg)
        self._notify()
        return self.cfg["turbo_mode"]

    # ------------------------------------------------------------ persistence
    def _save(self):
        with self._lock:
            self._save_locked()

    def _save_locked(self):
        data = []
        for j in self.jobs:
            data.append({
                "id": j.id, "url": j.url, "filename": j.final_filename,
                # hedef klasor her zaman yazilir: varsayilan indirme
                # klasoru degisse de is yeniden yuklenince yerini korur
                "dir": str(j.target_dir), "segments": j.spec.segments,
                "headers": j.spec.headers, "cookies": j.spec.cookies,
                "user": j.spec.user, "password": j.spec.password,
                "proxy": j.spec.proxy, "insecure": j.spec.insecure,
                "referer": j.spec.referer, "ua": j.spec.user_agent,
                "kind": j.kind, "state": j.state, "created": j.created,
                # aynalar, saglamalar ve is hiz limiti daemon yeniden
                # baslasa da kaybolmasin
                "mirrors": list(j.spec.mirrors or []),
                "checksums": j.spec.wanted(),
                "limit": int(j.spec.limit or 0),
            })
        util.atomic_write_json(self.env.state_dir / QUEUE_FILE, data)

    def _load(self):
        data = util.read_json(self.env.state_dir / QUEUE_FILE) or []
        for item in data:
            if not isinstance(item, dict):
                continue
            checks = util.normalize_checksums(item.get("checksums") or {})
            spec = JobSpec(
                url=item.get("url", ""),
                filename=item.get("filename", ""),
                directory=item.get("dir", ""),
                segments=int(item.get("segments", 8)),
                headers=item.get("headers") or {},
                cookies=item.get("cookies", ""),
                user=item.get("user", ""), password=item.get("password", ""),
                proxy=item.get("proxy", ""), insecure=bool(item.get("insecure")),
                referer=item.get("referer", ""), user_agent=item.get("ua", ""),
                mirrors=[m for m in (item.get("mirrors") or [])
                         if isinstance(m, str)
                         and m.startswith(("http://", "https://"))],
                sha256=checks.get("sha256", ""),
                md5=checks.get("md5", ""),
                checksums={a: v for a, v in checks.items()
                           if a not in ("sha256", "md5")},
                limit=int(item.get("limit", 0) or 0),
            )
            job = DownloadJob(self.env, self.cfg, spec, job_id=item.get("id"))
            job.kind = item.get("kind", "http")
            job.created = item.get("created", time.time())
            st = item.get("state")
            if st in (JobState.DONE,):
                # dosya yerinde mi?
                if job.final_path.exists():
                    job.state = JobState.DONE
                    job.total = job.done = job.final_path.stat().st_size
                else:
                    job.state = JobState.QUEUED
            elif st in (JobState.PAUSED, JobState.ERROR, JobState.CANCELED):
                job.state = JobState.PAUSED if st != JobState.CANCELED else JobState.CANCELED
            else:
                job.state = JobState.QUEUED
            self.jobs.append(job)

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict:
        with self._lock:
            jobs = [j.snapshot() for j in self.jobs]
        speed = sum(j["speed"] for j in jobs)
        return {
            "jobs": jobs,
            "total_speed": speed,
            "active": sum(1 for j in jobs if j["state"] in
                          (JobState.CONNECTING, JobState.DOWNLOADING)),
            "speed_limit": self.cfg.get("speed_limit", 0),
            "max_concurrent": self.cfg.get("max_concurrent", 3),
            # dizin acilamiyorsa (kaldirilmis disk) istatistik kirilmasin
            "download_dir": self._download_dir_text(),
            "schedule_active": self._schedule_active(),
            "nm_connected": (time.time() - self.last_nm_ping) < 120,
            "turbo": bool(self.cfg.get("turbo_mode")),
            "language": self.cfg.get("language", "auto"),
            "version": _version(),
        }

    def _download_dir_text(self) -> str:
        try:
            return str(self.env.download_dir)
        except OSError:
            return ""

    def _prober_loop(self):
        """Kuyrukta bekleyen islerin boyutunu arka planda ogrenir; boylece
        smart_queue kucuk-once siralamasi gercek boyutla calisir."""
        from . import httpcli, dash, hls as _hls, ftp as _ftp
        while self._running:
            try:
                with self._lock:
                    cand = next((j for j in self.jobs
                                 if j.state == JobState.QUEUED
                                 and j.total < 0
                                 and j.id not in self._size_probed), None)
                if cand is None:
                    time.sleep(0.4)
                    continue
                url = cand.url
                if _ftp.is_ftp(url):
                    # ftp:// urllib'in HTTP yoklayicisina gitmemeli
                    try:
                        opt = cand.spec.options(self.cfg)
                        size = _ftp.probe_size(
                            url, timeout=min(max(1, int(opt.timeout)), 5),
                            insecure=opt.insecure, user=cand.spec.user,
                            password=cand.spec.password, proxy=opt.proxy)
                        if size > 0:
                            with cand._lock:
                                if cand.state == JobState.QUEUED:
                                    cand.total = size
                    except Exception:
                        pass
                elif httpcli.is_hls(url, "") or dash.is_dash(url, ""):
                    with cand._lock:
                        if cand.state == JobState.QUEUED:
                            cand.total = -1  # manifest; boyut anlamsiz
                else:
                    try:
                        p = httpcli.probe(url, cand.spec.options(self.cfg))
                        if p.total > 0:
                            with cand._lock:
                                if cand.state == JobState.QUEUED:
                                    cand.total = p.total
                    except Exception:
                        pass
                with self._lock:
                    self._size_probed.add(cand.id)
            except Exception:
                time.sleep(0.5)

    def shutdown(self):
        self._running = False
        with self._lock:
            jobs = list(self.jobs)
        for j in jobs:
            if j.state in STATES_RUNNING:
                j.pause()
        self._save()


def _version():
    from .. import __version__
    return __version__
