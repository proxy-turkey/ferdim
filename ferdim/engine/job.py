"""DownloadJob: dinamik segmentli indirme isi.

- N is parcasi; biten isci en buyuk kalan araligin ikinci yarisini calar
  (dinamik segmentasyon + is calma).
- Her is .part dosyasina os.pwrite ile yazar; meta JSON'u periyodik diske
  dokulur, boylece elektrik kesilse bile kaldigi yerden devam eder.
- If-Range ile ETag/Last-Modified dogrulamasi: kaynak degismisse bastan baslar.
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import os
import random
import re
import socket
import stat
import threading
import time
import urllib.error
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import cdc, tcpinfo, util
from ..coexist import LatencySignal
from ..paths import AppEnv, fsync_dir
from ..profiles import host_key, open_profiles
from ..ratelimit import TokenBucket
from . import dash, ftp, hls, httpcli, segments, socks

CHUNK = 1 << 16            # 64 KB okuma blogu
MIN_SPLIT = 1 << 20        # 1 MB'dan kucuk aralik bolunmez
CHECKPOINT_INTERVAL = 2.0  # sn
SWRR_FLOOR = 0.05          # yavas kaynaklar bile en iyinin %5'i kadar pay alir
RATE_WINDOW = 2.0          # kaynak hizi olcum penceresi (sn)
SIDECAR_ALGOS = ("sha256", "sha512", "md5", "sha1")   # en gucluden
SIDECAR_WAIT = 5.0         # sn: bitiste henuz aranan yan dosya bu kadar beklenir
PROFILE_MIN_BYTES = 1 << 20   # daha az aktarim profil ogrenmek icin yetersiz
MERKLE_REPAIR_MAX = 2      # bozuk parca onarim turu (sonra merkle_mismatch)
TCP_SAMPLE_BYTES = 4 << 20  # TCP_INFO ornekleme araligi (link basina)
CONGESTION_UP_BACKOFF = 30.0  # sn: tikaniklik tavaninda yukari kesif arasi
CONGESTION_GAIN = 1.05     # "+1 link" kazanci esigi (altinda kazanc yok)
PEER_PREFILL_BUDGET = 3.5  # sn: esbaz taramasi (pause 8 sn bekler)
# yumusak 404: govdesi HTML olan yanit, adi baska tur vaat ediyorsa uyari
_HTML_EXT = frozenset({".html", ".htm", ".xhtml", ".shtml", ".php",
                       ".asp", ".aspx", ".jsp", ".cgi"})
_HTML_HEAD = (b"<!doctype html", b"<html", b"<head", b"<body")


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
    if isinstance(e, socks.SocksError):
        return str(e)
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        if isinstance(r, (TimeoutError, ConnectionError, socks.SocksError)):
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


class _ChunkCheck:
    """Merkle parcalarini AKIS uzerinde dogrular (tek okuma).

    Bildirim ofsetleri bitisiktir (cdc.Manifest.parse dogrular), bu yuzden
    okunan baytlar parca sinirlariyla sirayla eslesir: yeniden parcalama
    (pahali FastCDC taramasi) ya da ikinci okuma gerekmez."""

    def __init__(self, man):
        self._chunks = man.chunks
        self.bad: list[tuple[int, int]] = []
        self._i = 0
        self._h = None
        self._left = 0
        self._off = 0
        self._len = 0
        self._hx = ""

    def feed(self, _offset: int, block: bytes) -> None:
        pos = 0
        n = len(block)
        while pos < n and self._i < len(self._chunks):
            if self._h is None:
                self._off, self._len, self._hx = self._chunks[self._i]
                self._h = hashlib.sha256()
                self._left = self._len
            take = min(self._left, n - pos)
            self._h.update(block[pos:pos + take])
            self._left -= take
            pos += take
            if self._left == 0:
                if self._h.hexdigest() != self._hx:
                    self.bad.append((self._off, self._len))
                self._h = None
                self._i += 1


def _verify_pass(path, algos, man=None, halt=None, progress=None):
    """Dosyayi TEK okumayla: istenen ozetler + (varsa) merkle parcalari.

    Donus: ({algo: ozet}, merkle). merkle: None (bildirim yok) ya da
    (boyut uygun mu, bozuk parcalar) - cdc.verify_file ile ayni anlam.
    Duraklat/iptal (halt) keserse ({}, None): karar yazilmaz."""
    chk = None
    size_ok = True
    if man is not None:
        try:
            size_ok = os.stat(path).st_size == man.size
        except OSError:
            size_ok = False
        if size_ok:
            chk = _ChunkCheck(man)
    got = util.file_digests(path, algos, halt=halt, progress=progress,
                            on_block=chk.feed if chk is not None else None)
    if man is None:
        return got, None
    return got, (size_ok, chk.bad if chk is not None else [])


def _identity_encoding(value: str) -> bool:
    """Content-Encoding 'sikistirma yok' mu? Virgullu liste desteklenir.

    Kabul: bos, `identity`, `none` ve her ogesi bunlardan olan listeler
    (`identity, identity`). Baska her oge (gzip, br, deflate...) reddedilir:
    aralikli indirmede govde baytlari Range ofsetleriyle eslesmez."""
    parts = [t.strip().lower() for t in str(value or "").split(",")]
    return all(t in ("", "identity", "none") for t in parts)


def _stat_key(path) -> tuple:
    """Dogrulama sonucunun hangi dosya durumuna ait oldugu (onbellek anahtari)."""
    try:
        st = os.stat(path)
    except OSError:
        return ()
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _looks_html(head: bytes) -> bool:
    """Govde bir HTML sayfasi gibi mi basliyor? (BOM/bosluk atlanir.)"""
    s = head.lstrip(b"\xef\xbb\xbf \t\r\n")[:512].lower()
    return s.startswith(_HTML_HEAD)


def _looks_like_soft_404(name: str, content_type: str, path, kind: str) -> bool:
    """Yumusak 404: sunucu 'dosya yok' sayfasi (HTML) donduyse ama adi
    baska bir tur vaat ediyorsa indirme sessizce 'done' olmamali.

    Tetiklenmez: ad zaten HTML (uzanti .html/.htm...) ya da uzantisiz.
    Content-Type HTML ise dosya acilmadan karar verilir; tur
    bildirilmemisse govdenin ilk baytlari koklanir (yalniz HTTP islerinde:
    FTP/HLS'te tur yok, yanlis alarm uretmemeli).
    """
    ext = Path(name or "").suffix.lower()
    if not ext or ext in _HTML_EXT:
        return False
    if "html" in (content_type or "").lower():
        return True
    if kind != "http":
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError:
        return False
    return _looks_html(head)


class MerkleError(Exception):
    """Acikca verilen merkle bildirimi okunamadi/gecersiz (kalici hata)."""


_SRC_CODES = {           # kaynak basarisizlik nedeni -> kararli hata kodu
    "gone": "not_found", "conn": "network", "stall": "stall",
    "reset": "network", "read": "network", "http": "unknown",
    "size": "size_mismatch", "encoding": "content_encoding",
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
    if isinstance(e, (SourceInvalid, ftp.FtpError, socks.SocksError)):
        return e.code
    if isinstance(e, MerkleError):
        return "merkle_manifest"
    if isinstance(e, (TimeoutError, ConnectionError)):
        return "network"
    if isinstance(e, OSError):
        if e.errno in util.DISK_ERRNOS:
            return "disk"
        return "network"
    return "unknown"


def _socks_auth_error(e: BaseException) -> bool:
    """Gecit kimlik/izin hatasi kalicidir: parola degismedikce tekrar
    denemek yalnizca geri cekilme gecikmesi ekler."""
    if isinstance(e, urllib.error.URLError):
        e = e.reason
    return isinstance(e, socks.SocksError) and e.code in ("auth", "forbidden")


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
    http3: str = ""                  # "" -> cfg["http3"] (off|auto|force)
    # Ozel pencere isi (eklenti 'ephemeral' ile isaretler): kuyruk ve
    # gecmis diske cerez/baslik/parola yazmaz. Bellekteki is etkilenmez.
    ephemeral: bool = False
    limit: int = 0  # is basina bayt/sn
    mirrors: list = field(default_factory=list)  # alternatif kaynak URL'leri
    sha256: str = ""   # bitiste dogrulanir
    md5: str = ""
    checksums: dict = field(default_factory=dict)  # digerleri: sha1, sha512...
    merkle: str = ""   # .ferdim-merkle bildirimi: URL ya da yerel yol

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
            http3=self.http3 or str(cfg.get("http3", "off")),
            # kimlik kapsami BIRINCIL adresin kokenidir: farkli kokenli
            # ayna ya da yonlendirme hedefi parolayi/cerezi almaz (ayni
            # kokenli ayna kimligi korur). Bak: httpcli.headers_for
            origin=httpcli.origin_of(self.url),
        )


class DownloadJob:
    def __init__(self, env: AppEnv, cfg: dict, spec: JobSpec,
                 job_id: str | None = None, on_change=None):
        self.env = env
        self.cfg = cfg
        self.spec = spec
        self.id = job_id or uuid.uuid4().hex[:10]
        # kilit ve durum sayaci 'state' ozelliginden ONCE kurulur
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._state = JobState.QUEUED
        self._active_secs = 0.0       # connecting+downloading toplam saniye
        self._active_mark: float | None = None
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

        self._stop = threading.Event()
        # start() penceresi: _prepare + monitor yayini bitene kadar temiz.
        # pause()/cancel() bunu bekler; yoksa monitor henuz yayinlanmamisken
        # (ya da onceki kosunun olu monitor'u ile) 'downloading' durumda
        # donerlerdi.
        self._start_done = threading.Event()
        self._start_done.set()
        # kosu kimligi: her start()/cancel() artirir. Baslatma icinde
        # (esbaz onbellegi gibi) uzun suren bir is, sonucu uygulamadan once
        # kimligi dogrular: eski kosunun baytlari yeni kosuya karismaz.
        self._run = 0
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
        # birlikte yasam tavani (kuyruk kurar): hat mesgulken baglanti
        # sayisi bunu asamaz; None = tavan yok (varsayilan)
        self._ext_cap: int | None = None
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
        # .part'a yazilan toplam bayt (tek yonlu sayac) ve son fsync'te
        # guvenceye alinan degeri: nokta yalnizca ARADAKI fark varsa fsync eder
        self._wrote = 0
        self._durable_wrote = 0
        self._speed_done_mark = 0
        self._speed_mark_ts = time.monotonic()
        # gecmis icin baglanti sayisi ornekleri (kuyruk tick'i toplar)
        self._links_sum = 0
        self._links_samples = 0
        self._links_peak = 0
        self._meta: dict = {}
        self.extra: dict = {}
        # --- kaynak sagligi (birincil + aynalar) ve yeniden deneme ---
        self._srcs: list[_Src] = []
        self._vsrc = ""            # ETag/Last-Modified hangi kaynaktan geldi
        self._sidecar: dict = {}   # otomatik bulunan saglama degerleri
        self._merkle = None        # cdc.Manifest: parca bazli dogrulama
        self._merkle_auto = False  # kaynak: yan dosya (bayat olabilir)
        self._merkle_tries = 0     # yapilan parca onarim turu
        self._merkle_res = None    # verilen dosya durumu icin merkle sonucu
        self._merkle_key = ()      # sonucun ait oldugu dosya (_stat_key)
        self._content_type = ""    # sunucunun bildirdigi tur (yumusak 404)
        self._reserved: Path | None = None   # sahiplenilmis son ad (yer tutucu)
        self._sc_lock = threading.Lock()   # yan dosya <-> bitis el skmasmasi
        self._finalized = threading.Event()
        self._dir: Path | None = None    # hedef klasor: ilk erisimde sabitlenir
        self._retries = 5
        self._stall = 20           # sn: bu kadar sure bayt gelmezse akis durdu
        self._restarts = 0
        self._rand = random.Random()
        self._limit_mark = 0       # limit sayacini sifirlayan ilerleme isareti
        # --- ogrenilen ana makine profili (1.6): olcumler bitiste bildirilir
        self._host = ""            # profil anahtari: probe sonunda belli olur
        self._probe_rtt = 0.0      # sn: yoklama yanitina kadar gecen sure
        self._seeded = False       # profil baslangici is basina bir kez
        self._reported_done = 0    # ayni baytlar iki kez bildirilmez
        self._throttle_seen = False
        self._safe_links = 0       # 429/503 sonrasi sunucunun kabul ettigi sayi
        # metrics ucu: tur -> {ana makine: sayac} (kimlik bilgisi yok)
        self.stats_counters = {"retries": {}, "throttles": {}}
        # gecikme sinyali (birlikte yasam): aralik isteklerinin TTFB'si
        self._lat = LatencySignal()
        # tikaniklik sinyali (2.x): cekirdek TCP_INFO ornekleri (istege
        # bagli; kapaliyken governor davranisi birebir eskisi gibi kalir)
        self._tcp_on = bool(cfg.get("tcp_info", True))
        self._cong = tcpinfo.CongestionSignal()
        self._reset_sources()

    # ----------------------------------------------------------------- durum
    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        """Durum gecisi aktif sureyi biriktirir. Gecmis istatistigindeki
        'sure' yalniz connecting/downloading saniyeleridir: kuyrukta bekleme
        ve duraklama sayilmaz (eski kayit bitisten olusturulma zamanini
        cikariyordu, duraklatilan is oldugundan uzun gorunuyordu)."""
        now = time.monotonic()
        with self._lock:
            if self._active_mark is not None:
                self._active_secs += max(0.0, now - self._active_mark)
                self._active_mark = None
            if value in (JobState.CONNECTING, JobState.DOWNLOADING):
                self._active_mark = now
            self._state = value

    def active_seconds(self, now: float | None = None) -> float:
        """Aktif gecen toplam sure (suren dilim dahil)."""
        mark = self._active_mark
        if mark is None:
            return self._active_secs
        return self._active_secs + max(0.0, (now or time.monotonic()) - mark)

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
            if self.extra.get("merkle") == "warn":
                warnings.append("merkle_mismatch")
            if self.extra.get("warning"):
                warnings.append(self.extra["warning"])
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
                "merkle": self.extra.get("merkle", ""),
                "checksum_note": self.extra.get("checksum_note", ""),
                "merkle_note": self.extra.get("merkle_note", ""),
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
                "verify_done": int(self.extra.get("verify_done") or 0),
                "verify_total": int(self.extra.get("verify_total") or 0),
                "warning": str(self.extra.get("warning") or ""),
                "best_speed": int(self._gov["best"]),
            }

    def _changed(self):
        self.on_change(self)

    # ----------------------------------------------------------------- start
    def start(self, *, queued_only: bool = False):
        """Baslatir. queued_only: yalnizca hala KUYRUK'ta olan isi baslatir
        (zamanlayici boyle cagirir); bu arada kullanici duraklattiysa is
        baslamaz - eskiden zamanlayici duraklamayi sessizce ezerdi."""
        with self._lock:
            if self.state in (JobState.DOWNLOADING, JobState.CONNECTING):
                return
            if queued_only and self.state != JobState.QUEUED:
                return
            self._run += 1             # yeni kosu: onceki hazirligin sonucu gecersiz
            self._start_done.clear()
            self._stop.clear()
            self.error = ""
            self.error_code = ""
            self._merkle_tries = 0      # elle yeniden deneme yeni butce verir
            self.state = JobState.CONNECTING
            self._reset_sources()
        self._changed()
        try:
            try:
                self._prepare()
            except Exception as e:
                # duraklatma hazirligi yarida kesti: bu bir hata degil
                if self._stop.is_set():
                    self._fold_stopped()
                    return
                # hazirlik yarida kaldi: acilan .part taniticisi sizmasin
                # (ve hata, onceki iyi denetim noktasini ezmesin: fd yoksa
                # _checkpoint yazmaz)
                self._close_fd()
                self._fail(_describe_error(e), _code_for(e))
                return
            # _prepare() bu arada pause()/cancel() gormus olabilir. Karari
            # AYNI kilit altinda ver: DOWNLOADING yazip isci baslatmak
            # durdurma/iptal kararini ezerdi.
            with self._lock:
                stopped = self._stop.is_set() or self.state == JobState.CANCELED
                if not stopped:
                    self.state = JobState.DOWNLOADING
            if stopped:
                self._fold_stopped()
                return
            self._changed()
            if self.kind == "http":
                self._start_mirror_probe()
            self._spawn()
        finally:
            self._start_done.set()

    def _fold_stopped(self):
        """start() pause/cancel gordu: is 'paused' kalir, nokta yazilir;
        hazirligin yarida kesilmesi HATA degildir. (_checkpoint yarim
        hazirligi yazmaz: _fd acilmadan durum diske dokulmez.)"""
        pausing = False
        with self._lock:
            if self.state != JobState.CANCELED:
                self.state = JobState.PAUSED
                pausing = True
            self._cond.notify_all()
        if pausing:
            self._checkpoint(force=True)
        self._changed()

    def _close_fd(self) -> None:
        """Acik .part taniticiyi kapatir. Her hazirlik ONCE kapatir: duraklat
        /devam dongusunde her turda bir fd acikta kaliyordu (sizinti).
        Kilit altinda: esbaz onbelleginin yazma denetimi fd'yi AYNI anda
        gormeli (kapanmis numara yeni kosuda yeniden kullanilabilir)."""
        with self._lock:
            fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _drop_reservation(self) -> None:
        """Sahiplenilmis ama doldurulmamis son ad yer tutucusunu siler.

        Kilit altinda: yayinlama ile ayni kritik bolumde karar verilir.
        Yayinlandiysa (`_reserved` replace sonrasi temizlenir) hicbir sey
        silinmez - gercek dosya yer tutucu sanilip kaldirilmaz."""
        with self._lock:
            p, self._reserved = self._reserved, None
            if p is None or self._finalized.is_set():
                return
            try:
                p.unlink()
            except OSError:
                pass

    def _cleanup_temp(self) -> None:
        """Isin gecici dosyalari: yarim `.part` ve HLS/DASH `.segs/` klasoru.

        Iptal/kaldirmada kosulsuz silinir (is artik devam etmez). HATA
        durumunda cagrilmaz: ERROR isi `resume`/`restart` ile yarim dosyadan
        surdurulebilir (bkz. `_prepare` devam yolu, FTP REST noktasi)."""
        try:
            self.part_path.unlink(missing_ok=True)
        except OSError:
            pass
        segments.cleanup(self.meta_path)

    def _prepare(self):
        opt = self.spec.options(self.cfg)
        # yoklama/yedege gecme dongusu de yapilandirilan deneme sayisini kullanir
        self._retries = max(0, int(opt.retries))
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self._close_fd()             # onceki kosudan acik kalan fd kapatilir
        # HLS / DASH manifest mi? (FTP semasi once gelir: ftp://...m3u8
        # bir FTP dosyasidir, manifest degil)
        resume_meta = util.read_json(self.meta_path) or {}
        # onceki kosu adi sahiplenip yayinlayamadan olduyse bos yer tutucu kalir
        self._drop_stale_reservation(resume_meta)
        if self.kind == "ftp" or ftp.is_ftp(self.url) or \
           resume_meta.get("kind") == "ftp":
            self.kind = "ftp"
            self._host = host_key(self.url)   # profil: RTT + hiz bildirir
            self._merkle_http_only()
            if not self.final_filename:
                self.final_filename = util.filename_from_url(self.url)
            self._meta = resume_meta
            self._prepare_ftp()
            return
        if self.kind == "hls" or httpcli.is_hls(self.url, "") or \
           resume_meta.get("kind") == "hls":
            self.kind = "hls"
            self._host = host_key(self.url)
            self._merkle_http_only()
            if not self.final_filename:
                self.final_filename = util.filename_from_url(self.url) + ".ts"
            name = self.final_filename = _ensure_ext(self.final_filename)
            self._fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT, 0o644)
            self._meta = resume_meta
            return
        if self.kind == "dash" or dash.is_dash(self.url, "") or \
           resume_meta.get("kind") == "dash":
            self.kind = "dash"
            self._host = host_key(self.url)
            self._merkle_http_only()
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
                t0 = time.monotonic()
                p = httpcli.probe(src.url, opt)
                # RTT bandi: yanit basliklarina kadar gecen sure (ek istek yok)
                self._probe_rtt = max(0.0, time.monotonic() - t0)
                self._vsrc = src.url
                # profil anahtari: veriyi gercekten sunan ana makine
                self._host = host_key(p.final_url or src.url)
                # gecikme sinyali: yoklama TTFB'si tabanin ilk ornegidir
                self._lat.add(self._host, self._probe_rtt)
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
                if _socks_auth_error(e):
                    self._src_kill(src, "conn", _code_for(e))
                else:
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
        self._content_type = p.content_type or ""

        if self._resumable(resume_meta, p):
            self._pending = [tuple(r) for r in resume_meta["ranges"]]
            self.done = max(0, self.total - sum(e - s + 1 for s, e in self._pending)) \
                if self.total > 0 else int(resume_meta.get("done", 0))
            # sunucunun kabul ettigi isci sayisini hatirla
            if resume_meta.get("allowed"):
                self._allowed = max(1, min(int(resume_meta["allowed"]),
                                           self._ceiling_locked()))
                self.extra["adaptive"] = self._allowed
            fresh = False
        elif self._publish_ready(resume_meta, p):
            # her sey inmis, yalnizca yayinlanmamis (cokme penceresi):
            # yeniden indirmek yerine dosya sahiplenilip bitirilir
            self._pending = []
            self.done = self.total
            fresh = False
        else:
            self._fresh_start(p)
            fresh = True
        self._fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT, 0o644)
        if fresh:
            # bastan: eski .part'in kuyrugu KALMAZ. Bilinmeyen boyutta
            # (total = -1) ftruncate(total) yok; kirpilmazsa eski kuyruk
            # yeni icerigin ardinda kalir ve is yanlislikla DONE olur.
            os.ftruncate(self._fd, 0)
        if self.total > 0:
            os.ftruncate(self._fd, self.total)
        self._meta = resume_meta
        # kaynak kapasiteleri: her kaynak isin izin verdigi kadar baglanti
        # alabilir; limit sinyali gelince kaynak bazinda daralir
        for s in self._srcs:
            s.cap = max(1, self._allowed)
        # ogrenilmis tavandan basla (soguk baslangic yok); governor kesif
        # yonunu yine kendi olcumleriyle belirler
        self._seed_profile()
        self._retries = max(0, int(opt.retries))
        try:
            self._stall = max(5, min(120, int(self.cfg.get("stall_timeout", 20))))
        except (TypeError, ValueError):
            self._stall = 20
        self._load_merkle(opt)
        self._peer_prefill(opt)     # esbaz onbellegi (istege bagli): eksik parcalar
        self._start_sidecar(opt)

    def _resumable(self, resume_meta: dict, p: httpcli.ProbeResult) -> bool:
        """Onceki denetim noktasindan devam edilebilir mi?

        Sunucu ne ETag ne Last-Modified verdiyse ('"" == ""') esitlik her
        zaman saglanir ve degismis bir dosya sessizce birlestirilirdi:
        dogrulayici yoksa BASTAN indirilir. Acik bir saglama/merkle
        verilmisse devam guvenlidir - degisim bitis dogrulamasinda yakalanir.
        """
        if resume_meta.get("url") != self.url \
                or resume_meta.get("etag") != p.etag \
                or resume_meta.get("last_modified") != p.last_modified \
                or not resume_meta.get("ranges") or not self.part_path.exists():
            return False
        if not (p.etag or p.last_modified) \
                and not (self.spec.wanted() or self.spec.merkle):
            return False
        return True

    def _publish_ready(self, resume_meta: dict, p: httpcli.ProbeResult) -> bool:
        """Nokta 'her sey indi, yalnizca yayinlanmadi' mi diyor?

        Indirme bittikten sonra `_finalize_file` calismadan cokme olursa
        denetim noktasi bos aralik listesi + done == total tasir ve .part
        tamdir: yeniden indirmek yerine dosya yayinlanir. Dogrulayici
        kosulu `_resumable` ile aynidir (dogrulayicisiz veri guvenilmez)."""
        if self.total <= 0 or resume_meta.get("url") != self.url:
            return False
        if resume_meta.get("etag") != p.etag \
                or resume_meta.get("last_modified") != p.last_modified:
            return False
        if resume_meta.get("ranges"):
            return False
        if int(resume_meta.get("done") or 0) != self.total:
            return False
        if not (p.etag or p.last_modified) \
                and not (self.spec.wanted() or self.spec.merkle):
            return False
        try:
            return self.part_path.stat().st_size == self.total
        except OSError:
            return False

    def _record_reservation(self, dest: Path) -> None:
        """Sahiplenilen son adi noktaya yazar (cokme kurtarmasi icin).

        Yer tutucunun kimligi (dev, ino) de saklanir: bir sonraki hazirlik
        yalnizca HALA BOS ve AYNI yer tutucu olan dosyayi siler - yayinlanmis
        (inode'u degismis) dosyaya asla dokunulmaz."""
        try:
            st = dest.stat()
        except OSError:
            return
        meta = util.read_json(self.meta_path) or {}
        meta["reserved"] = [dest.name, st.st_dev, st.st_ino]
        try:
            util.atomic_write_json(self.meta_path, meta)
        except OSError:
            pass          # kurtarma kaydi yardimci: yayini engellemez

    def _drop_stale_reservation(self, meta: dict) -> None:
        """Onceki kosunun yayinlanmamis ad yer tutucusunu siler."""
        rec = (meta or {}).get("reserved")
        if not (isinstance(rec, list) and len(rec) == 3):
            return
        name, dev, ino = rec
        if not isinstance(name, str) or not name or name != Path(name).name:
            return
        p = self.target_dir / name
        try:
            st = p.stat()
        except OSError:
            return
        if st.st_size == 0 and st.st_dev == dev and st.st_ino == ino:
            try:
                p.unlink()
            except OSError:
                pass

    def _fresh_start(self, p: httpcli.ProbeResult):
        self._reset_verify_extra()      # bastan: eski uyarilar tasinmaz
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
            user=self.spec.user, password=self.spec.password,
            proxy=opt.proxy)
        self._fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT, 0o644)
        part = self.part_path.stat().st_size if self.part_path.exists() else 0
        if size >= 0 and part > size:
            # yerel dosya sunucudakinden buyuk: ayna/eski kalinti, bastan
            os.ftruncate(self._fd, 0)
            part = 0
            self._reset_verify_extra()
        old = (self._meta or {}).get("ftp_mtime")
        if part > 0 and mtime and old and old != mtime:
            # kaynak degismis: yarim dosya yeni icerikle karistirilamaz
            os.ftruncate(self._fd, 0)
            part = 0
            self._reset_verify_extra()
        self._ftp_mtime = mtime
        self.total = size
        self.done = part if size < 0 else min(part, size)

    # ---------------------------------------------------------------- workers
    def _start_workers(self):
        """Isci havuzunu (yeniden) kurar; monitor'e dokunmaz.

        Parca onarimi ayni monitor dongusunde yeni araliklarla cagirir:
        ikinci bir monitor baslatmak bitisi iki kez tetiklerdi."""
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

    def _spawn(self):
        self._start_workers()
        # monitor EN SON yayinlanir: panelde gorunen durum ile join edilecek
        # halka ayni kusaga ait olsun (pause/cancel _start_done'dan sonra
        # okur, o yuzden atama sirasi onemli).
        mon = threading.Thread(target=self._monitor_loop, daemon=True)
        mon.start()
        self._monitor = mon

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

    def _ceiling_locked(self) -> int:
        """Baglanti tavani: sunucu tavani ile dis (birlikte yasam)
        tavaninin kucugu. Dis tavan yoksa yalniz sunucu tavani."""
        if self._ext_cap is None:
            return self._seg_max
        return max(1, min(self._seg_max, self._ext_cap))

    def set_external_cap(self, cap: int | None) -> None:
        """Kuyrugun kurdugu dis tavan (birlikte yasam modu).

        Hat mesgulken baglanti sayisi bu siniri asamaz: governor tavani
        asla yoklamaz. Tavan kalkinca (None) kesif hemen yeniden baslar;
        varsayilan azami guc davranisi degismez.
        """
        with self._cond:
            new = None if cap is None else max(1, int(cap))
            if new == self._ext_cap:
                return
            self._ext_cap = new
            if new is None:
                if self.extra.get("adaptive_reason") == "coexist":
                    self.extra.pop("adaptive_reason", None)
                self._gov["next_up"] = time.monotonic()
                self._gov["cooldown"] = 0.0
                self._cond.notify_all()
                return
            if self._allowed > new:
                self._allowed = self._ceiling_locked()
                self._gov["probe"] = False       # yarim kesif iptal
                self.extra["adaptive"] = self._allowed
                self.extra["adaptive_reason"] = "coexist"
                self._cond.notify_all()

    def _set_allowed(self, n: int) -> None:
        with self._cond:
            n = max(1, min(int(n), self._ceiling_locked()))
            if n != self._allowed:
                self._allowed = n
                if n < self._seg_max:
                    self.extra["adaptive"] = n
                else:
                    self.extra.pop("adaptive", None)
                    if self.extra.get("adaptive_reason") == "congestion":
                        self.extra.pop("adaptive_reason", None)
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

    def _congested(self) -> bool:
        """Kernel TCP_INFO tikaniklik sinyali (kapaliyken daima False)."""
        return bool(self._tcp_on and self.kind == "http"
                    and self._cong.congested())

    def _flag_congestion_locked(self) -> None:
        """Tikaniklik tavanini gorunur kilar (birlikte yasam nedenini ezmez)."""
        if self.extra.get("adaptive_reason") == "coexist":
            return
        if self._allowed < self._seg_max:
            self.extra["adaptive"] = self._allowed
            self.extra["adaptive_reason"] = "congestion"

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
                gain = sample > 0 and ema > g["ref_speed"] * CONGESTION_GAIN
                if g["dir"] > 0 and accept and not gain and self._congested():
                    # ek baglanti hiz getirmedi + yeniden iletim/RTT
                    # tirmandi: bunu bir tavan say (varsayilan davranis
                    # yalnizca tcp_info acikken bu kadar muhafazakar)
                    self._set_allowed(g["ref_allowed"])
                    g["cooldown"] = now + 15.0
                    g["next_up"] = now + CONGESTION_UP_BACKOFF
                    self._flag_congestion_locked()
                    return
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
            cong = self._congested()
            if cong:
                # tikaniklik varken yukari kesif yok; asagi kesif kosulu
                # (ema >= best*0.9) degismeden calisir
                g["next_up"] = max(g["next_up"], now + CONGESTION_UP_BACKOFF)
                self._flag_congestion_locked()
            elif self.extra.get("adaptive_reason") == "congestion":
                self.extra.pop("adaptive_reason", None)
            limit_free = (now - self._last_limit_ts) > 60.0
            allowed = self._allowed
            if allowed < self._ceiling_locked() and limit_free and not cong \
                    and (ema < g["best"] * 0.8 or now >= g["next_up"]):
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
        self._cong.reset()          # kaynaklar tazelenir: eski link ornekleri gecersiz
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
        # olumlu kaynak gecikme sinyalinden hemen dusurulur: yoksa terk
        # edilmis yavas bir ayna tavan sinyalini kalici sisirirdi
        self._lat.drop(host_key(s.url))
        self.extra["sources_dead"] = sum(1 for x in self._srcs if x.dead)

    def _bump(self, kind: str, s: "_Src | None" = None) -> None:
        """Prometheus sayaci: tur basina ana makine.

        Metrik anlik goruntusu `_lock` altinda alinir; sayac da ayni kilit
        altinda guncellenir (kilit zaten tutuluyorsa RLock yeniden girer).
        """
        with self._lock:
            host = host_key(s.url) if s is not None else self._host
            d = self.stats_counters.setdefault(kind, {})
            key = host or "?"
            d[key] = d.get(key, 0) + 1

    def _src_fail(self, s: _Src, kind: str, cooldown: bool = True,
                  code: str = "") -> None:
        """Kaynak basarisizligi: secim agirligi yariya iner, ardisik
        basarisizlik sayisi artar; tekrar sayisini asarsa (ya da kaynak bu
        is icin gecersizse: 'gone') kaynak bu is icin olu sayilir.
        cooldown=False: baska kaynak yok, geri cekilme uykusu zaten var."""
        with self._cond:
            s.streak += 1
            s.fails += 1
            self._bump("retries", s)
            s.ema *= 0.5
            s.cur = 0.0
            s.reason = kind
            s.code = code or _SRC_CODES.get(kind, "unknown")
            if kind in ("gone", "encoding") or s.streak > self._retries:
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
            if reason.startswith("HTTP "):
                self._bump("throttles", s)      # 429/503
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

    def _note_limit(self, s: "_Src | None" = None):
        """Is genelinde limit sayaci. 1 MB'dan fazla ilerleme varsa sifirlanir:
        ilerleyen is hicbir zaman limit yuzunden basarisiz sayilmaz."""
        if self.done - self._limit_mark > (1 << 20):
            self._limit_mark = self.done
            self._limit_events = 0
        self._limit_events += 1
        self._bump("throttles", s)        # metrics: ana makine basina 429/503
        self._throttle_seen = True     # profilde "limitli sunucu" isareti

    def _dead_reason(self) -> str:
        names = {"conn": "baglanti hatasi", "http": "HTTP hatasi",
                 "gone": "dosya yok", "stall": "akis durdu",
                 "reset": "baglanti kesildi", "read": "okuma hatasi",
                 "size": "farkli dosya",
                 "encoding": "sikistirilmis yanit"}
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
                self._safe_links = self._allowed   # profilde guvenli tavan
                self.extra["adaptive"] = self._allowed
                self.extra["adaptive_reason"] = reason
            self._throttle_seen = True
            if src is not None:
                src.cap = max(1, min(src.cap, self._allowed))
            self._cond.notify_all()

    # ------------------------------------------- ogrenilen ana makine profili
    def _profile_store(self):
        """Profil deposu; ogrenme kapaliysa None (hicbir olcum yazilmaz)."""
        if not self.cfg.get("learn_hosts", True):
            return None
        return open_profiles(self.env)

    def latency_inflation(self) -> float:
        """Kendi baglantilarimizin TTFB sismesi (1.0 = normal).

        Kuyruk bunu birlikte yasam modunun gecikme sinyali olarak okur:
        kaynak bazinda guncel EMA'nin tabana (en iyi TTFB) orani."""
        return self._lat.inflation()

    def _seed_profile(self) -> None:
        """Isin baslangic noktasini ogrenilmis profilden kurar.

        Profil yalniz BASLANGIC noktasidir: governor yine yukari/asagi yoklar
        ve azami guc varsayilani degismez. Limit yemis ana makinede ogrenilmis
        tavan ceza tabani olarak da kurulur; 60 sn buyume penceresi korunur.
        """
        if self.kind != "http" or not self.cfg.get("learn_hosts", True) \
                or self._seeded:
            return
        self._seeded = True
        if not self._host or self.extra.get("adaptive"):
            return          # devam eden isin gercek olcumu profilden iyidir
        prof = open_profiles(self.env).get(self._host)
        if not prof:
            return
        try:
            links = int(prof.get("links") or 0)
        except (TypeError, ValueError):
            return
        if links < 1:
            return
        # tavan AYNI kilit altinda okunur ve uygulanir: araya giren
        # set_external_cap (birlikte yasam) is tavani asamaz
        with self._cond:
            links = max(1, min(links, self._ceiling_locked()))
            self._allowed = links
            if self._allowed < self._seg_max:
                self.extra["adaptive"] = self._allowed
                self.extra["adaptive_reason"] = "learned"
            if prof.get("throttled"):
                now = time.monotonic()
                self._server_floor = self._allowed
                self._last_limit_ts = now
                self._gov["next_up"] = now + 60.0
            for s in self._srcs:
                s.cap = max(1, self._allowed)
            self._cond.notify_all()

    def _report_profile(self) -> None:
        """Olculen degeri ana makine profiline bildirir (bitis/duraklama).

        Yalniz anlamli aktarim ve gercek bir governor olcumu varsa yazilir;
        profil hatasi isi asla etkilemez (depo hatalari kendi icinde yutulur).
        """
        if self.kind != "http" or not self.cfg.get("learn_hosts", True) \
                or not self._host:
            return
        with self._lock:
            done = self.done
            if done < PROFILE_MIN_BYTES or done <= self._reported_done:
                return
            ema = float(self._gov.get("ema") or 0.0)
            throttled = bool(self._throttle_seen or self._safe_links)
            if ema <= 0 and not throttled:
                return          # governor henuz hiz olcemedi: ogrenecek sey yok
            links = int(self._gov.get("best_workers") or self._allowed)
            links = max(1, min(links, self._seg_max))
            if self._safe_links:
                links = min(links, max(1, self._safe_links))
            allowed = max(1, self._allowed)
            per_link = 0.0 if self._bucket.rate > 0 else ema / allowed
            self._reported_done = done
        open_profiles(self.env).record(
            self._host, links=links, throttled=throttled,
            rtt=self._probe_rtt, per_link_bps=per_link)

    def _report_ftp_profile(self, started: float, first_byte: float,
                            moved: int, seconds: float) -> None:
        """FTP tek akis: yalniz RTT bandi ve baglanti basi hiz ogrenilir.

        `links`/`seg_workers` bilerek yazilmaz (FTP tek baglanti kurar,
        aralik islerinin tohumunu yanlis yere cekmemeli). Yeterli bayt
        tasinmadiysa ya da ogrenme kapaliysa hicbir sey yazilmaz."""
        store = self._profile_store()
        if store is None or not self._host or moved < PROFILE_MIN_BYTES:
            return
        rtt = max(0.0, first_byte - started) if first_byte else 0.0
        bps = moved / seconds if seconds > 0 else 0.0
        store.record(self._host, rtt=rtt, per_link_bps=bps)

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
        # SIKISTIRMA: istekler Accept-Encoding: identity ile gider (bkz.
        # httpcli.base_headers). Sunucu yine de sikistirdiysa govde Range
        # araliklariyla eslesmez: baytlar 'geldigi gibi' yazilirsa dosya
        # sessizce bozulur. Boyle bir kaynak bu is icin gecersizdir.
        enc = resp.headers.get("Content-Encoding") or ""
        if not _identity_encoding(enc):
            resp.close()
            raise SourceInvalid(
                f"sikistirilmis yanit (Content-Encoding: {enc})",
                "content_encoding")
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

    def _sample_tcp(self, resp, wid: int) -> None:
        """Range yanitinin soketinden TCP_INFO ornegi alir (ucuz, hata yutmaz).

        h3 (QUIC) yanitinda TCP soketi yoktur; ornek atlanir. Ornekleme
        kapaliysa (`tcp_info=false`) hicbir sey yapilmaz: governor birebir
        eski davranista kalir."""
        if not self._tcp_on or self.kind != "http":
            return
        try:
            sock = httpcli.response_socket(resp)
            if sock is None:
                return
            info = tcpinfo.read(sock)
            if info:
                self._cong.add((wid, id(sock)), info, time.monotonic())
        except Exception:
            pass

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
                t_open = time.monotonic()
                resp = self._open(src, opt, cursor, e)
                # TTFB: baglanti + basliklara kadar gecen sure. Ek istek
                # acilmaz; taban/EMA karsilastirmasi birlikte yasam
                # modunun gecikme sinyalini besler (eski kusak yazmaz).
                if gen == self._gen:
                    self._lat.add(host_key(src.url),
                                  time.monotonic() - t_open)
                    self._sample_tcp(resp, wid)
            except SourceInvalid as ex:
                # ayna baska dosya veriyor / Range desteklemiyor / sikistirilmis
                # yanit: isi bastan baslatmak YANLIS olur (sonsuz restart
                # dongusu); kaynak elenir, kalan kaynaklar surdurur
                self._src_fail(src, "gone" if ex.code != "content_encoding"
                               else "encoding", code=ex.code)
                if self._others_alive(src):
                    continue
                requeue()
                self._fail(f"{'ayna' if not src.primary else 'kaynak'} "
                           f"gecersiz: {ex}", ex.code)
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
                    self._note_limit(src)
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
                if _socks_auth_error(ex):
                    self._src_fail(src, "gone", cooldown=False,
                                   code=_code_for(ex))
                    self._fail(_describe_error(ex), _code_for(ex))
                    return (0.0, True)
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
            next_tcp = TCP_SAMPLE_BYTES
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
                        self._wrote += len(chunk)      # nokta fsync'i buna bakar
                        self._inflight[wid] = (cursor, e)
                    nbytes += len(chunk)
                    win_b += len(chunk)
                    if nbytes >= next_tcp:
                        # tikaniklik sinyali: link basina ~4 MB'da bir ornek
                        self._sample_tcp(resp, wid)
                        next_tcp += TCP_SAMPLE_BYTES
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
    def _merkle_http_only(self) -> None:
        """Merkle butun dosyanin bayt araliklarina dayanir: FTP/HLS/DASH
        isinde sessizce yok saymak kullanicinin dogrulama beklentisini bosa
        cikarirdi; acikca verilen bildirim kalici hata olur."""
        if self.spec.merkle:
            raise MerkleError("merkle yalnizca HTTP(S) isleri icin")

    def _merkle_text(self, want: str, opt) -> str:
        """Bildirim metni: URL ise kisa bir istek, degilse yerel dosya.
        Yerel okuma sinirlidir: normal dosya + MAX_MANIFEST ust siniri
        (aygit/boru ya da devasa dosya okunmaz)."""
        if want.startswith(("http://", "https://")):
            return httpcli.fetch_small(
                want, opt, timeout=min(max(int(opt.timeout), 2), 8),
                limit=cdc.MAX_MANIFEST)
        path = Path(want).expanduser()
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):
            raise MerkleError("merkle bildirimi normal dosya degil")
        if st.st_size > cdc.MAX_MANIFEST:
            raise MerkleError("merkle bildirimi cok buyuk")
        with path.open("rb") as f:
            raw = f.read(cdc.MAX_MANIFEST + 1)
        if len(raw) > cdc.MAX_MANIFEST:
            raise MerkleError("merkle bildirimi cok buyuk")
        return raw.decode("utf-8", "replace")

    def _load_merkle(self, opt) -> None:
        """Acikca verilen merkle bildirimini cozer (URL ya da yerel yol).

        Sessizce yok sayilmaz: kullanici parca dogrulamasi istedi, bildirim
        okunamiyorsa is merkle_manifest koduyla biter."""
        want = str(self.spec.merkle or "").strip()
        if not want:
            return
        try:
            text = self._merkle_text(want, opt)
            self._merkle = cdc.Manifest.parse(text)
        except MerkleError:
            raise
        except Exception as e:
            raise MerkleError(f"merkle bildirimi okunamadi: {e}") from e
        self._merkle_auto = False      # acikca verilen bildirim: kesin

    # ------------------------------------------------------ LAN esbaz onbellegi
    def _peer_prefill(self, opt) -> None:
        """Merkle parcalarini once LAN esbazlarindan doldurur (istege bagli).

        YALNIZCA ACIKCA verilen bildirimle calisir: kendiliginden bulunan
        `.ferdim-merkle` yan dosyasi (bayat olabilir, kimse imzalamadi) esbaz
        baytlarini indirmeye sokmaz - eski surum bir dosya "tamam" gorunurdu.
        Yalnizca TAMAMEN bekleyen bir araliga dusen parcalar denenir; her
        parca SHA-256 ile dogrulanmadan .part'a yazilmaz. Yazma ve
        `_pending`/`done` guncellemesi kosu kimligi (`_run`) ve `_stop` ile
        AYNI kilit altinda dogrulanir: duraklatma/iptal/restart sirasinda
        eski taramanin baytlari yeni kosuya karismaz. Butce kisa tutulur
        (PEER_PREFILL_BUDGET): pause() 8 sn bekler, hazirlik bunun altinda
        biter ve is PAUSED'a katlanir.
        """
        if self.kind != "http" or not bool(self.cfg.get("peer_cache")) \
                or self._stop.is_set() or self.total <= 0 \
                or self.extra.get("no_ranges"):
            return
        man = self._merkle
        if man is None or self._merkle_auto or man.size != self.total \
                or not str(self.spec.merkle or "").strip():
            return
        fd = self._fd
        if fd is None:
            return
        run = self._run

        def halted() -> bool:
            return self._stop.is_set() or run != self._run

        def write(off: int, data: bytes) -> bool:
            with self._lock:
                if halted() or self._fd is None:
                    return False
                try:
                    os.pwrite(self._fd, data, off)
                except OSError:
                    return False
                self._wrote += len(data)    # esbaz baytlari da diske inmeli
            return True

        try:
            from .. import peer
            filled = peer.open_peer(self.env).fetch_chunks(
                man, list(self._pending), fd, stop=halted, write=write,
                budget=PEER_PREFILL_BUDGET)
        except Exception:
            return
        if not filled:
            return
        with self._lock:
            if halted() or self.state == JobState.CANCELED:
                return
            holes = [(off, off + ln - 1) for off, ln in filled]
            self._pending = peer.subtract_ranges(self._pending, holes)
            nbytes = sum(ln for _, ln in filled)
            self.done += nbytes
            # esbaz baytlari agdan gelmedi: hiz ornegi sicramasin
            self._speed_done_mark = self.done
            self._gov["done"] = self.done
            self.extra["peer_chunks"] = int(
                self.extra.get("peer_chunks", 0)) + len(filled)
            self.extra["peer_bytes"] = int(
                self.extra.get("peer_bytes", 0)) + nbytes
        self._checkpoint(force=True)

    def _peer_index_add(self, dest: Path) -> None:
        """Merkle ile dogrulanmis bitmis dosyayi esbaz indeksine yazar."""
        man = self._merkle
        if man is None or not bool(self.cfg.get("peer_cache")):
            return
        try:
            from .. import peer
            peer.open_peer(self.env).add_verified(man, dest)
        except Exception:
            pass

    def _start_sidecar(self, opt):
        """Yan dosya saglamasi: <url>.ferdim-merkle ve <url>.sha256 / .md5 /
        .sha512 / .sha1 arka planda aranir; bulunanla indirme bitince
        dogrulanir ve merkle bildirimi varsa yalnizca bozuk parcalar yeniden
        iner. Acikca verilmis saglamalar eksikse calisir; isi yavaslatmaz."""
        self._sidecar = {}
        self._sidecar_t = None
        if self._stop.is_set():
            return
        on = bool(self.cfg.get("checksum_sidecar", True))
        algos = on and not all(a in self.spec.wanted() for a in SIDECAR_ALGOS)
        merkle = on and not self.spec.merkle and self._merkle is None
        if not algos and not merkle:
            return
        self._sidecar_t = threading.Thread(
            target=self._sidecar_loop, args=(opt, algos, merkle), daemon=True,
            name=f"ferdim-{self.id}-sidecar")
        self._sidecar_t.start()

    def _sidecar_loop(self, opt, algos: bool = True, merkle: bool = True):
        name = self.final_filename or util.filename_from_url(self.url)
        explicit = self.spec.wanted()
        if merkle:
            self._merkle_sidecar(opt)
        if not algos:
            return
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

    def _merkle_sidecar(self, opt) -> None:
        """Yayincinin dosya yanina koydugu `.ferdim-merkle` bildirimini arar.
        Bulunamaz/bozuksa yalnizca parca onarimi yapilmaz; indirme etkilenmez.
        Is bittikten sonra bulunan bildirim onarim penceresi kapandigi icin
        yok sayilir (gec gelen yan dosya dosyayi hatali yapmaz)."""
        try:
            text = httpcli.fetch_small(
                util.url_sibling(self.url, cdc.SUFFIX), opt,
                timeout=min(max(int(opt.timeout), 2), 8),
                limit=cdc.MAX_MANIFEST)
            man = cdc.Manifest.parse(text)
        except Exception:
            return
        with self._sc_lock:
            if self._finalized.is_set():
                return
            self._merkle = man
            self._merkle_auto = True   # yan dosya: bayat olabilir, onarilmaz

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
            if gen != self._gen:
                return  # duraklat/devam: eski kosu yeni kusaga karismaz
            if self._stop.is_set():
                # duraklat/iptal: nokta yazilir, durum 'paused'. Tek akisli
                # aile (ftp/hls/dash) icin ORTAK sozlesme: eskiden yalniz ftp
                # katlaniyordu, hls/dash 'downloading' kaliyordu ve resume()
                # no-op oluyordu.
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
                if self._finish():
                    continue        # onarim araliklari kuyruga girdi
                if self._stop.is_set():
                    self._fold_threads()   # dogrulama sirasinda durduruldu
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
                             meta_path=self.meta_path,
                             store=self._profile_store(),
                             cap=self._seg_cap())
            # duraklatma/iptal sonrasi 'tail' yarim dosyanin sonudur: toplami
            # ona esitlemek isi 'eksik veri' ya da erken 'done' yapardi
            if not self._stop.is_set():
                self.total = self.done = int(meta.get("tail", 0))
                util.atomic_write_json(self.meta_path, meta)
        except Exception as ex:
            self._fail(str(ex), _code_for(ex))

    def _seg_cap(self) -> int:
        """Segment havuzu tavani: sabit 8 ile isin baglanti tavani
        (birlikte yasam / sunucu) kucuk olani. Tohum ayni siniri asmaz."""
        with self._lock:
            ceiling = self._ceiling_locked()
        return max(1, min(segments.SEG_MAX, ceiling))

    def _run_dash(self):
        opt = self.spec.options(self.cfg)
        meta = util.read_json(self.meta_path) or {}
        meta.setdefault("url", self.url)
        meta["kind"] = "dash"
        try:
            def prog(n):
                with self._lock:
                    self.done += n

            t0 = time.monotonic()
            text = dash.fetch_mpd(self.url, opt)
            rtt = time.monotonic() - t0    # MPD yanit suresi (RTT bandi)
            init_url, seg_urls = dash.build_segment_list(text, self.url)
            self.extra["segments_listed"] = len(seg_urls)
            hls.segments.assemble(opt, self._fd, meta, self._stop,
                                  self._bucket, prog, self.meta_path,
                                  init_url, seg_urls,
                                  store=self._profile_store(),
                                  cap=self._seg_cap(), rtt=rtt)
            if not self._stop.is_set():
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
        # profil olcumu: tek akis oldugu icin yalniz RTT bandi + hiz ogrenilir
        t0 = time.monotonic()
        first = [0.0]             # ilk baytin geldigi an (TTFB)
        moved = [0]               # bu kosuda yazilan bayt (denemeler dahil)

        def run(offset: int):
            def prog(n):
                with self._lock:
                    self.done += n
                    self._wrote += n        # FTP yazimlari da sayilir
                if n and not first[0]:
                    first[0] = time.monotonic()
                moved[0] += n
                self._maybe_checkpoint()

            return ftp.download(
                self.url, self._fd, offset, stop=self._stop,
                bucket=self._bucket, progress=prog, stalled=self._stall,
                insecure=opt.insecure, timeout=max(1, int(opt.timeout)),
                halted=lambda: gen != self._gen,
                user=self.spec.user, password=self.spec.password,
                proxy=opt.proxy)

        try:
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
                            self._reset_verify_extra()
                            mark = 0
                            verify = False
                            run(0)
                    if gen != self._gen:
                        return
                    if self._stop.is_set():
                        return      # monitor noktayi yazar ve durumu paused yapar
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
                        attempt = 0  # onceki deneme ilerledi: butce tazelenir
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
        finally:
            # duraklatma/bitis/hata: kosunun kendi olcumu profile yazilir
            self._report_ftp_profile(t0, first[0], moved[0],
                                     time.monotonic() - t0)

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
        self._report_profile()          # duraklatmadan once ogrenilenler yazilir
        self._changed()

    def _do_restart(self):
        self._fold_threads()
        self._restarts += 1
        self._lat.reset()      # kaynak degisti: eski TTFB tabani anlamsiz
        self._cong.reset()     # eski TCP_INFO ornekleri de gecersiz
        with self._lock:
            self._restart_needed = False
            self._threads = []
            if self.total > 0:
                self._pending = [(0, self.total - 1)]
                self.done = 0
                if self._fd is not None:
                    os.ftruncate(self._fd, self.total)
        self._reset_verify_extra()     # bastan: eski dogrulama izleri kalmasin
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

    def _stopped(self) -> bool:
        """Duraklat/iptal sinyali geldi mi (dogrulama aralarinda sorulur)."""
        return self._stop.is_set() or self.state == JobState.CANCELED

    def _finish(self) -> bool:
        """Bitirir. True: merkle onarim araliklari kuyruga girdi (monitor
        dongusu devam eder, is DONE sayilmaz)."""
        with self._lock:
            if self.total >= 0 and self.done < self.total:
                self._fail("eksik veri", "size_mismatch")
                return False
        if self._merkle_repair():
            return True
        if self._stopped():
            return False        # duraklat/iptal: DONE yazilmaz
        self._finalize_file()
        return False

    def _merkle_repair(self) -> bool:
        """Merkle bildirimi varsa YALNIZCA bozuk parcalari yeniden indirir.

        Araliklar mevcut kuyruk mekanizmasiyla iner: If-Range dogrulayicisi
        korunur, kaynak degistiyse normal restart yolu devreye girer. Onarim
        turu MERKLE_REPAIR_MAX ile sinirlidir; asilirsa is bitis
        dogrulamasinda merkle_mismatch ile hata olur (dosya silinmez).

        Yalnizca ACIKCA verilen bildirim onarilir: kendiliginden bulunan yan
        dosya bayat olabilir, bosa onarim turu harcamayiz (uyari yeter).
        Duraklat/iptal dogrulama sirasinda da gecebilir: her turda ve her
        okuma blogunda `_stopped` sorulur, durunca DONE/onarim yazilmaz."""
        man = self._merkle
        if man is None or self._merkle_auto or self.kind != "http" \
                or self.extra.get("no_ranges") or self._stopped():
            return False
        t = getattr(self, "_sidecar_t", None)
        if t is not None and t.is_alive():
            # merkle yan dosyasi henuz araniyor olabilir: bitis dogrulamasi
            # nasil bekliyorsa onarim karari da ayni pencereyi bekler
            t.join(timeout=SIDECAR_WAIT)
        if self._stopped() or self._fd is None or not self.part_path.exists():
            return False
        res = cdc.verify_file(self.part_path, man, halt=self._stopped)
        if res is None or self._stopped():
            return False              # duraklat/iptal: karar yazilmaz
        size_ok, bad = res
        if not size_ok:
            return False              # boyut: aralikla onarilamaz
        if not bad:
            # ayni dosya bitis dogrulamasinda YENIDEN okunmaz (_stat_key)
            self._merkle_res, self._merkle_key = res, _stat_key(self.part_path)
            if self._merkle_tries:
                self.extra["merkle"] = "repaired"
            else:
                self.extra["merkle"] = "ok"
            return False
        if self._merkle_tries >= MERKLE_REPAIR_MAX:
            return False              # bitis dogrulamasi hatayi yazar
        self._merkle_tries += 1
        need = sum(ln for _, ln in bad)
        self.extra["merkle"] = "repairing"
        self.extra["merkle_repaired_bytes"] = int(
            self.extra.get("merkle_repaired_bytes", 0)) + need
        with self._lock:
            self._pending = [(off, off + ln - 1) for off, ln in bad]
            self._inflight.clear()
            self.done = max(0, self.done - need)
            # ilerleme geri sarildi: hiz ornekleri negatife dusmesin, sonraki
            # olcum yalnizca yeni inen baytlari saysin
            self._speed_done_mark = self.done
            self._gov["done"] = self.done
        self.state = JobState.DOWNLOADING
        self._changed()
        self._start_workers()
        return True

    def _finalize_file(self):
        try:
            with self._lock:
                self._finalized.clear()   # bu deneme henuz yayinlanmadi
            if self._fd is not None:
                try:
                    # os.replace oncesi: dosya diske inmeli (.part -> son ad)
                    os.fsync(self._fd)
                finally:
                    self._close_fd()
            self.target_dir.mkdir(parents=True, exist_ok=True)
            # Son ad ATOMIK sahiplenilir (O_CREAT|O_EXCL yer tutucu): ayni
            # adli iki is birbirinin dosyasini ezemez; adi alan is bitirir.
            dest = util.unique_path(self.target_dir,
                                    self.final_filename or "download.bin")
            with self._lock:
                self._reserved = dest
            self._record_reservation(dest)   # cokme: bos yer tutucu kurtarilir
            try:
                os.replace(self.part_path, dest)   # yer tutucunun uzerine
            except OSError as ex:
                if ex.errno != errno.EXDEV:
                    raise
                # baska dosya sistemi: kopyala + fsync + sil (ad zaten bizim)
                import shutil
                shutil.move(str(self.part_path), str(dest))
                fd = os.open(dest, os.O_RDONLY)
                try:
                    os.fsync(fd)               # kopya diske inmeden bitmez
                finally:
                    os.close(fd)
            with self._lock:
                self._reserved = None          # ad artik dosyanin kendisi
            # ad degisikligi diske insin; sonra nokta dosyasi kaldirilir
            fsync_dir(self.target_dir)
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
            self._verify(dest, found)          # ozetler + merkle: TEK okuma
            if self.state != JobState.ERROR and not self._stopped():
                self._verify_merkle(dest)
                if self.extra.get("merkle") in ("ok", "repaired"):
                    # dogrulanmis parcalar esbaz onbellegine yazilir
                    self._peer_index_add(dest)
            self.extra.pop("verifying", None)
            if self.state != JobState.ERROR and not self._stopped():
                if _looks_like_soft_404(self.final_filename, self._content_type,
                                        dest, self.kind):
                    # yumusak 404: dosya yerinde kalir, is DONE; yalnizca uyari
                    self.extra["warning"] = "soft_404"
            if self.state != JobState.ERROR and not self._stopped():
                # duraklat/iptal dogrulama sirasinda geldiyse DONE yazilmaz
                self.state = JobState.DONE
        except Exception as ex:
            self.extra.pop("verifying", None)
            self._drop_reservation()      # doldurulmamis ad yer tutucusu
            self._fail(str(ex), _code_for(ex))
        if self.state == JobState.DONE:
            self._report_profile()      # biten isin olcumu ana makine profiline
        self._changed()

    def _reset_verify_extra(self) -> None:
        """Bastan baslayan kosu eski dogrulama/uyari izlerini tasimaz.

        `checksum`/`merkle`/`verified` gibi alanlar ONCEKI denemeye aittir;
        sifirdan indirilen dosya icin gecersizdir (bayat uyari rozeti)."""
        with self._lock:
            for k in ("checksum", "checksum_note", "merkle", "merkle_note",
                      "verified", "merkle_repaired_bytes", "peer_bytes",
                      "peer_chunks", "verifying", "verify_done",
                      "verify_total", "warning"):
                self.extra.pop(k, None)
            self._merkle_res = None
            self._merkle_key = ()

    def _verify_progress(self, done: int) -> None:
        """Dogrulama ilerlemesi (bayt) - arayuz bu sirada % gosterebilir."""
        with self._lock:
            self.extra["verify_done"] = int(done)

    def _verify(self, dest: Path, found: dict | None = None) -> None:
        """Saglama. Acikca istenen ozetler uyusmazsa is HATA (veri silinmez).
        Otomatik bulunan yan dosya (sidecar) uyusmazsa dosya yerinde
        birakilir ve UYARI ile isaretlenir - sunucudaki bayat yan dosya
        yanlis alarm uretebilir, dosya kendi kendine supheli sayilmaz.
        'found': bu ana kadar bulunan yan dosya ozetleri (None = hepsi).

        Butun ozetler ve (varsa) merkle parcalari TEK okumada hesaplanir;
        merkle sonucu `_verify_merkle` icin saklanir. Duraklat/iptal her
        okuma blogunda gorulur: karar yazilmaz."""
        explicit = self.spec.wanted()
        found = {k: v for k, v in (self._sidecar if found is None
                                   else found).items()
                 if k not in explicit}
        man = self._merkle
        algos = {**found, **explicit}
        if not algos and man is None:
            return
        if not algos and self._merkle_res is not None \
                and _stat_key(dest) == self._merkle_key:
            return                     # ayni dosya zaten dogrulandi: okuma yok
        self._merkle_res, self._merkle_key = None, ()
        try:
            total = dest.stat().st_size
        except OSError:
            total = 0
        with self._lock:
            self.extra["verify_total"] = total
            self.extra["verify_done"] = 0
        got, mres = _verify_pass(dest, algos, man, halt=self._stopped,
                                 progress=self._verify_progress)
        with self._lock:
            self.extra.pop("verify_done", None)
            self.extra.pop("verify_total", None)
        if self._stopped():
            return                     # kesildi: karar yazilmaz
        if mres is not None:
            self._merkle_res = mres
            self._merkle_key = _stat_key(dest)
        if not explicit and not found:
            return                     # yalniz merkle dogrulandi
        bad_expl = [a for a, v in explicit.items() if got.get(a) != v]
        bad_found = [a for a, v in found.items() if got.get(a) != v]
        self.extra["verified"] = [a for a in got
                                  if a not in bad_expl and a not in bad_found]
        if bad_expl:
            a = bad_expl[0]
            # durum en son: ERROR goren her okuyucu hata metnini de gorur
            self.error = ("saglama uyusmazi: beklenen "
                          f"{explicit[a][:12]}..., gelen "
                          f"{(got.get(a) or '')[:12]}... ({a})")
            self.error_code = "checksum_mismatch"
            self.extra["checksum"] = "mismatch"
            self.state = JobState.ERROR
        elif bad_found:
            self.extra["checksum"] = "warn"
            self.extra["checksum_note"] = bad_found[0]
        else:
            self.extra["checksum"] = "ok"

    def _verify_merkle(self, dest: Path) -> bool:
        """Parca bazli (Merkle) dogrulama. Acikca verilen bildirim uyusmazsa
        is HATA olur (dosya silinmez; `ferdim merkle update` ile yalnizca o
        parcalar onarilabilir). Kendiliginden bulunan yan dosya bayat
        olabilecegi icin yalnizca UYARI isaretidir: dosya yerinde kalir,
        is bitmis sayilir (yan dosya saglamasindaki kuralin aynisi).

        `_verify` ayni okumada merkle sonucunu zaten urettiyse (ya da onarim
        turu ayni dosyayi dogruladiysa) yeniden okunmaz.

        True: dogrulama tamamlandi. False: duraklat/iptal nedeniyle kesildi."""
        man = self._merkle
        if man is None:
            return True
        res = self._merkle_res
        if res is None or _stat_key(dest) != self._merkle_key:
            res = cdc.verify_file(dest, man, halt=self._stopped)
            self._merkle_res, self._merkle_key = res, _stat_key(dest)
        if res is None:
            return False
        size_ok, bad = res
        if size_ok and not bad:
            self.extra.setdefault("merkle", "ok")
            self.extra["verified"] = sorted(
                set(self.extra.get("verified", [])) | {"merkle"})
            return True
        if self._merkle_auto:
            self.extra["merkle"] = "warn"
            self.extra["merkle_note"] = (
                "size" if not size_ok else f"{bad[0][0]}-{bad[0][1]}")
            return True
        self.extra["merkle"] = "mismatch"
        self.error_code = "merkle_mismatch"
        if not size_ok:
            try:
                got = dest.stat().st_size
            except OSError:
                got = -1
            self.error = (f"merkle boyut uyusmazi: beklenen {man.size}, "
                          f"gelen {got}")
        else:
            off, ln = bad[0]
            self.error = (f"merkle uyusmazi: {len(bad)} bozuk parca, "
                          f"ilk {off}-{off + ln - 1}")
        self.state = JobState.ERROR        # metin ve kod hazir, durum en son
        return True

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
        # once devam noktasi: ERROR goren (arayuz, testler) noktayi diskte
        # bulmali; asagidaki ikinci yazim son durumu tazeler
        self._checkpoint(force=True)
        with self._lock:
            if self.state == JobState.CANCELED:
                return
            self.error = msg
            self.error_code = code
            self.state = JobState.ERROR
            self.speed = 0
        self._stop.set()  # diger iscalar kalan araliklari katlasin
        with self._cond:
            self._cond.notify_all()
        self._checkpoint(force=True)
        self._changed()

    # -------------------------------------------------------------- control
    def _await_run(self, deadline: float) -> None:
        """start() bitene ve monitor zinciri kapanana kadar bekler. Zincir
        onemli: _do_restart yeni bir monitor yayinlayabilir, o zaman eski
        halkayi degil sonuncuyu join ederiz."""
        left = deadline - time.monotonic()
        if left > 0 and not self._start_done.is_set():
            self._start_done.wait(left)
        cur = threading.current_thread()
        while True:
            m = self._monitor
            if m is None or m is cur:
                return
            left = deadline - time.monotonic()
            if left <= 0:
                return
            m.join(left)
            if self._monitor is m:
                return

    def _run_ended(self) -> bool:
        """Kosu gercekten bitti mi: monitor olu, canli isci yok, kosucu yok.
        HLS/DASH/FTP akislarinda isci listesi bostur; kosucu ayri izlenir."""
        m = self._monitor
        if m is not None and m.is_alive():
            return False
        r = self._runner
        if r is not None and r.is_alive():
            return False
        return not any(t.is_alive() for t in list(self._threads))

    def pause(self):
        """Durdurur ve (en cok ~8 sn) havuzun kapanmasini bekler: komut
        dondugunde kalan araliklar kuyruktadir, denetim noktasi
        yazilmistir ve durum 'paused' dir.

        Baslatma penceresi de kapsanir: start() hazirlik/probe asamasindayken
        gelen duraklatma, isi 'downloading' durumda birakmaz (eskiden monitor
        henuz yayinlanmadigi icin olu/None monitor join edilip donuluyordu).
        """
        with self._cond:
            # dur sinyali AYNI kilit altinda kurulur: start() _stop'u kilit
            # altinda temizledigi icin araya giren bir pause kaybolmaz
            self._stop.set()
            self._cond.notify_all()       # park etmis iscalar uyansin
            queued = self.state == JobState.QUEUED
            if queued:
                # zamanlayicinin baslatmasini bekle: start(queued_only=True)
                self.state = JobState.PAUSED
        if queued:
            self._changed()
            return
        self._await_run(time.monotonic() + 8.0)
        # Kosu gercekten bittiyse durum 'paused' olmali. HLS/DASH/FTP'de
        # monitor durunca durum 'downloading' kaliyordu (isci listesi bos,
        # kosucu ayri izlenir) ve resume() no-op oluyordu: duraklatma
        # etkisiz gorunuyordu.
        if self.state == JobState.DOWNLOADING and self._run_ended():
            self._checkpoint(force=True)
            with self._lock:
                if self.state == JobState.DOWNLOADING:
                    self.state = JobState.PAUSED
        self._report_profile()          # anlamli aktarim varsa profile yazilir
        self._changed()

    def resume(self):
        if self.state in (JobState.PAUSED, JobState.ERROR, JobState.QUEUED):
            self.start()

    def cancel(self, delete_files: bool = False):
        """Iptal: is bir daha devam etmez.

        Gecici dosyalar (`.part`, HLS/DASH `.segs/`, sahiplenilmis son ad
        yer tutucusu) HER DURUMDA silinir - iptal edilmis isin yarim dosyasi
        yetim kalirdi. `delete_files` eski imza icin korunur (bitmis dosyayi
        kuyruk `remove` tarafinda siler); yarim dosya zaten silinmistir."""
        with self._cond:
            self._stop.set()
            self._run += 1          # eski hazirlik sonucu artik uygulanmaz
            self._cond.notify_all()
        # fd ancak start() penceresi ve monitor kapandiktan sonra kapatilir:
        # yoksa baslatma ortasindaki isciler kapali fd'ye yazardi.
        self._await_run(time.monotonic() + 5.0)
        self._join_threads(2.0)
        self._close_fd()
        with self._lock:
            self.state = JobState.CANCELED
        self.meta_path.unlink(missing_ok=True)
        self._drop_reservation()
        self._cleanup_temp()
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
        # hls/dash meta dosyasi segment ilerlemesini tasir (segments.py
        # yazar); aralik bicimli bir nokta onu ezerdi
        if self.kind in ("hls", "dash") or self._fd is None:
            return
        with self._lock:
            if self._fd is None:
                return
            run = self._run
            # Sayac ilerlediyse yeni baytlar yazildi: nokta onlari 'bitti'
            # sayabilir, bu yuzden once fsync gerekir. Ilerleme yoksa (ayni
            # noktanin tekrari) fsync bosuna surmez.
            need = self._wrote != self._durable_wrote
            wrote = self._wrote
            meta = self._checkpoint_meta_locked()
            # kopya fd: izleyici asil fd'yi bu arada kapatabilir (HATA yolu);
            # kopya ayni dosyayi gosterir, numara geri donusumu olmaz
            fd = os.dup(self._fd) if need else None
        if need:
            try:
                # KILIT DISINDA: fsync yavas diskte saniyeler surer; kilit
                # tutulurken isciler pwrite icin bekler ve pause() 8 sn
                # butcesi asilirdi.
                os.fsync(fd)
            except OSError:
                # yazilamiyor: eski (daha geri) nokta kalir - guvenli yon
                return
            finally:
                os.close(fd)
            with self._lock:
                if self._run != run:
                    return      # yeni kosu: eski karar yazilmaz
                # nokta fsync ONCESI goruntuden yazilir: fsync sirasinda
                # yazilan bayt bir sonraki turda fsync edilir (sayac ilerler)
                self._durable_wrote = wrote
        else:
            with self._lock:
                if self._run != run:
                    return      # yeni kosu: eski karar yazilmaz
        util.atomic_write_json(self.meta_path, meta)

    def _checkpoint_meta_locked(self) -> dict:
        """Denetim noktasinin diske yazilacak goruntusu (kilit altinda)."""
        return {
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
        }

    def update_speed(self):
        """QueueManager ticker'i cagirir; hiz ve baglanti ornekleri."""
        now = time.monotonic()
        with self._lock:
            dt = now - self._speed_mark_ts
            if dt >= 0.5:
                self.speed = (self.done - self._speed_done_mark) / dt
                self._speed_done_mark = self.done
                self._speed_mark_ts = now
                if self.state in (JobState.CONNECTING, JobState.DOWNLOADING):
                    n = self._active
                    self._links_sum += n
                    self._links_samples += 1
                    self._links_peak = max(self._links_peak, n)


def _ensure_ext(name: str) -> str:
    return name if name.lower().endswith((".ts", ".mp4", ".mkv")) else name + ".ts"
