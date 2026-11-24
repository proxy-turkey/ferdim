"""QueueManager: eszamanli is siniri, zamanlama penceresi, kalici kuyruk."""
from __future__ import annotations

import logging
import os
import threading
import time

from .. import util
from ..coexist import DEFAULT_LATENCY, DEFAULT_THRESHOLD, LineMonitor
from ..history import STATES as HISTORY_STATES, open_history, redact_credentials
from ..paths import AppEnv
from .job import DownloadJob, JobSpec, JobState

QUEUE_FILE = "queue.json"
QUEUE_PREV = "queue.json.prev"     # son iyi kopya (bozuk ana dosyanin yedegi)
QUEUE_ITEM_VERSION = 1             # kayit sema surumu (dosya duz liste kalir)
STATES_RUNNING = (JobState.CONNECTING, JobState.DOWNLOADING)

log = logging.getLogger("ferdim.queue")

# Bilinen kayit alanlari. Disinda kalan anahtarlar (bizden yeni surum)
# is uzerinde saklanir ve yeniden yazimda aynen korunur.
_QUEUE_FIELDS = frozenset({
    "v", "id", "url", "filename", "dir", "segments", "headers", "cookies",
    "user", "password", "proxy", "insecure", "http3", "referer", "ua",
    "kind", "state", "created", "active", "mirrors", "checksums", "merkle",
    "limit", "ephemeral",
})


def _queue_migration_1(rec: dict) -> None:
    """v1: kayit duzeyinde surum alani; eski kayitlarda 'v' yok."""


# ileri goc: her adim bir onceki surumun uzerine ekler (history.py gibi)
QUEUE_MIGRATIONS = {1: _queue_migration_1}


def _migrate_record(rec: dict) -> dict:
    """Tek kuyruk kaydini sirali goclardan gecirir.

    Surum alani olmayan kayit v0 sayilir. Bizden yeni surum (v >
    QUEUE_ITEM_VERSION) goc ettirilmez: bilinen alanlar en iyi cabayla
    okunur, bilinmeyenler korunur (ileri uyumluluk).
    """
    rec = dict(rec)
    try:
        version = max(0, int(rec.get("v") or 0))
    except (TypeError, ValueError):
        version = 0
    while version < QUEUE_ITEM_VERSION:
        QUEUE_MIGRATIONS[version + 1](rec)
        version += 1
        rec["v"] = version
    return rec


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _scrub_ephemeral(rec: dict) -> None:
    """Ozel pencere isinin disk kaydindan kimlik bilgilerini siler.

    Cerez, ozel istek basliklari (Authorization dahil), kullanici/parola ve
    URL userinfo'su dusurulur; is yine saklanir ki yeniden baslatmada
    kuyrukta gorunsun. Boyle bir is cerezsiz geri yuklenir: oturum
    gerektiren kaynakta kimlik dogrulamasi basarisiz olabilir. Bu kabul
    edilen davranistir - ozel pencere verisi diskte kalmamalidir.
    """
    rec["headers"] = {}
    rec["cookies"] = ""
    rec["user"] = ""
    rec["password"] = ""
    rec["url"] = redact_credentials(rec.get("url", ""))


class QueueManager:
    def __init__(self, env: AppEnv, cfg: dict):
        self.env = env
        self.cfg = cfg
        self.jobs: list[DownloadJob] = []
        from ..ratelimit import TokenBucket
        self.bucket = TokenBucket(cfg.get("speed_limit", 0))
        # Prometheus ornekleyicisi: /metrics ucu bunu okur
        from ..metrics import Collector
        self.metrics = Collector()
        self._plan_saved: int | None = None   # plandan onceki kullanici limiti
        # plan degisikligi ile tick'in eski plani yeniden uygulamasi
        # yarisamaz (RLock: set_speed_plan tick'i kendisi cagirir)
        self._plan_lock = threading.RLock()

        self._lock = threading.RLock()
        self._listeners: list = []       # degisiklik bildirimi (soket push)
        self._running = False
        self._ticker: threading.Thread | None = None
        self.last_nm_ping = 0.0
        self.history = open_history(env)
        self._hist_state: dict[str, str] = {}   # is -> yazilmis son durum
        # saklama suresi (gun): 0 = sonsuza kadar; hata kuyrugu durdurmaz
        self.history.prune(cfg.get("history_days", 365))
        self.state_warning = ""      # arayuze bildirilen durum kurtarma kodu
        self._rotate_ok = True       # ana dosya -> .prev donusumu guvenli mi
        self._load()
        self._running = True
        self._size_probed: set[str] = set()
        # start() agi yoklar (timeout + denemeler): tick iplikciginde
        # calistirmak tum kuyrugu kilitler. Baslatmasi parcaciga verilmis
        # isler yuvayi HEMEN tutar (durum 'connecting' olana kadar).
        self._starting: set[str] = set()
        # birlikte yasam (varsayilan kapali): hat izleyici yalniz acikken
        # ornekler; 'coexist_on' gecisi tavani birakmak icin tutulur
        self.coexist = LineMonitor()
        self._coexist_on = False
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

    def _job_changed(self, job):
        """Is degisti: dinleyicilere iletir; BITEN isi gecmise yazar.
        Gecmis hatasi (disk dolu, bozuk DB) kuyrugu asla durdurmaz."""
        self._notify(job)
        state = job.state
        if state in HISTORY_STATES and self._hist_state.get(job.id) != state:
            self._hist_state[job.id] = state
            try:
                self.history.record(job)
            except Exception:
                pass

    # ----------------------------------------------------------------- jobs
    def add(self, spec: JobSpec, job_id: str | None = None,
            paused: bool = False) -> DownloadJob:
        """paused=True: is kuyruga 'durdurulmus' girer, tick baslatmaz."""
        job = DownloadJob(self.env, self.cfg, spec, job_id=job_id,
                          on_change=self._job_changed)
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
        with self._lock:
            # dagitimi verilmis ama henuz 'connecting' olmamis is de
            # iptal edilir: aksi halde baslatma parcacigi kullanici
            # kaldirdiktan sonra indirmeye baslardi
            dispatching = job.id in self._starting
            self._starting.discard(job.id)
        if job.state in STATES_RUNNING or dispatching:
            job.cancel(delete_files)
        else:
            # duran/hatali is devam etmeyecek: yarim .part, .segs/ ve
            # denetim noktasi kuyruktan cikinca sahipsiz kalirdi
            if job.state != JobState.DONE:
                try:
                    job.meta_path.unlink(missing_ok=True)
                except OSError:
                    pass
                job._cleanup_temp()
            if delete_files:
                try:
                    job.final_path.unlink(missing_ok=True)
                except Exception:
                    pass
        with self._lock:
            if job not in self.jobs:        # es zamanli iki remove
                return False
            self.jobs.remove(job)
            self._hist_state.pop(job.id, None)
            self._save()
        self._notify()
        return True

    def clear_finished(self):
        with self._lock:
            for j in list(self.jobs):
                if j.state in (JobState.DONE, JobState.ERROR, JobState.CANCELED):
                    self.jobs.remove(j)
                    self._hist_state.pop(j.id, None)
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
        self._speed_plan_tick()
        limit = int(self.cfg.get("max_concurrent", 3))
        # kapanistan sonra yeni is dagitilmaz: baslatma parcaciklari
        # beklemedeyken _tick elle cagrilirsa is baslamamali
        if self._running and self._schedule_active():
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
                        self._start_async(job)
            elif slots:
                for job in queued[:slots]:
                    self._start_async(job)
        for j in self.jobs:
            j.update_speed()
        self._coexist_tick(running)
        with self._lock:
            jobs = list(self.jobs)
        self.metrics.observe(
            jobs, coexist_active=bool(self.cfg.get("coexist")
                                      and self.coexist.active))
        # durum degisimlerini kalici kaydet (biten/iptal edilen isler)
        sig = [(j.id, j.state) for j in self.jobs]
        if sig != getattr(self, "_last_sig", None):
            self._last_sig = sig
            self._save()

    # -------------------------------------------------------- start dagitimi
    def _start_async(self, job) -> bool:
        """Isi tick iplikcigine BAGLAMADAN baslatir.

        start() icindeki ag yoklamasi (timeout x deneme) dakikalarca
        surebilir; tick iplikciginde calistirmak olu bir ana makinenin
        diger islerin baslamasini, hiz guncellemesini ve kaliciligi
        durdurmasina yol aciyordu. Yuva AYNI kilit altinda ayrilir: es
        zamanli tick'ler max_concurrent'i asamaz (start() durumu
        'connecting' yapana kadar is _starting'de sayilir).

        Dagitim penceresi kilit altinda dogrulanir: is bu arada
        kaldirildiysa ya da duraklatildiysa (artik KUYRUK'ta degilse)
        baslatilmaz - aksi halde 'silinen' is indirmeye devam ederdi."""
        with self._lock:
            if not any(j is job for j in self.jobs) \
                    or job.state != JobState.QUEUED:
                return False
            busy = len({j.id for j in self.jobs if j.state in STATES_RUNNING}
                       | self._starting)
            if busy >= int(self.cfg.get("max_concurrent", 3)):
                return False
            self._starting.add(job.id)
        try:
            threading.Thread(target=self._run_start, args=(job,), daemon=True,
                             name=f"ferdim-start-{job.id}").start()
        except RuntimeError:            # parcacik acilamadi: yuva birakilir
            with self._lock:
                self._starting.discard(job.id)
            return False
        return True

    def _run_start(self, job):
        try:
            # kilit altinda son kontrol: is kaldirildiysa/duraklatildiysa
            # parcacik onu baslatmaz (remove(): _starting'den dusurur)
            with self._lock:
                if job.id not in self._starting \
                        or not any(j is job for j in self.jobs) \
                        or job.state != JobState.QUEUED:
                    return
            job.start(queued_only=True)
        except Exception:
            pass
        finally:
            with self._lock:
                self._starting.discard(job.id)

    # ------------------------------------------------------- birlikte yasam
    def _coexist_tick(self, running=None):
        """Opt-in birlikte yasam: hat mesgulse baglanti sayisini yarilar,
        sessizlesince tavani birakir. Kapaliyken hat hic okunmaz; varsayilan
        azami guc davranisi aynen surer."""
        if not bool(self.cfg.get("coexist")):
            if self._coexist_on:            # kapanma gecisi: basinci unut
                self._coexist_on = False
                self.coexist.reset()
            # Mod kapaliyken degismez: HICBIR iste dis tavan kalmaz. Her
            # tick dogrulanir (is duraklatilmisken kapatilip sonra devam
            # ettirilirse ya da yeniden baslatilirsa tavan tasinmaz).
            self._apply_coexist_cap(False)
            return
        try:
            self.coexist.threshold = max(
                0.02, min(0.9, float(self.cfg.get("coexist_threshold",
                                                  DEFAULT_THRESHOLD))))
        except (TypeError, ValueError):
            self.coexist.threshold = DEFAULT_THRESHOLD
        try:
            self.coexist.latency_factor = max(
                1.2, min(20.0, float(self.cfg.get("coexist_latency",
                                                  DEFAULT_LATENCY))))
        except (TypeError, ValueError):
            self.coexist.latency_factor = DEFAULT_LATENCY
        self._coexist_on = True
        if running is None:
            with self._lock:
                running = [j for j in self.jobs if j.state in STATES_RUNNING]
        # kendi hizimizi duseriz: yalniz YABANCI (etkilesimli) trafik kalir
        own = sum(max(0.0, j.speed) for j in running)
        # gecikme sinyali: kendi baglantilarimizin TTFB sismesi (en kotu is)
        latency = 1.0
        for j in running:
            try:
                latency = max(latency, j.latency_inflation())
            except Exception:
                pass
        self.coexist.sample(own, latency=latency)
        self._apply_coexist_cap(self.coexist.active)

    def _apply_coexist_cap(self, active: bool) -> None:
        """Basinc aktifken her isin tavanini yarilar (en az 1 kalir);
        birakirken TUM islerden tavan silinir.

        Birakma durumu is durumuna BAKMAZ: duraklatilmis, bitmis ya da
        iptal edilmis bir is sonradan devam/restart edilirse tavan
        tasimamali. Uygulama ise yalniz kosabilecek islere yapilir (bitmis
        isin gorunumu degismesin); yeniden baslatilan is bir sonraki tick'te
        zaten tavanini alir.
        """
        with self._lock:
            jobs = list(self.jobs)
        for j in jobs:
            if not active:
                j.set_external_cap(None)
                continue
            if j.state in HISTORY_STATES:   # done/error/canceled: kosmaz
                continue
            j.set_external_cap(max(1, j._seg_max // 2))

    def coexist_status(self) -> dict:
        """stats/CLI icin: mod acik mi, basinc ne, tavan uygulaniyor mu."""
        m = self.coexist
        enabled = bool(self.cfg.get("coexist"))
        return {
            "enabled": enabled,
            "pressure": round(float(m.pressure), 3),
            "active": bool(enabled and m.active),
            "threshold": float(m.threshold),
            # gecikme sinyali: kendi TTFB sismemiz (1.0 = normal)
            "latency": round(float(getattr(m, "latency", 1.0)), 3),
            "latency_factor": float(getattr(m, "latency_factor",
                                            DEFAULT_LATENCY)),
        }

    def set_speed_limit(self, bytes_per_sec: int):
        self.cfg["speed_limit"] = int(bytes_per_sec)
        self.bucket.set_rate(int(bytes_per_sec))
        # aktif islerin kendi bucket'larini de guncelle
        with self._lock:
            for j in self.jobs:
                if j.spec.limit == 0:
                    j._bucket.set_rate(int(bytes_per_sec))

    # -------------------------------------------------- saat plani (1.8)
    def plan_status(self) -> dict:
        """Etkin plan kurali, limiti (bayt/sn) ve elle ezme durumu."""
        plan = self.cfg.get("speed_plan")
        plan = plan if isinstance(plan, list) else []
        rule = util.speed_plan_rule(plan)
        active = None
        if rule is not None:
            active = {**rule, "limit": util.parse_size(rule.get("limit")),
                      "index": next((i for i, r in enumerate(plan)
                                     if r is rule), -1)}
        return {
            "enabled": util.speed_plan_limit(plan) is not None,
            # elle verilen limit plani ezer; 'limit plan' denetimi geri verir
            "manual": bool(self.cfg.get("speed_plan_manual")),
            "active_rule": active,
            "limit": active["limit"] if active else 0,
        }

    def set_speed_plan(self, plan, manual: bool | None = None) -> dict:
        """Plani degistirir ve hemen uygular. manual None: dokunma;
        True/False: elle ezme durumunu ayarla (resume False gonderir).

        Plan kilidi, arka plan tick'inin eski plani temizlikten sonra
        yeniden uygulamasini engeller. Plan bosalinca (hangi yoldan
        olursa olsun) onceki kullanici limiti geri yuklenir."""
        with self._plan_lock:
            self.cfg["speed_plan"] = util.sanitize_speed_plan(plan)
            if manual is not None:
                self.cfg["speed_plan_manual"] = bool(manual)
            self._plan_restore_locked()
            self.env.save_config(self.cfg)
            self._speed_plan_tick()
        self._notify()
        return self.plan_status()

    def set_manual_limit(self, bytes_per_sec: int) -> None:
        """Elle verilen limit (limit komutu): plani ezer ve plan silinirse
        BU deger geri yuklenir (bayat plan-oncesi deger degil)."""
        with self._plan_lock:
            self.cfg["speed_plan_manual"] = True
            self.set_speed_limit(int(bytes_per_sec))
            # plan etkinken 'speedplan clear' kullanicinin son secimini korur
            self._plan_saved = (int(bytes_per_sec)
                                if util.speed_plan_limit(
                                    self.cfg.get("speed_plan")) is not None
                                else None)

    def _plan_restore_locked(self) -> None:
        """Plan bosaldiysa (config-set, set_speed_plan ya da dogrudan atama)
        plandan onceki/elle verilen limiti geri yukler. Kilit cagiranda."""
        if util.speed_plan_limit(self.cfg.get("speed_plan")) is not None:
            return
        saved, self._plan_saved = self._plan_saved, None
        if saved is not None:
            self.set_speed_limit(saved)

    def _speed_plan_tick(self):
        """Etkin kuralin limitini her tick uygular; elle verilen limit
        (limit komutu) plani ezer ve kullanici denetimi geri alana kadar
        tick hicbir seye dokunmaz. Plan bosaldiginda (hangi yoldan olursa
        olsun) kayitli kullanici limiti geri yuklenir: aksi halde plan
        limiti kalici olarak uygulanmis kalirdi."""
        changed = False
        with self._plan_lock:
            self._plan_restore_locked()
            limit = util.speed_plan_limit(self.cfg.get("speed_plan"))
            if limit is None or self.cfg.get("speed_plan_manual"):
                return
            if self._plan_saved is None:
                self._plan_saved = int(self.cfg.get("speed_limit") or 0)
            if limit > 0 and self.cfg.get("turbo_mode"):
                # turbo "limitsiz" demek: plan limitiyle celismesin
                self.set_turbo(False)
            if int(self.cfg.get("speed_limit") or 0) != limit:
                self.set_speed_limit(limit)
                changed = True
        if changed:
            self._notify()

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
            # onceki dosyadan gelen bilinmeyen alanlar korunur (ileri surum)
            rec = dict(getattr(j, "_rec_extra", None) or {})
            rec.update({
                "id": j.id, "url": j.url, "filename": j.final_filename,
                # hedef klasor her zaman yazilir: varsayilan indirme
                # klasoru degisse de is yeniden yuklenince yerini korur
                "dir": str(j.target_dir), "segments": j.spec.segments,
                "headers": j.spec.headers, "cookies": j.spec.cookies,
                "user": j.spec.user, "password": j.spec.password,
                "proxy": j.spec.proxy, "insecure": j.spec.insecure,
                "http3": j.spec.http3,
                "referer": j.spec.referer, "ua": j.spec.user_agent,
                "kind": j.kind, "state": j.state, "created": j.created,
                # gecmis 'sure' alani aktif saniyeleri tasir: daemon yeniden
                # baslarsa onceki turlarin suresi kaybolmaz
                "active": round(j.active_seconds(), 2),
                # aynalar, saglamalar ve is hiz limiti daemon yeniden
                # baslasa da kaybolmasin
                "mirrors": list(j.spec.mirrors or []),
                "checksums": j.spec.wanted(),
                "merkle": j.spec.merkle,
                "limit": int(j.spec.limit or 0),
                "ephemeral": bool(j.spec.ephemeral),
            })
            if rec["ephemeral"]:
                _scrub_ephemeral(rec)
            # daha yeni bir surumden yuklendiyse surum numarasi dusurulmez
            rec["v"] = max(QUEUE_ITEM_VERSION,
                           _int(getattr(j, "_rec_version", 0), 0))
            data.append(rec)
        path = self.env.state_dir / QUEUE_FILE
        if self._rotate_ok and path.exists():
            # son iyi kopya: ana dosya once .prev'e tasinir (ad degisikligi,
            # kopya degil). Cokme iki ad arasinda olursa _load .prev'e duser.
            try:
                os.replace(path, path.with_name(QUEUE_PREV))
            except OSError:
                pass
        util.atomic_write_json(path, data)

    # -------------------------------------------------- bozuk durum kurtarma
    def _warn_state(self, msg: str) -> None:
        """Durum kurtarma kodunu arayuze isaretler (stats 'warning')."""
        self.state_warning = "state_recovered"
        log.warning("%s", msg)

    def _quarantine(self, path) -> None:
        """Bozuk kuyruk dosyasini zaman damgali adla saklar.

        Var olan bir .bad dosyasinin uzerine asla yazilmaz; ayni saniyede
        ikinci cagri sayacla ayrisir. Dosya tasinamazsa silinir; o da
        olmazsa .prev donusumu kapatilir (bozuk kopya iyi yedegi ezmesin).
        """
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        bad = path.with_name(f"{path.name}.bad-{stamp}")
        n = 1
        while bad.exists():
            bad = path.with_name(f"{path.name}.bad-{stamp}-{n}")
            n += 1
        try:
            os.replace(path, bad)
        except OSError as exc:
            try:
                path.unlink()
            except OSError:
                self._rotate_ok = False
            log.warning("bozuk kuyruk karantinaya alinamadi: %s", exc)

    def _load(self):
        """Kuyrugu diskten yukler; bozuk kayit tum kuyrugu dusurmez.

        Sirasi: ana dosya -> son iyi kopya (.prev) -> bos kuyruk. Ana dosya
        ayristirilamazsa zaman damgali .bad kopyasi saklanir ve arayuze
        'state_recovered' uyarisi cikar. Her kayit tek tek dogrulanir:
        kullanilamaz kayit atlanir ve loglanir, digerleri yuklenir.
        """
        path = self.env.state_dir / QUEUE_FILE
        prev = path.with_name(QUEUE_PREV)
        data: list = []
        if path.exists():
            raw = util.read_json(path)
            if isinstance(raw, list):
                data = raw
            else:
                self._quarantine(path)
                self._warn_state("kuyruk dosyasi bozuk; karantinaya alindi")
                data = self._load_prev(prev)
        elif prev.exists():
            # atomik yazim iki ad arasinda kesildiyse ana dosya yoktur
            self._warn_state("kuyruk dosyasi eksik; son iyi kopya yuklendi")
            data = self._load_prev(prev)
        skipped = 0
        for item in data:
            job = self._job_from_record(item)
            if job is None:
                skipped += 1
                continue
            self.jobs.append(job)
        if skipped:
            self._warn_state(f"{skipped} bozuk kuyruk kaydi atlandi")
        if self.state_warning:
            # onarilmis/elenmis kuyrugu hemen diske yaz (uyari tekrar etmesin);
            # yazim hatasi baslatiyi dusurmez - kuyruk bellekte calismaya devam eder
            try:
                self._save()
            except Exception as exc:
                log.warning("onarilmis kuyruk yazilamadi: %s", exc)

    def _load_prev(self, prev) -> list:
        """Son iyi kopyayi okur; o da bozuksa silinir (sonsuz dongu olmasin)."""
        raw = util.read_json(prev) if prev.exists() else None
        if isinstance(raw, list):
            return raw
        if prev.exists():
            try:
                prev.unlink()
            except OSError:
                pass
        return []

    def _job_from_record(self, item) -> DownloadJob | None:
        """Tek kuyruk kaydindan is uretir; kullanilamaz kayit None (atlanir).

        Alan turleri tek tek dogrulanir: bir kaydin bozuk alani (orn.
        segments: "abc", headers: liste) tum kuyrugu dusurmemeli. URL'si
        olmayan kayit indirilemez, atlanir. Bilinmeyen durum 'queued'a duser.
        """
        if not isinstance(item, dict):
            log.warning("kuyruk kaydi sozluk degil: %s",
                        type(item).__name__)
            return None
        rec = _migrate_record(item)
        url = rec.get("url")
        if not isinstance(url, str) or not url.strip():
            log.warning("kuyruk kaydi atlandi: url yok")
            return None
        headers = self._record_headers(rec.get("headers"))
        mirrors = rec.get("mirrors")
        if headers is None or not isinstance(mirrors, (list, type(None))):
            log.warning("kuyruk kaydi atlandi: bozuk alan turu")
            return None
        checks = util.normalize_checksums(rec.get("checksums") or {})
        spec = JobSpec(
            url=url,
            filename=_text(rec.get("filename")),
            directory=_text(rec.get("dir")),
            segments=max(1, _int(rec.get("segments"), 8)),
            headers=headers,
            cookies=_text(rec.get("cookies")),
            user=_text(rec.get("user")), password=_text(rec.get("password")),
            proxy=_text(rec.get("proxy")), insecure=bool(rec.get("insecure")),
            http3=_text(rec.get("http3")),
            referer=_text(rec.get("referer")), user_agent=_text(rec.get("ua")),
            mirrors=[m for m in (mirrors or [])
                     if isinstance(m, str)
                     and m.startswith(("http://", "https://"))],
            sha256=checks.get("sha256", ""),
            md5=checks.get("md5", ""),
            checksums={a: v for a, v in checks.items()
                       if a not in ("sha256", "md5")},
            limit=max(0, _int(rec.get("limit"), 0)),
            merkle=_text(rec.get("merkle")),
            # ozel pencere kaydi cerezsiz saklanir (bkz. _scrub_ephemeral)
            ephemeral=bool(rec.get("ephemeral")),
        )
        job = DownloadJob(self.env, self.cfg, spec,
                          job_id=_text(rec.get("id")) or None)
        job.kind = _text(rec.get("kind")) or "http"
        job.created = _float(rec.get("created"), time.time())
        job._active_secs = max(0.0, _float(rec.get("active"), 0.0))
        # bilinmeyen alanlar ve daha yeni surum numarasi korunur
        job._rec_extra = {k: v for k, v in rec.items()
                          if k not in _QUEUE_FIELDS}
        job._rec_version = max(QUEUE_ITEM_VERSION,
                               _int(rec.get("v"), QUEUE_ITEM_VERSION))
        st = rec.get("state")
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
            # bilinmeyen/gelecek durum: guvenli varsayilan kuyrukta bekle
            job.state = JobState.QUEUED
        return job

    @staticmethod
    def _record_headers(raw) -> dict | None:
        """{ad: deger} sozlugu; None = kayit atlanir (bozuk tur)."""
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            return None
        try:
            return {str(k): str(v) for k, v in raw.items()}
        except (TypeError, ValueError):
            return None

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
            "speed_plan": self.plan_status(),
            "max_concurrent": self.cfg.get("max_concurrent", 3),
            # dizin acilamiyorsa (kaldirilmis disk) istatistik kirilmasin
            "download_dir": self._download_dir_text(),
            "schedule_active": self._schedule_active(),
            "nm_connected": (time.time() - self.last_nm_ping) < 120,
            "turbo": bool(self.cfg.get("turbo_mode")),
            "coexist": self.coexist_status(),
            "language": self.cfg.get("language", "auto"),
            # durum dosyasi kurtarildiysa arayuz uyarabilsin ("" = sorun yok)
            "warning": self.state_warning,
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
            # baslatmasi yeni dagitilmis isler de durdurulur: aksi halde
            # kapanistan sonra indirmeye baslarlardi
            starting = set(self._starting)
        for j in jobs:
            if j.state in STATES_RUNNING or j.id in starting:
                j.pause()
        self._save()


def _version():
    from .. import __version__
    return __version__
