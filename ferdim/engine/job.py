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
import random
import re
import socket
import threading
import time
import urllib.error
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import util
from ..paths import AppEnv
from ..ratelimit import TokenBucket
from . import dash, ftp, hls, httpcli

CHUNK = 1 << 16            # 64 KB okuma blogu
MIN_SPLIT = 1 << 20        # 1 MB'dan kucuk aralik bolunmez
CHECKPOINT_INTERVAL = 2.0  # sn
SWRR_FLOOR = 0.05          # yavas kaynaklar bile en iyinin %5'i kadar pay alir
RATE_WINDOW = 2.0          # kaynak hizi olcum penceresi (sn)
SIDECAR_ALGOS = ("sha256", "sha512", "md5", "sha1")   # en gucluden
SIDECAR_WAIT = 5.0         # sn: bitiste henuz aranan yan dosya bu kadar beklenir


def backoff_delay(attempt: int, cap: float = 15.0,
                  rng: random.Random | None = None) -> float:
    """Ustel geri cekilme + jitter: 2, 4, 8, 16... sn (ust sinir cap), her
    denemede 0.5x-1.5x rastgele carpimla. Jitter, ayni anda patlayan
    baglantilarin senkronca tekrar denemesini (thundering herd) onler."""
    base = min(float(2 ** max(0, min(attempt, 8))), cap)
    r = rng.random() if rng is not None else random.random()
    return base * (0.5 + r)


_HTTP_WORDS = {
    400: "gecersiz istek", 401: "kimlik dogrulama gerekli",
    403: "erisim engellendi", 404: "kaynak bulunamadi",
    405: "yontem desteklenmiyor", 407: "proxy kimlik dogrulamasi gerekli",
    408: "istek zaman asimina ugradi", 410: "kaynak kaldirilmis",
    413: "istek cok buyuk", 415: "desteklenmeyen medya",
    416: "aralik gecersiz", 422: "istek islenemedi",
    429: "cok fazla istek", 451: "yasal olarak engellendi",
    500: "sunucu ic hatasi", 501: "sunucu uygulanmadi",
    502: "gecit (proxy) hatasi", 503: "servis su an kullanilamiyor",
    504: "gecit zaman asimi", 507: "depolama alani yetersiz",
    511: "ag kimlik dogrulamasi gerekli",
}


def _describe_error(e: BaseException) -> str:
    """Kullaniciya gosterilecek kisa, anlasilir hata metni. Kitapligin ham
    metni ('HTTP Error 404: File not found') yerine durumun ne oldugunu
    soyler; tanimlanmamis hatalarda istisnanin kendi metni korunur."""
    if isinstance(e, urllib.error.HTTPError):
        name = _HTTP_WORDS.get(e.code)
        return f"{name} (HTTP {e.code})" if name else f"sunucu hatasi (HTTP {e.code})"
    if isinstance(e, (TimeoutError, ConnectionError)):
        return "baglanti zaman asimi" if isinstance(e, TimeoutError) else \
            f"sunucuya baglanilamadi: {e.strerror or e}"
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        if isinstance(r, (TimeoutError, ConnectionError)):
            return _describe_error(r)
        import ssl
        if isinstance(r, ssl.SSLError):
            why = getattr(r, "verify_message", None) or getattr(r, "reason", None) \
                or str(r)
            return "guvenli baglanti kurulamadi: " + str(why)
        return "sunucuya baglanilamadi: " + str(getattr(r, "strerror", None) or r)
    return str(e)


def _retry_after(err) -> float:
    """Retry-After basligindan beklenecek saniye (sayi ya da HTTP-tarih)."""
    try:
        ra = (err.headers.get("Retry-After", "") or "").strip()
    except Exception:
        return 3.0
    if not ra:
        return 3.0
    if ra.replace(".", "", 1).isdigit():
        return max(1.0, min(float(ra), 3600.0))
    try:
        import email.utils
        when = email.utils.parsedate_to_datetime(ra).timestamp()
        return max(1.0, min(when - time.time(), 3600.0))
    except Exception:
        return 3.0


class SourceInvalid(Exception):
    """Kaynak bu is icin gecersiz (farkli boyutlu ya da Range desteksiz ayna)."""

    def __init__(self, msg: str, code: str = "size_mismatch"):
        super().__init__(msg)
        self.code = code


_SRC_CODES = {           # kaynak basarisizlik nedeni -> kararli hata kodu
    "gone": "not_found", "conn": "network", "stall": "stall",
    "reset": "network", "read": "network", "http": "unknown",
    "size": "size_mismatch",
}


def _code_for(e: BaseException) -> str:
    """Arayuzlerin cevirebilmesi icin kararli hata kodu (mesaj Turkce kalir)."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (404, 410):
            return "not_found"
        if e.code in (401, 407):
            return "auth"
        if e.code in (403, 451):
            return "forbidden"
        if e.code == 416:
            return "source_changed"
        return "unknown"
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        return _code_for(r) if isinstance(r, BaseException) else "network"
    if isinstance(e, (SourceInvalid, ftp.FtpError)):
        return e.code
    if isinstance(e, (TimeoutError, ConnectionError)):
        return "network"
    if isinstance(e, OSError):
        if e.errno in util.DISK_ERRNOS:
            return "disk"
        return "network"
    return "unknown"


class _Src:
    """Bir indirme kaynaginin (birincil ya da ayna) saglik kaydi.

    ema   : baglanti basi olculen hiz (bayt/sn), bayt agirlikli ortalama
    cap   : ayni anda acilmasi tercih edilen en cok baglanti (limit sinyali
            gelince yariya iner; ceza penceresi gecince yavasca toparlar)
    cur   : yumusak agirlikli round-robin (SWRR) durumu
    streak: ardisik basarisizlik; ilerleme gordukce sifirlanir, tekrar
            sayisinin asinca kaynak bu is icin 'olu' sayilir
    """
    __slots__ = ("url", "primary", "cap", "active", "ema", "mass", "w", "cur",
                 "streak", "fails", "cooldown", "dead", "reason", "code")

    def __init__(self, url: str, primary: bool, cap: int):
        self.url = url
        self.primary = primary
        self.cap = max(1, int(cap))
        self.active = 0
        self.ema = 0.0
        self.mass = 0.0
        self.w = 1.0
        self.cur = 0.0
        self.streak = 0
        self.fails = 0
        self.cooldown = 0.0
        self.dead = False
        self.reason = ""
        self.code = ""          # arayuz icin kararli hata kodu


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
    mirrors: list = field(default_factory=list)  # alternatif kaynak URL'leri
    sha256: str = ""   # bitiste dogrulanir
    md5: str = ""
    checksums: dict = field(default_factory=dict)  # digerleri: sha1, sha512...

    def wanted(self) -> dict:
        """{algoritma: beklenen ozet} - acikca istenen saglamalar."""
        w = util.normalize_checksums(self.checksums or {})
        for algo, val in (("sha256", self.sha256), ("md5", self.md5)):
            ok = util.normalize_checksums({algo: val})
            if ok:
                w[algo] = ok[algo]
        return w

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
        self.error_code = ""
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
        # eldeki (iscilerin isledigi) araliklar: denetim noktasi bunlari da
        # yazar, boylece duraklama/cokme sonrasi hicbir aralik kaybolmaz
        self._inflight: dict[int, tuple[int, int]] = {}
        self._gen = 0              # isci kusagi: her (yeniden) baslatmada artar
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
        self._probe_t: threading.Thread | None = None   # ayna yoklamasi
        self._runner: threading.Thread | None = None   # hls/dash/ftp kosucusu
        self._fd: int | None = None
        self._pending: list[tuple[int, int]] = []   # kalan araliklar
        self._etag = ""
        self._last_modified = ""
        self._ftp_mtime = ""      # FTP MDTM: resume dogrulamasi
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
        # --- kaynak sagligi (birincil + aynalar) ve yeniden deneme ---
        self._srcs: list[_Src] = []
        self._vsrc = ""            # ETag/Last-Modified hangi kaynaktan geldi
        self._sidecar: dict = {}   # otomatik bulunan saglama degerleri
        self._sc_lock = threading.Lock()   # yan dosya <-> bitis el skmasmasi
        self._finalized = threading.Event()
        self._dir: Path | None = None    # hedef klasor: ilk erisimde sabitlenir
        self._retries = 5
        self._stall = 20           # sn: bu kadar sure bayt gelmezse akis durdu
        self._restarts = 0
        self._rand = random.Random()
        self._limit_mark = 0       # limit sayacini sifirlayan ilerleme isareti
        self._reset_sources()

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
        """Hedef klasor. Acikca verilmisse o; degilse isin ilk
        kullanimindaki varsayilan indirme klasoru SABITLENIR: varsayilan
        sonradan degisse de bitmis isin dosyasi yerinde kalir (yolu,
        silme, arsiv onizleme hep dogru yeri gosterir)."""
        if self.spec.directory:
            return Path(self.spec.directory)
        if self._dir is None:
            d = self.env.download_dir
            with self._lock:
                self._dir = self._dir or d
        return self._dir

    @property
    def final_path(self) -> Path:
        name = self.final_filename or util.filename_from_url(self.url)
        return self.target_dir / name

    @property
    def category(self) -> str:
        return util.category_for(
            self.final_filename or util.filename_from_url(self.url))

    def _window_wait(self) -> str:
        """Bu isin kategorisine zamanlama penceresi tanimlanmissa ve pencere
        su an kapaliysa 'HH:MM-HH:MM' dondurur; yoksa bos."""
        try:
            cats = (self.cfg.get("schedule") or {}).get("categories") or {}
            w = cats.get(self.category) if isinstance(cats, dict) else None
            if not w or util.window_open(w.get("start", ""), w.get("stop", "")):
                return ""
            return f"{w.get('start', '')}-{w.get('stop', '')}"
        except Exception:
            return ""

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        with self._lock:
            window = self._window_wait()
            warnings = []
            if self.extra.get("sources_dead"):
                warnings.append("mirror_dead")
            if self.extra.get("checksum") == "warn":
                warnings.append("checksum_mismatch")
            return {
                "id": self.id, "url": util.redact_url(self.url),
                "state": self.state,
                "filename": self.final_filename or util.filename_from_url(self.url),
                "dir": str(self.target_dir),
                "fullpath": str(self.final_path),
                "size": self.total, "done": self.done,
                "speed": int(self.speed),
                "eta": ((self.total - self.done) / self.speed)
                       if self.total > 0 and self.speed > 1 else -1,
                "kind": self.kind, "error": self.error,
                "error_code": self.error_code,
                "warning_codes": warnings,
                "category": self.category,
                "created": self.created,
                "segments": self._active,          # canli baglanti sayisi
                "segments_cfg": self.spec.segments,
                "segments_effective": self._allowed,
                "adaptive": self.extra.get("adaptive"),
                "checksum": self.extra.get("checksum", ""),
                "verified": list(self.extra.get("verified", [])),
                "mirrors": len(self.spec.mirrors or []),
                "mirror_health": [
                    {"url": util.redact_url(s.url),
                     "state": "dead" if s.dead else "ok",
                     "code": s.code if s.dead else ""}
                    for s in self._srcs if not s.primary],
                "sources_dead": sum(1 for s in self._srcs if s.dead),
                "window": window,
                "sched_wait": bool(window),
                "verifying": bool(self.extra.get("verifying")),   # dogrulaniyor
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
            self.error_code = ""
            self.state = JobState.CONNECTING
            self._reset_sources()
        self._changed()
        try:
            self._prepare()
        except Exception as e:
            self._fail(_describe_error(e), _code_for(e))
            return
        self.state = JobState.DOWNLOADING
        self._changed()
        if self.kind == "http":
            self._start_mirror_probe()
        self._spawn()

    def _prepare(self):
        opt = self.spec.options(self.cfg)
        self.target_dir.mkdir(parents=True, exist_ok=True)
        # HLS / DASH manifest mi? (FTP semasi once gelir: ftp://...m3u8
        # bir FTP dosyasidir, manifest degil)
        resume_meta = util.read_json(self.meta_path) or {}
        if self.kind == "ftp" or ftp.is_ftp(self.url) or \
           resume_meta.get("kind") == "ftp":
            self.kind = "ftp"
            if not self.final_filename:
                self.final_filename = util.filename_from_url(self.url)
            self._meta = resume_meta
            self._prepare_ftp()
            return
        if self.kind == "hls" or httpcli.is_hls(self.url, "") or \
           resume_meta.get("kind") == "hls":
            self.kind = "hls"
            if not self.final_filename:
                self.final_filename = util.filename_from_url(self.url) + ".ts"
            name = self.final_filename = _ensure_ext(self.final_filename)
            self._fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT, 0o644)
            self._meta = resume_meta
            return
        if self.kind == "dash" or dash.is_dash(self.url, "") or \
           resume_meta.get("kind") == "dash":
            self.kind = "dash"
            if not self.final_filename:
                base = util.filename_from_url(self.url)
                self.final_filename = re.sub(r"\.mpd$", "", base,
                                             flags=re.I) + ".mp4"
            self._fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT, 0o644)
            self._meta = resume_meta
            return

        # Kaynak kesfi: birincil + aynalar sirayla denenir; yanit vermeyen
        # ya da baska dosya veren kaynak elenir, ilk saglikli yanit kullanilir
        # (birincil kaynak kapaliysa aynalar isi baslatabilir).
        # Ayarnin boyutu burada ZAMAN ALARAK yoklanmaz: her Range yanitinin
        # Content-Range basligi zaten dogrulanir (bkz. _open).
        self._vsrc = ""
        vs = resume_meta.get("validator_src")
        if vs:
            # devam edilen iste dogrulayiciyi veren kaynak once denensin
            self._srcs.sort(key=lambda s: s.url != vs)
        p = None
        last_err = None
        tries = 0
        while p is None and tries < 10 and not self._stop.is_set():
            tries += 1
            src = self._next_source()
            if src is None:
                break
            try:
                p = httpcli.probe(src.url, opt)
                self._vsrc = src.url
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 503):
                    # limitli sunucu: Retry-After'a saygi, bekle ve yeniden dene
                    self._demote(src, f"HTTP {e.code}", _retry_after(e),
                                 count=False)
                else:
                    self._src_fail(src, "gone" if e.code in (404, 410)
                                   else "http", code=_code_for(e))
            except Exception as e:
                last_err = e
                self._src_fail(src, "conn", code=_code_for(e))
        if p is None:
            if last_err is None:
                last_err = RuntimeError("indirme kaynagi kalmadi")
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
        # kaynak kapasiteleri: her kaynak isin izin verdigi kadar baglanti
        # alabilir; limit sinyali gelince kaynak bazinda daralir
        for s in self._srcs:
            s.cap = max(1, self._allowed)
        self._retries = max(0, int(opt.retries))
        try:
            self._stall = max(5, min(120, int(self.cfg.get("stall_timeout", 20))))
        except (TypeError, ValueError):
            self._stall = 20
        self._start_sidecar(opt)

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

    def _prepare_ftp(self):
        """Tek akis FTP: boyut SIZE'dan, kaldigi yer .part boyutundan.
        Sunucu SIZE vermezse boyut bilinmez (-1); EOF gercek sondur.
        MDTM varsa onceki denetim noktasiyla karsilastirilir: kaynak
        degismisse bastan indirilir (HTTP'deki If-Range karsiligi)."""
        opt = self.spec.options(self.cfg)
        try:
            self._stall = max(5, min(120, int(self.cfg.get("stall_timeout", 20))))
        except (TypeError, ValueError):
            self._stall = 20
        self._allowed = 1                 # FTP tek baglanti; governor yok
        size, mtime = ftp.probe_meta(
            self.url, timeout=max(1, int(opt.timeout)),
            insecure=opt.insecure, stall=self._stall,
            user=self.spec.user, password=self.spec.password)
        self._fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT, 0o644)
        part = self.part_path.stat().st_size if self.part_path.exists() else 0
        if size >= 0 and part > size:
            # yerel dosya sunucudakinden buyuk: ayna/eski kalinti, bastan
            os.ftruncate(self._fd, 0)
            part = 0
        old = (self._meta or {}).get("ftp_mtime")
        if part > 0 and mtime and old and old != mtime:
            # kaynak degismis: yarim dosya yeni icerikle karistirilamaz
            os.ftruncate(self._fd, 0)
            part = 0
        self._ftp_mtime = mtime
        self.total = size
        self.done = part if size < 0 else min(part, size)

    # ---------------------------------------------------------------- workers
    def _spawn(self):
        n = max(1, min(int(self.spec.segments), 32))
        with self._lock:
            self._spawned_n = n
            self._gen += 1              # yeni kusak: eskiler cekilir
            gen = self._gen
        self._threads = [t for t in self._threads if t.is_alive()]
        for i in range(n):
            t = threading.Thread(target=self._worker, args=(i, gen), daemon=True,
                                 name=f"ferdim-{self.id}-{i}")
            t.start()
            self._threads.append(t)
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
                # esnek esik: anlik dalgalanmalar (CPU/wifi) kesfi yanlis
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

    # ------------------------------------------- coklu kaynak (ayna) sagligi
    def _reset_sources(self):
        """Kaynak saglik kayitlarini kurar. Her baslatmada tazelenir:
        duraklatilip devam edilen is, onceki denemede olumlu sayilan
        aynalari yeniden dener."""
        seen, srcs = set(), []
        for i, u in enumerate([self.url] + list(self.spec.mirrors or [])):
            if not isinstance(u, str) \
                    or not u.startswith(("http://", "https://")) or u in seen:
                continue
            seen.add(u)
            srcs.append(_Src(u, i == 0, max(1, self._allowed)))
        self._srcs = srcs
        self.extra.pop("sources_dead", None)

    def _recover_caps_locked(self, now: float):
        """Limit cezasi uzerinden 60 sn gectiyse kapasiteler toparlar."""
        if self._last_limit_ts and now - self._last_limit_ts > 60.0:
            for s in self._srcs:
                if s.cap < self._allowed:
                    s.cap = min(self._allowed, s.cap * 2)
            self._last_limit_ts = 0.0

    def _pick_locked(self, now: float) -> tuple[_Src | None, bool]:
        """Sonraki baglanti icin kaynak secer (kilit altinda).

        Olculmus baglanti basi hiza gore yumusak agirlikli round-robin
        (SWRR): hizli kaynak daha cok baglanti alir, henuz olculmemis kaynak
        en iyinin agirligiyla adil baslar, cok yavas kaynaklar bile yeniden
        olculebilmek icin %5 pay alir. Olumlu ve cezali (cooldown) kaynaklar
        atlanir; kapasitesi dolmus kaynaklar yalniz baska secenek yokken
        kullanilir (governor'in kesiflerine yer acar).
        Donus: (kaynak, False) secildi; (None, True) canli var ama hepsi
        cezali -> bekle; (None, False) canli kaynak yok."""
        alive = [s for s in self._srcs if not s.dead]
        if not alive:
            return None, False
        self._recover_caps_locked(now)
        ready = [s for s in alive if now >= s.cooldown]
        if not ready:
            return None, True
        free = [s for s in ready if s.active < s.cap]
        pool = free or ready
        best = max(s.ema for s in self._srcs)
        total = 0.0
        for s in pool:
            s.w = (max(s.ema, SWRR_FLOOR * best) if s.ema > 0
                   else (best or 1.0))
            s.cur += s.w
            total += s.w
        if free:
            pick = max(pool, key=lambda s: s.cur)
        else:
            # herkes kapasitesinde: en bos olana (yeni baglanti fazlaligi
            # yine de acilabilir; governor yararini olcupt geri verir)
            pick = min(pool, key=lambda s: s.active / s.cap)
        pick.cur -= total
        for s in self._srcs:
            if s not in pool:
                s.cur = 0.0     # cezadan donunce birikmis hakla patlamasin
        return pick, False

    def _next_source(self) -> _Src | None:
        """Sonraki baglanti icin kaynak. Canli kaynak var ama hepsi cezaliysa
        yer acilana kadar bekler (0.5 sn'de bir yeniden bakar). None: is
        durduruldu ya da tum kaynaklar olu."""
        with self._cond:
            while True:
                if self._stop.is_set():
                    return None
                pick, wait = self._pick_locked(time.monotonic())
                if pick is not None:
                    return pick
                if not wait:
                    return None
                self._cond.wait(0.5)

    def _others_alive(self, src: _Src) -> bool:
        """Baska canli kaynak var mi? Varsa tek kaynakin hatasi isi
        bitirmez: kalan aralik baska kaynaktan surdurulur (failover)."""
        return any(s is not src and not s.dead for s in self._srcs)

    def _mark_dead_locked(self, s: _Src):
        s.dead = True
        s.cooldown = float("inf")
        s.cur = 0.0
        self.extra["sources_dead"] = sum(1 for x in self._srcs if x.dead)

    def _src_fail(self, s: _Src, kind: str, cooldown: bool = True,
                  code: str = "") -> None:
        """Kaynak basarisizligi: secim agirligi yariya iner, ardisik
        basarisizlik sayisi artar; tekrar sayisini asarsa (ya da kaynak bu
        is icin gecersizse: 'gone') kaynak bu is icin olu sayilir.
        cooldown=False: baska kaynak yok, geri cekilme uykusu zaten var."""
        with self._cond:
            s.streak += 1
            s.fails += 1
            s.ema *= 0.5
            s.cur = 0.0
            s.reason = kind
            s.code = code or _SRC_CODES.get(kind, "unknown")
            if kind == "gone" or s.streak > self._retries:
                self._mark_dead_locked(s)
            elif cooldown:
                base = min(2.0 * (2 ** (s.streak - 1)), 30.0)
                s.cooldown = time.monotonic() + base * (0.5 + self._rand.random())
            self._cond.notify_all()

    def _src_kill(self, s: _Src, kind: str, code: str) -> None:
        """Yoklama kesin bir sorun gordu: kaynak hemen olu isaretlenir."""
        with self._cond:
            s.reason = kind
            s.code = code
            s.fails += 1
            self._mark_dead_locked(s)
            self._cond.notify_all()

    def _demote(self, s: _Src, reason: str, wait: float,
                count: bool = True) -> None:
        """Baska canli kaynak varken limit/basarisizlik sinyali: yalniz O
        kaynak kuculur - kapasite yariya iner, kisa cooldown, agirlik cezasi.
        Is genelinde kuculme yok; diger kaynaklar yuku devralir. Ardisik
        sinyaller tekrar sayisini asarsa kaynak olu sayilir (olumlu ayna
        deneme maliyetini ise bitene kadar suruklemez: birakilir)."""
        with self._cond:
            s.cap = max(1, s.cap // 2)
            s.cooldown = time.monotonic() + min(max(wait, 1.0), 30.0)
            s.ema *= 0.5
            s.cur = 0.0
            s.reason = reason
            s.code = "rate_limited"
            if count:
                s.streak += 1
                s.fails += 1
                if s.streak > self._retries:
                    self._mark_dead_locked(s)
            self._cond.notify_all()

    def _src_ok(self, src: _Src) -> None:
        """Baglanti araligini hatasiz tamamladi ya da belirgin ilerleme
        sagladi: basarisizlik serisi sifirlanir (ilerleyen kaynak olmez)."""
        with self._lock:
            src.streak = 0
            src.code = ""

    def _src_rate(self, s: _Src, nbytes: int, secs: float) -> None:
        """Kaynagin baglanti basi hizini gunceller: bayt agirlikli ortalama
        (eski olcumler soluklasir, hizli degisime ayak uydurur). Hiz
        limitli ista olcum anlamsiz: atlanir."""
        if s is None or nbytes < 16384 or secs <= 0.05 or self._bucket.rate > 0:
            return
        inst = nbytes / secs
        with self._lock:
            if s.ema <= 0:
                s.ema = inst
            else:
                s.ema = (s.ema * s.mass + inst * nbytes) / (s.mass + nbytes)
            s.mass = min((s.mass + nbytes) * 0.9, 128 << 20)

    def _note_limit(self):
        """Is genelinde limit sayaci. 1 MB'dan fazla ilerleme varsa sifirlanir:
        ilerleyen is hicbir zaman limit yuzunden basarisiz sayilmaz."""
        if self.done - self._limit_mark > (1 << 20):
            self._limit_mark = self.done
            self._limit_events = 0
        self._limit_events += 1

    def _dead_reason(self) -> str:
        names = {"conn": "baglanti hatasi", "http": "HTTP hatasi",
                 "gone": "dosya yok", "stall": "akis durdu",
                 "reset": "baglanti kesildi", "read": "okuma hatasi",
                 "size": "farkli dosya"}
        with self._lock:
            why = sorted({names.get(s.reason, s.reason)
                          for s in self._srcs if s.dead and s.reason})
        return ("tum kaynaklar erisilemiyor"
                + (f" ({', '.join(why)})" if why else ""))

    # ------------------------------------------------- arka plan ayna yoklamasi
    def _start_mirror_probe(self):
        """Aynalari is baslar baslamaz arka planda yoklar: erisilemeyen ya
        da baska dosya veren ayna bir isci ona baglanmadan isaretlenir.
        Birincil kaynak ve su an hizmet veren kaynak yoklanmaz; yoklama isi
        asla yavaslatmaz, basarisizligi isi bitirmez. Ayni anda tek yoklama
        calisir; kaynak listesi (kusak) kimligi yakalanir."""
        t = self._probe_t
        if t is not None and t.is_alive():
            return
        srcs = self._srcs
        if self._stop.is_set() or not any(
                not s.primary and not s.dead for s in srcs):
            return
        self._probe_t = threading.Thread(
            target=self._probe_mirrors, args=(srcs,), daemon=True,
            name=f"ferdim-{self.id}-probe")
        self._probe_t.start()

    def _probe_valid(self, srcs) -> bool:
        """Yoklama hala guncel kaynak kusagina mi bakiyor? Is durdurulduysa
        (duraklat/iptal) ya da yeni baslatma kaynak listesini degistirdiyse
        (devam) eski kayitlara dokunulmaz."""
        with self._lock:
            return not self._stop.is_set() and self._srcs is srcs

    def _probe_apply(self, srcs, fn, *args, **kwargs) -> None:
        """Yoklama sonucunu kaynak kilidi altinda uygular; kosullar arada
        degistiyse (stop / yeni kusak) sessizce vazgecer."""
        with self._cond:
            if self._stop.is_set() or self._srcs is not srcs:
                return
            fn(*args, **kwargs)

    def _probe_mirrors(self, srcs=None):
        """Aynalari sirayla yoklar (HEAD yerine 1 baytlik Range: Range
        destegini de dogrular). Yalniz kesin kanit (404/410, ayna icin
        401/403, Range yok, baska boyut) kaynagi hemen oldurur; diger HTTP
        5xx, zaman asimi ve baglanti hatalari normal basarisizliktir
        (seri + cooldown), 429/503 ise yalnizca cezadir."""
        srcs = self._srcs if srcs is None else srcs
        opt = replace(self.spec.options(self.cfg),
                      timeout=min(max(int(self.cfg.get("timeout", 30)), 2), 10))
        for s in list(srcs):
            if not self._probe_valid(srcs):
                return
            if s.primary or s.dead or s.url == self._vsrc:
                continue
            try:
                p = httpcli.probe(s.url, opt)
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    self._probe_apply(srcs, self._demote, s, f"HTTP {e.code}",
                                      _retry_after(e), count=False)
                elif e.code in (401, 403):
                    self._probe_apply(srcs, self._src_kill, s, "http",
                                      _code_for(e))
                elif e.code in (404, 410):
                    self._probe_apply(srcs, self._src_kill, s, "gone",
                                      _code_for(e))
                else:
                    self._probe_apply(srcs, self._src_fail, s, "http",
                                      code=_code_for(e))
                continue
            except Exception as e:
                self._probe_apply(srcs, self._src_fail, s, "conn",
                                  code=_code_for(e))
                continue
            if not self._probe_valid(srcs):
                return
            if not p.accept_ranges:
                self._probe_apply(srcs, self._src_kill, s, "size",
                                  "incompatible")
            elif self.total > 0 and p.total > 0 and p.total != self.total:
                self._probe_apply(srcs, self._src_kill, s, "size",
                                  "size_mismatch")

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

    def _shrink(self, reason: str, src: _Src | None = None) -> None:
        """Sunucu limitine uyum (baska kaynak yokken): izin verilen isci
        sayisini yariya indir, ceza tabanini guncelle."""
        with self._cond:
            self._last_limit_ts = time.monotonic()
            self._gov["probe"] = False
            if self._allowed > 1:
                self._allowed = max(1, self._allowed // 2)
                self._server_floor = self._allowed
                self.extra["adaptive"] = self._allowed
                self.extra["adaptive_reason"] = reason
            if src is not None:
                src.cap = max(1, min(src.cap, self._allowed))
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

    def _worker(self, wid: int, gen: int | None = None):
        if self.kind in ("hls", "ftp"):
            return  # HLS havuzunu, FTP tek akisi kendi yonetir
        if gen is None:
            gen = self._gen
        opt = self.spec.options(self.cfg)
        # kusak degismisse (durdurma -> devam arasinda okumada kalan eski
        # isci) isci cekilir: araligi denetim noktasinda yazili, iade edilmez
        while not self._stop.is_set() and gen == self._gen:
            rng = self._acquire(wid)
            if rng is None:
                return
            cooldown = 0.0
            exit_worker = False
            try:
                cooldown, exit_worker = self._process_range(rng, opt, wid, gen)
            except Exception:
                # beklenmedik hata: elindeki aralik kaybolmasin
                with self._lock:
                    left = self._inflight.pop(wid, None)
                    if left and gen == self._gen:
                        self._pending.append(left)
                raise
            finally:
                self._release()
            if cooldown:
                time.sleep(min(cooldown, 30.0))
            if exit_worker:
                return

    def _open(self, src: _Src, opt, cursor: int, e: int):
        """Kaynak icin Range baglantisi acar ve yaniti dogrular.
        Hatalar: RangeInvalid (birincil kaynak degismis -> bastan indir),
        SourceInvalid (ayna baska dosya veriyor / Range desteklemiyor ->
        ayna elenir), HTTPError ve baglanti hatalari."""
        same = src.url == self._vsrc      # dogrulayici (If-Range) sadece bu kaynaga
        try:
            resp = httpcli.open_range(
                src.url, opt, cursor, e,
                self._etag if same else "",
                self._last_modified if same else "",
                timeout=self._stall)
        except httpcli.RangeInvalid:
            if src.primary:
                raise
            raise SourceInvalid("Range desteklenmiyor", "incompatible") from None
        m = re.match(r"bytes\s+(\d+)-\d+(?:/(\d+|\*))?",
                     resp.headers.get("Content-Range") or "")
        if m:
            total = m.group(2)
            bad = int(m.group(1)) != cursor or (
                bool(total) and total.isdigit() and self.total > 0
                and int(total) != self.total)
            if bad:
                resp.close()
                if src.primary:
                    raise httpcli.RangeInvalid("Content-Range uyusmuyor")
                raise SourceInvalid("farkli dosya (boyut/aralik)", "size_mismatch")
        return resp

    def _process_range(self, rng, opt, wid: int = -1,
                       gen: int | None = None) -> tuple[float, bool]:
        """Bir aralik isler. (cooldown, cikis) dondurur.

        cooldown: sunucu limiti nedeniyle bekleme (slot birakildi).
        cikis: isci sonlanmali (hata/restart/durdurma).
        Kaynak basarisizliginda aralik KAYBOLMAZ: baska canli kaynak varsa
        beklemeden ona gecilir (failover), yoksa jitter'li ustel geri
        cekilmeyle ayni kaynak yeniden denenir. Aralik yalniz isci
        cekilirken kuyruga iade edilir (ayni iscinin devam ettigi
        denemelerde kopya olusmaz).
        """
        s, e = rng
        cursor = s
        if gen is None:
            gen = self._gen
        attempt = 0            # alternatifsiz basarisizlik (geri cekilme basamagi)
        limit_hits = 0
        last_donate = time.monotonic()
        with self._lock:
            self._inflight[wid] = (cursor, e)      # elde: denetim noktasinda

        def requeue():
            with self._lock:
                self._pending.append((cursor, e))
                self._inflight.pop(wid, None)      # artik kuyrukta, elde degil

        def backoff():
            nonlocal attempt
            attempt += 1
            time.sleep(backoff_delay(attempt, rng=self._rand))

        while cursor <= e and not self._stop.is_set() and gen == self._gen:
            src = self._next_source()
            if src is None:
                if not self._stop.is_set() and gen == self._gen:
                    requeue()
                    self._fail(self._dead_reason(), "no_sources")
                return (0.0, True)
            try:
                resp = self._open(src, opt, cursor, e)
            except SourceInvalid as ex:
                # ayna baska dosya veriyor / Range desteklemiyor: isi bastan
                # baslatmak YANLIS olur (sonsuz restart dongusu); ayna elenir,
                # kalan kaynaklar surdurur
                self._src_fail(src, "gone", code=ex.code)
                if self._others_alive(src):
                    continue
                requeue()
                self._fail(f"ayna gecersiz: {ex}", ex.code)
                return (0.0, True)
            except httpcli.RangeInvalid:
                # birincil kaynak degismis (If-Range 200 / 416) -> bastan indir
                requeue()
                with self._lock:
                    self._restart_needed = True
                return (0.0, True)
            except urllib.error.HTTPError as err:
                if err.code in (429, 503):
                    # sunucu limiti -> kucul, bekle, devam et (is HATA vermez)
                    wait = _retry_after(err)
                    if self._others_alive(src):
                        self._demote(src, f"HTTP {err.code}", wait)
                        continue
                    limit_hits += 1
                    self._note_limit()
                    if limit_hits > 40 or self._limit_events > 200:
                        requeue()
                        self._fail(f"sunucu surekli limit donduruyor "
                                   f"(HTTP {err.code})", "rate_limited")
                        return (0.0, True)
                    self._shrink(f"HTTP {err.code}", src)
                    requeue()
                    return (min(wait, 30.0), False)
                fatal = err.code in (401, 403, 404, 410)
                kind = "gone" if err.code in (404, 410) or (
                    not src.primary and err.code in (401, 403)) else "http"
                if self._others_alive(src):
                    self._src_fail(src, kind, code=_code_for(err))
                    continue
                requeue()
                self._src_fail(src, kind, cooldown=False, code=_code_for(err))
                if fatal or attempt >= opt.retries:
                    self._fail(_describe_error(err), _code_for(err))
                    return (0.0, True)
                backoff()
                continue
            except (ConnectionRefusedError, ConnectionResetError,
                    BrokenPipeError, http.client.RemoteDisconnected) as ex:
                if self._others_alive(src):
                    if self._crowded():
                        self._demote(src, "baglanti reddedildi (limit?)", 2.0)
                    else:
                        self._src_fail(src, "conn", code=_code_for(ex))
                    continue
                if self._crowded():
                    # sunucu eszamanli baglantiyi kisitliyor olabilir
                    self._shrink("baglanti reddedildi (limit?)", src)
                    requeue()
                    return (2.0, False)
                requeue()
                self._src_fail(src, "conn", cooldown=False, code=_code_for(ex))
                if attempt >= opt.retries:
                    self._fail(_describe_error(ex), _code_for(ex))
                    return (0.0, True)
                backoff()
                continue
            except Exception as ex:
                if self._others_alive(src):
                    self._src_fail(src, "conn", code=_code_for(ex))
                    continue
                requeue()
                self._src_fail(src, "conn", cooldown=False, code=_code_for(ex))
                if attempt >= opt.retries:
                    self._fail(_describe_error(ex), _code_for(ex))
                    return (0.0, True)
                backoff()
                continue

            # ---- baglanti acildi: akisi oku -----------------------------
            with self._lock:
                src.active += 1
            nbytes = 0
            progressed = False
            win_t = time.monotonic()
            win_b = 0
            try:
                while True:
                    remaining = e - cursor + 1
                    if remaining <= 0:
                        # bagis sonrasi kisalanan aralik bitti; sunucunun
                        # gonderecegi fazlaligi okumadan cikiyoruz
                        self._src_ok(src)
                        return (0.0, False)
                    chunk = resp.read(min(self._chunk, remaining))
                    if not chunk:
                        break
                    if self._stop.is_set() or gen != self._gen:
                        if gen == self._gen:       # eski kusak iade etmez
                            requeue()              # (aralik kayitta zaten var)
                        return (0.0, True)
                    if not self._bucket.take(len(chunk), self._stop) \
                            or gen != self._gen:
                        # hiz kovasi beklerken durdurma geldi: okunan parca
                        # yazilmadi, aralik (cursor'dan) kuyruga doner
                        if gen == self._gen:
                            requeue()
                        return (0.0, True)
                    with self._lock:
                        os.pwrite(self._fd, chunk, cursor)
                        cursor += len(chunk)
                        self.done += len(chunk)
                        self._inflight[wid] = (cursor, e)
                    nbytes += len(chunk)
                    win_b += len(chunk)
                    if not progressed and nbytes >= (1 << 20):
                        # belirgin ilerleme: basarisizlik serisi sifirlanir
                        progressed = True
                        self._src_ok(src)
                    self._maybe_checkpoint()
                    now = time.monotonic()
                    if now - win_t >= RATE_WINDOW:
                        self._src_rate(src, win_b, now - win_t)
                        win_t, win_b = now, 0
                    if now - last_donate >= 1.0:
                        last_donate = now
                        e = self._maybe_donate(cursor, e)
                        with self._lock:           # bagislanan kuyrugta, elde degil
                            if wid in self._inflight:
                                self._inflight[wid] = (cursor, e)
                if self.total < 0:
                    # bilinmeyen boyut: EOF dosyanin gercek sonudur
                    with self._lock:
                        self.total = cursor
                    self._checkpoint(force=True)
                    return (0.0, True)
                if cursor <= e:
                    raise IOError("erken EOF")
                self._checkpoint(force=True)
                self._src_ok(src)
                return (0.0, False)
            except (TimeoutError, socket.timeout):
                # akis durdu: stall_timeout boyunca tek bayt gelmedi
                if self._others_alive(src):
                    self._src_fail(src, "stall")
                    continue
                requeue()
                self._src_fail(src, "stall", cooldown=False)
                if attempt >= opt.retries:
                    self._fail("okuma hatasi: akis durdu (zaman asimi)", "stall")
                    return (0.0, True)
                backoff()
                continue
            except (ConnectionResetError, ConnectionAbortedError,
                    http.client.RemoteDisconnected,
                    http.client.IncompleteRead) as ex:
                if self._others_alive(src):
                    if self._crowded():
                        self._demote(src, "aktarim kesildi (limit?)", 1.5)
                    else:
                        self._src_fail(src, "reset", code=_code_for(ex))
                    continue
                if self._crowded():
                    self._shrink("aktarim kesildi (limit?)", src)
                    requeue()
                    return (1.5, False)
                requeue()
                self._src_fail(src, "reset", cooldown=False, code=_code_for(ex))
                if attempt >= opt.retries:
                    self._fail(f"okuma hatasi: {_describe_error(ex)}",
                               _code_for(ex))
                    return (0.0, True)
                backoff()
                continue
            except Exception as ex:
                if self._others_alive(src):
                    self._src_fail(src, "read", code=_code_for(ex))
                    continue
                requeue()
                self._src_fail(src, "read", cooldown=False, code=_code_for(ex))
                if attempt >= opt.retries:
                    self._fail(f"okuma hatasi: {_describe_error(ex)}",
                               _code_for(ex))
                    return (0.0, True)
                backoff()
                continue
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                now = time.monotonic()
                self._src_rate(src, win_b, now - win_t)
                with self._cond:
                    src.active -= 1
                    self._cond.notify_all()
                with self._lock:
                    self._inflight.pop(wid, None)  # bitti ya da iade edildi
        # stop geldi veya dongu bitti
        return (0.0, False)

    # -------------------------------------------------------- saglama (yan dosya)
    def _start_sidecar(self, opt):
        """Yan dosya saglamasi: <url>.sha256 / .md5 / .sha512 / .sha1 arka
        planda aranir; bulunursa indirme bitince otomatik dogrulanir.
        Acikca verilmis saglamalar eksikse calisir; isi asla yavaslutmaz."""
        self._sidecar = {}
        self._sidecar_t = None
        if not self.cfg.get("checksum_sidecar", True) or self._stop.is_set():
            return
        if all(a in self.spec.wanted() for a in SIDECAR_ALGOS):
            return
        self._sidecar_t = threading.Thread(
            target=self._sidecar_loop, args=(opt,), daemon=True,
            name=f"ferdim-{self.id}-sidecar")
        self._sidecar_t.start()

    def _sidecar_loop(self, opt):
        name = self.final_filename or util.filename_from_url(self.url)
        explicit = self.spec.wanted()
        for algo in SIDECAR_ALGOS:           # en gucluden zayifa
            if algo in explicit or self._stop.is_set():
                continue
            for suffix in (f".{algo}", f".{algo}sum"):
                try:
                    text = httpcli.fetch_small(
                        util.url_sibling(self.url, suffix), opt,
                        timeout=min(max(int(opt.timeout), 2), 8))
                except Exception:
                    continue               # bu adreste yan dosya yok
                digest = util.parse_checksum_text(text, algo, name)
                if digest:
                    with self._sc_lock:
                        self._sidecar[algo] = digest
                        late = self._finalized.is_set()
                    if late:
                        self._verify_late({algo: digest})   # geciken yan dosya
                    return                 # en guclu bulunani yeter

    # --------------------------------------------------------------- monitor
    def _monitor_loop(self):
        if self.kind in ("hls", "dash", "ftp"):
            if self.kind == "hls":
                runner = self._run_hls
            elif self.kind == "dash":
                runner = self._run_dash
            else:
                runner = self._run_ftp
            gen = self._gen
            t = threading.Thread(target=runner, daemon=True)
            t.start()
            self._runner = t
            t.join()
            if self.kind == "ftp":
                if gen != self._gen:
                    return  # duraklat/devam: eski kosu yeni kusaga karismaz
                if self._stop.is_set():
                    # pause() bu noktayi bekler: nokta yazilir, durum paused
                    self._checkpoint(force=True)
                    with self._lock:
                        if self.state == JobState.DOWNLOADING:
                            self.state = JobState.PAUSED
                    self._changed()
                    return
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
            self._fail(str(ex), _code_for(ex))

    def _run_dash(self):
        opt = self.spec.options(self.cfg)
        meta = util.read_json(self.meta_path) or {}
        meta.setdefault("url", self.url)
        meta["kind"] = "dash"
        try:
            def prog(n):
                with self._lock:
                    self.done += n

            text = dash.fetch_mpd(self.url, opt)
            init_url, seg_urls = dash.build_segment_list(text, self.url)
            self.extra["segments_listed"] = len(seg_urls)
            hls.segments.assemble(opt, self._fd, meta, self._stop,
                                  self._bucket, prog, self.meta_path,
                                  init_url, seg_urls)
            self.total = self.done = int(meta.get("tail", 0))
            util.atomic_write_json(self.meta_path, meta)
        except Exception as ex:
            self._fail(str(ex), _code_for(ex))

    def _run_ftp(self):
        """Tek akis indirme, geri cekilmeli yeniden deneme ile. Durdurma
        geldiginde yazilan baytlar .part'ta kalir; devam REST ile olur.
        Gecici hatalar (ag, stall, 4xx) opt.retries kez denenir; kalici
        5xx (530/550...) hemen isi bitirir."""
        gen = self._gen
        opt = self.spec.options(self.cfg)
        retries = max(0, int(opt.retries))
        attempt = 0
        mark = self.done          # ilerleme isareti: basarili bayt -> butce sifir
        verify = False            # son aktarim son yanitla dogrulanmadi

        def run(offset: int):
            def prog(n):
                with self._lock:
                    self.done += n
                self._maybe_checkpoint()

            return ftp.download(
                self.url, self._fd, offset, stop=self._stop,
                bucket=self._bucket, progress=prog, stalled=self._stall,
                insecure=opt.insecure, timeout=max(1, int(opt.timeout)),
                halted=lambda: gen != self._gen,
                user=self.spec.user, password=self.spec.password)

        while True:
            try:
                if verify or not (self.total >= 0
                                  and self.done >= self.total):
                    try:
                        run(self.done)
                    except ftp.RestUnsupported:
                        if self._stop.is_set() or gen != self._gen:
                            return
                        # sunucu REST bilmiyor: tek care bastan indirmek
                        with self._lock:
                            os.ftruncate(self._fd, 0)
                            self.done = 0
                        mark = 0
                        verify = False
                        run(0)
                if gen != self._gen:
                    return
                if self._stop.is_set():
                    return          # monitor noktayi yazar ve durumu paused yapar
                if self.total < 0:
                    with self._lock:
                        self.total = self.done
                self._checkpoint(force=True)
                if self.total >= 0 and self.done != self.total:
                    raise ftp.FtpError(
                        "eksik veri" if self.done < self.total
                        else "boyut uyusmuyor", "size_mismatch")
                return
            except ftp.FtpError as ex:
                if gen != self._gen or self._stop.is_set():
                    return
                if self.done > mark:
                    mark = self.done
                    attempt = 0      # onceki deneme ilerledi: butce tazelenir
                if not ex.transient or attempt >= retries:
                    self._fail(str(ex), ex.code)
                    return
                if self.total >= 0 and self.done >= self.total:
                    # baytlar tamam ama son yanit gelmedi: bos REST ile dogrula
                    verify = True
                attempt += 1
                self._checkpoint(force=True)
                if self._stop.wait(backoff_delay(attempt, rng=self._rand)):
                    return
            except Exception as ex:
                if gen == self._gen:
                    self._fail(_describe_error(ex), _code_for(ex))
                return

    def _after_run(self):
        if self.state == JobState.ERROR:
            return
        if self._stop.is_set():
            return
        self._finalize_file()

    def _join_threads(self, timeout: float) -> None:
        """Tum iscileri ve hls/dash/ftp kosucusunu ORTAK bir sure icinde
        bekler: her isciye ayri sure degil (32 takili isci 32 x 3 sn degil,
        en cok 'timeout' sn). Kosucu de katilir; cancel fd'yi ancak bu
        beklemeden sonra kapatir."""
        deadline = time.monotonic() + timeout
        cur = threading.current_thread()
        threads = list(self._threads)
        if self._runner is not None and self._runner is not cur:
            threads.append(self._runner)
        for t in threads:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if t.ident is None:      # henuz baslamamis: join beklemesin
                continue
            t.join(left)

    def _fold_threads(self):
        self._join_threads(3.0)
        self._checkpoint(force=True)
        if self.state in (JobState.DOWNLOADING, JobState.CONNECTING):
            self.state = JobState.PAUSED
        self._changed()

    def _do_restart(self):
        self._fold_threads()
        self._restarts += 1
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
        if self._restarts > 3:
            # kaynak surekli degisiyor (tutarsiz sunucu/yuk dengeleyici):
            # sonsuz bastan-indirme dongusune girmeyelim
            self._fail("kaynak surekli degisiyor (tutarsiz sunucu)",
                       "source_changed")
            return
        self.state = JobState.DOWNLOADING
        self._changed()
        self._spawn()

    def _finish(self):
        with self._lock:
            if self.total >= 0 and self.done < self.total:
                self._fail("eksik veri", "size_mismatch")
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
            self.speed = 0
            # Saglama is BITMIS sayilmadan once yapilir: 'done' durumunu goren
            # herkes (CLI --wait, eklenti, web) dosyanin dogrulanmis oldugunu
            # bilir. Bu sirada is hala 'downloading' + verifying=True dur.
            self.extra["verifying"] = True
            self._changed()
            t = getattr(self, "_sidecar_t", None)
            if t is not None and t.is_alive():
                # kucuk dosyada yan dosya henuz araniyor olabilir; isi sonsuza
                # dek bekletmeyiz, sonra bulunanlar _verify_late ile degerlendirilir
                t.join(timeout=SIDECAR_WAIT)
            with self._sc_lock:
                found = dict(self._sidecar)
                self._finalized.set()
            self._verify(dest, found)
            self.extra.pop("verifying", None)
            if self.state != JobState.ERROR:
                self.state = JobState.DONE
        except Exception as ex:
            self.extra.pop("verifying", None)
            self._fail(str(ex), _code_for(ex))
        self._changed()

    def _verify(self, dest: Path, found: dict | None = None) -> None:
        """Saglama. Acikca istenen ozetler uyusmazsa is HATA (veri silinmez).
        Otomatik bulunan yan dosya (sidecar) uyusmazsa dosya yerinde
        birakilir ve UYARI ile isaretlenir - sunucudaki bayat yan dosya
        yanlis alarm uretebilir, dosya kendi kendine supheli sayilmaz.
        'found': bu ana kadar bulunan yan dosya ozetleri (None = hepsi)."""
        explicit = self.spec.wanted()
        found = {k: v for k, v in (self._sidecar if found is None
                                   else found).items()
                 if k not in explicit}
        if not explicit and not found:
            return
        got = util.file_digests(str(dest), {**found, **explicit})
        bad_expl = [a for a, v in explicit.items() if got.get(a) != v]
        bad_found = [a for a, v in found.items() if got.get(a) != v]
        self.extra["verified"] = [a for a in got
                                  if a not in bad_expl and a not in bad_found]
        if bad_expl:
            a = bad_expl[0]
            self.state = JobState.ERROR
            self.error = ("saglama uyusmazi: beklenen "
                          f"{explicit[a][:12]}..., gelen "
                          f"{(got.get(a) or '')[:12]}... ({a})")
            self.error_code = "checksum_mismatch"
            self.extra["checksum"] = "mismatch"
        elif bad_found:
            self.extra["checksum"] = "warn"
            self.extra["checksum_note"] = bad_found[0]
        else:
            self.extra["checksum"] = "ok"

    def _verify_late(self, found: dict) -> None:
        """Is bittikten sonra bulunan yan dosya: dosya yerinde, yalnizca
        dogrulandi/uyari rozeti guncellenir (gecikme hataya cevrilmez)."""
        if self.state != JobState.DONE or not self.final_path.is_file():
            return
        got = util.file_digests(str(self.final_path), found)
        bad = [a for a, v in found.items() if got.get(a) != v]
        with self._lock:
            if self.state != JobState.DONE:
                return
            if bad:
                self.extra["checksum"] = "warn"
                self.extra["checksum_note"] = bad[0]
                self.extra["verified"] = [a for a in
                                          self.extra.get("verified", [])
                                          if a not in bad]
            else:
                self.extra["checksum"] = "ok"
                self.extra["verified"] = sorted(
                    set(self.extra.get("verified", [])) | set(got))
        self._changed()

    def _fail(self, msg: str, code: str = "unknown"):
        with self._lock:
            if self.state == JobState.CANCELED:
                return
            self.state = JobState.ERROR
            self.error = msg
            self.error_code = code
            self.speed = 0
        self._stop.set()  # diger iscalar kalan araliklari katlasin
        with self._cond:
            self._cond.notify_all()
        self._checkpoint(force=True)
        self._changed()

    # -------------------------------------------------------------- control
    def pause(self):
        """Durdurur ve (en cok ~8 sn) havuzun kapanmasini bekler: komut
        dondugunde kalan araliklar kuyruktadir, denetim noktasi
        yazilmistir ve durum 'paused' dir."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()       # park etmis iscalar uyansin
        m = self._monitor
        if m is not None and m is not threading.current_thread():
            m.join(timeout=8.0)
        if self.kind == "ftp" and self.state == JobState.DOWNLOADING:
            # FTP tek akis: 8 sn icinde donmemis olabilir (yavas baglanti);
            # akis dur sinyalini gorur ve yazmaz, nokta burada tamamlanir.
            self._checkpoint(force=True)
            with self._lock:
                if self.state == JobState.DOWNLOADING:
                    self.state = JobState.PAUSED
        self._changed()

    def resume(self):
        if self.state in (JobState.PAUSED, JobState.ERROR, JobState.QUEUED):
            self.start()

    def cancel(self, delete_files: bool = False):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._monitor:
            self._monitor.join(timeout=5)
        self._join_threads(2.0)
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
                # eldeki araliklar da yazilir: duraklama/cokme aninda bir
                # iscinin elinde kalan bolum yeniden indirilir, kaybolmaz
                "ranges": [list(r) for r in self._pending] +
                          [list(r) for r in self._inflight.values()],
                "done": self.done,
                "kind": self.kind,
                "allowed": self._allowed,
                "validator_src": self._vsrc,
                "ftp_mtime": self._ftp_mtime,
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
