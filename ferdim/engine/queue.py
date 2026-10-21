"""QueueManager: eszamanli is siniri, zamanlama penceresi, kalici kuyruk."""
from __future__ import annotations

import datetime as _dt
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
    def add(self, spec: JobSpec, job_id: str | None = None) -> DownloadJob:
        job = DownloadJob(self.env, self.cfg, spec, job_id=job_id,
                          on_change=lambda j: self._notify(j))
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
            job.state = JobState.QUEUED
            job.error = ""
            job.total, job.done = -1, 0
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
        sch = self.cfg.get("schedule", {})
        if not sch.get("enabled"):
            return True
        try:
            now = _dt.datetime.now()
            cur = now.hour * 60 + now.minute
            s = _map_hm(sch.get("start", "02:00"))
            e = _map_hm(sch.get("stop", "08:00"))
            if s <= e:
                return s <= cur < e
            return cur >= s or cur < e  # gece yarisini asan pencere
        except Exception:
            return True

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
            for job in queued[: max(0, limit - len(running))]:
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
        data = []
        for j in self.jobs:
            data.append({
                "id": j.id, "url": j.url, "filename": j.final_filename,
                "dir": j.spec.directory, "segments": j.spec.segments,
                "headers": j.spec.headers, "cookies": j.spec.cookies,
                "user": j.spec.user, "password": j.spec.password,
                "proxy": j.spec.proxy, "insecure": j.spec.insecure,
                "referer": j.spec.referer, "ua": j.spec.user_agent,
                "kind": j.kind, "state": j.state, "created": j.created,
            })
        util.atomic_write_json(self.env.state_dir / QUEUE_FILE, data)

    def _load(self):
        data = util.read_json(self.env.state_dir / QUEUE_FILE) or []
        for item in data:
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
            "download_dir": str(self.env.download_dir),
            "schedule_active": self._schedule_active(),
            "nm_connected": (time.time() - self.last_nm_ping) < 120,
            "turbo": bool(self.cfg.get("turbo_mode")),
            "language": self.cfg.get("language", "auto"),
            "version": _version(),
        }

    def shutdown(self):
        self._running = False
        with self._lock:
            jobs = list(self.jobs)
        for j in jobs:
            if j.state in STATES_RUNNING:
                j.pause()
        self._save()


def _map_hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _version():
    from .. import __version__
    return __version__
