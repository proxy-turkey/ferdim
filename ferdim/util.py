"""Dosya adi, kategori ve bicimlendirme yardimcilari."""
from __future__ import annotations

import errno
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

# disk kaynakli OSError'lar: API'de "disk" kodu, yeniden denemek anlamsiz
DISK_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EACCES,
                         errno.EROFS, errno.ENAMETOOLONG})

CATEGORIES = {
    "video": ".mp4 .mkv .webm .flv .avi .mov .wmv .mpg .mpeg .ts .m4v .3gp",
    "music": ".mp3 .m4a .flac .wav .ogg .opus .wma .aac",
    "compressed": ".zip .rar .7z .tar .gz .bz2 .xz .zst .iso .cab",
    "program": ".exe .msi .apk .deb .rpm .appimage .dmg .bin .run",
    "document": ".pdf .doc .docx .xls .xlsx .ppt .pptx .odt .ods .txt .epub",
}
_CAT_INDEX = [
    (cat, exts.split()) for cat, exts in CATEGORIES.items()
]


def category_for(name_or_path: str) -> str:
    ext = Path(name_or_path).suffix.lower()
    for cat, exts in _CAT_INDEX:
        if ext in exts:
            return cat
    return "other"


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKC", name).strip()
    name = name.replace("\x00", "")
    # Bolum ayracilari ve dosya sistemlerine aykiri karakterleri temizle
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:200] or "download.bin"


def filename_from_url(url: str) -> str:
    path = unquote(urlsplit(url).path)
    base = os.path.basename(path)
    return sanitize_filename(base) if base else "download.bin"


def parse_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", value)
    if m:
        return sanitize_filename(unquote(m.group(1).strip('" ')))
    m = re.search(r'filename="?([^";]+)"?', value)
    if m:
        return sanitize_filename(unquote(m.group(1).strip()))
    return None


def unique_path(directory: Path, name: str) -> Path:
    """Hedef dizinde benzersiz bir ad SAHIPLENIR (bos yer tutucu acilir).

    Kontrol-sonra-kullan yarisi yoktur: ad O_CREAT|O_EXCL ile acilir, iki is
    ayni adi sectiginde yalniz biri kazanir, digeri '(1)' ile devam eder -
    bitiste biri digerinin dosyasinin uzerine yazamaz. Var olan ad (dizin ya
    da sembolik bag dahil) EEXIST verir. Dondurulen yol cagirana aittir:
    cagiran onu kendi icerigiyle doldurur (os.replace) ya da vazgecince
    siler. Hedef dizin onceden var olmalidir."""
    stem, dot, ext = name.partition(".")
    if not dot:
        stem, ext = name, ""
    i = 0
    while True:
        cand = directory / name if i == 0 else \
            directory / f"{stem} ({i}){('.' + ext) if ext else ''}"
        try:
            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            i += 1
            continue
        os.close(fd)
        return cand


def atomic_write_json(path: Path, obj) -> None:
    """Atomik JSON yazimi. Gecici dosya 0600 ile acilir: is/kuyruk
    dosyalari kimlik bilgisi tasiyan URL'ler icerebilir, dosya izinleri
    umask'tan bagimsiz olarak kullaniciya ozel kalsin."""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(tmp, flags, 0o600)
    except FileExistsError:
        # onceki cokme/eski surumden kalan gecici dosya: temizle ve dene
        try:
            os.unlink(tmp)
        except OSError:
            pass
        fd = os.open(tmp, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
    finally:
        if fd >= 0:
            os.close(fd)
    tmp.replace(path)


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except Exception:
        return None


def human_size(n) -> str:
    if n is None or n < 0:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B",) else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_eta(seconds) -> str:
    if seconds is None or seconds < 0:
        return "--"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}sn"
    if seconds < 3600:
        return f"{seconds // 60}dk {seconds % 60}sn"
    return f"{seconds // 3600}sa {((seconds % 3600) // 60):02d}dk"


_KIB, _MIB, _GIB = 1024, 1024 ** 2, 1024 ** 3
_SIZE_MULT = {"b": 1, "kb": _KIB, "k": _KIB, "kib": _KIB,
              "mb": _MIB, "m": _MIB, "mib": _MIB,
              "gb": _GIB, "g": _GIB, "gib": _GIB}


def parse_size(text: str | None):
    """'2MB', '512kb', '1048576' -> bayt. None/0 donen deger sinirsizdir.
    KB/MB/GB 1024 tabanlidir: girilen deger ekranda (human_size) ayni
    sayiyla geri gorunur ('2MB' -> '2.0 MB'), 'MB' ile 'MiB' ayniidir."""
    if not text:
        return 0
    s = str(text).strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(kib|mib|gib|kb|mb|gb|k|m|g|b)?", s)
    if not m:
        try:
            return int(float(s))
        except ValueError:
            return 0
    return int(float(m.group(1)) * _SIZE_MULT.get(m.group(2) or "b", 1))


def now_ms() -> int:
    return int(time.time() * 1000)


# desteklenen saglama algoritmalari ve ozet uzunluklari (beyaz liste:
# disaridan gelen algoritma adi asla dogrudan hashlib'e verilmez)
DIGEST_LEN = {"sha256": 64, "sha512": 128, "md5": 32, "sha1": 40}


def normalize_checksums(d) -> dict:
    """{algo: ozet} sozlugunu dogrular: bilinen algoritma + dogru uzunlukta
    onaltilik; uygun olmayanlar atilir, ozet kucuk harfe cevirilir."""
    out = {}
    if not isinstance(d, dict):
        return out
    for algo, val in d.items():
        n = DIGEST_LEN.get(str(algo).lower().replace("-", ""))
        v = str(val or "").strip().lower()
        if n and len(v) == n and all(c in "0123456789abcdef" for c in v):
            out[str(algo).lower().replace("-", "")] = v
    return out


def file_digest(path, algo: str = "sha256") -> str:
    return file_digests(path, [algo]).get(algo, "")


def file_digests(path, algos, halt=None, progress=None, on_block=None) -> dict:
    """Dosyayi TEK okumayla birden fazla algoritmayla ozetler. `algos`
    algoritma adlarinin listesi ya da {algo: ozet} sozlugu olabilir; yalniz
    DIGEST_LEN'deki algoritmalar kabul edilir.

    halt   : her okuma blogunda sorulur; True donerse okuma kesilir ve {}
          doner (cagiran karari yazmaz - duraklat/iptal bekletilmez).
    progress: okunan toplam bayt sayisiyla cagrilir (arayuz ilerlemesi).
    on_block(offset, block): akis uzerinde ek denetim; verilirse algoritma
          istenmese de dosya okunur (merkle parcalari ayni okumada dogrulanir).
    """
    import hashlib
    hs = {}
    for a in (algos or {}):
        name = str(a).lower().replace("-", "")
        if name in DIGEST_LEN and name not in hs:
            hs[name] = hashlib.new(name)
    if not hs and on_block is None:   # gecerli algoritma yok: dosyayi acma
        return {}
    read = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            if halt is not None and halt():
                return {}
            for h in hs.values():
                h.update(chunk)
            if on_block is not None:
                on_block(read, chunk)
            read += len(chunk)
            if progress is not None:
                progress(read)
    return {a: h.hexdigest() for a, h in hs.items()}


def parse_checksum_text(text: str, algo: str, filename: str | None = None) -> str:
    """Saglama yan dosyasindan (.sha256 / SHA256SUMS / BSD 'SHA256 (ad) = ozet')
    beklenen ozeti cikarir. Birden fazla kayit varsa dosya adi eslesen aranir;
    bulunamazsa bos doner (yanlis ozet, yanlis uyari demek)."""
    n = DIGEST_LEN.get(algo)
    if not n or not text:
        return ""
    want = (filename or "").lower()
    found: dict[str, str] = {}       # dosya adi ("" = adsiz) -> ozet
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "//", "-----")):
            continue
        m = re.search(rf"(?<![0-9A-Fa-f])[0-9A-Fa-f]{{{n}}}(?![0-9A-Fa-f])", line)
        if not m:
            continue
        digest = m.group(0).lower()
        bsd = re.match(rf"\w+\s*\(\s*([^)]*?)\s*\)\s*=", line)
        if bsd:
            name = bsd.group(1)
        else:
            name = line.replace(m.group(0), " ").strip().lstrip("*").strip()
        found[name.lower()] = digest
    if not found:
        return ""
    if want:
        base = Path(want).name
        for name, digest in found.items():
            if name and Path(name).name == base:
                return digest
    if len(found) == 1:
        return next(iter(found.values()))
    return ""


def parse_checksum_spec(text) -> tuple[str, str]:
    """'sha256:<ozet>' ya da uzunlugundan anlasilan '<ozet>' girisini
    (algoritma, ozet) ciftine cevirir; gecersizse ('', '')."""
    s = str(text or "").strip()
    if not s:
        return "", ""
    if ":" in s:
        algo, _, digest = s.partition(":")
    else:
        digest = s
        algo = next((a for a, n in DIGEST_LEN.items() if len(digest) == n), "")
    got = normalize_checksums({algo.strip(): digest.strip()})
    if not got:
        return "", ""
    return next(iter(got)), next(iter(got.values()))


def url_sibling(url: str, suffix: str) -> str:
    """URL'nin yoluna ek ekler; sorgu ve parca korunur
    ('/a/f.iso?x=1' + '.sha256' -> '/a/f.iso.sha256?x=1')."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path + suffix))


def redact_url(url: str) -> str:
    """Sifre iceren URL'leri arayuzlere sizdirmadan gosterir:
    'ftp://kullanici:gizli@host/a' -> 'ftp://kullanici@host/a'.
    Sifre yoksa URL aynen doner; ic kayit (spec.url) degismez."""
    try:
        parts = urlsplit(url)
        password = parts.password
    except ValueError:
        return re.sub(r"(://[^/@:]+):[^/@]*@", r"\1@", url)
    if password is None:
        return url
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"                 # IPv6
    netloc = f"{parts.username}@{host}" if parts.username else host
    try:
        port = parts.port
    except ValueError:
        # bozuk port: sifreyi yine de gizle, portu at
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query,
                           parts.fragment))
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query,
                       parts.fragment))


def _hm(s) -> int:
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


_HM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# hiz plani limit metni: '2MB', '512kb', '1.5g', '0' (sinirsiz)
_SIZE_TEXT_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:kib|mib|gib|kb|mb|gb|k|m|g|b)?$", re.IGNORECASE)


def window_open(start, stop, now=None, days=None) -> bool:
    """'02:00'-'08:00' penceresi su an acik mi? Gece yarisini asan pencere
    desteklenir; bozuk giris pencere yok sayilir (her zaman acik).

    days verilirse (0 = pazartesi) pencere yalniz o gunlerde aciktir. Gece
    yarisini asan pencerede gun, pencerenin BASLADIGI gundur: 'cuma
    22:00-02:00' cumartesi 01:00'de de aciktir."""
    import datetime as _dt
    now = now or _dt.datetime.now()
    if days is not None:
        days = frozenset(days)
        if not days:
            return False
    try:
        s, e = _hm(start), _hm(stop)
    except Exception:
        return days is None or now.weekday() in days
    cur = now.hour * 60 + now.minute
    if s <= e:
        return (days is None or now.weekday() in days) and s <= cur < e
    if cur >= s:
        return days is None or now.weekday() in days
    if cur < e:
        return days is None or ((now.weekday() - 1) % 7) in days
    return False


def parse_days(spec) -> frozenset[int]:
    """'mon-fri', 'mon,wed', 'sat-sun' -> gun kumesi (0 = pazartesi).
    Bos ya da '*' her gundur. Bilinmeyen ad ValueError firlatir: cagiran
    kurali atar (sessizce 'her gun' saymak plani bozardi)."""
    s = str(spec or "").strip().lower()
    if not s or s in ("*", "all", "daily", "everyday"):
        return frozenset(range(7))
    out: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        first, _, last = part.partition("-")
        if first not in _DAY_NAMES:
            raise ValueError(f"gun adi: {first}")
        i = _DAY_NAMES.index(first)
        if not last:
            out.add(i)
            continue
        if last not in _DAY_NAMES:
            raise ValueError(f"gun adi: {last}")
        j = _DAY_NAMES.index(last)
        while True:                    # 'fri-mon' gibi sarmayi da kabul eder
            out.add(i)
            if i == j:
                break
            i = (i + 1) % 7
    return frozenset(out)


def speed_plan_rule(plan, now=None):
    """Ilk eslesen speed_plan kurali (ilk eslesen kazanir); yoksa None."""
    import datetime as _dt
    now = now or _dt.datetime.now()
    for rule in plan or ():
        if not isinstance(rule, dict):
            continue
        try:
            days = parse_days(rule.get("days"))
        except ValueError:
            continue
        if window_open(rule.get("from", ""), rule.get("to", ""), now, days):
            return rule
    return None


def speed_plan_limit(plan, now=None):
    """Etkin plan limiti (bayt/sn; 0 = sinirsiz). Plan bos/gecersizse None:
    plan yok, mevcut limit oldugu gibi kalir. Kural eslesmezse 0: plan
    gunun tamamini kapsamak zorunda degil, disinda azami guc gecerlidir."""
    if not isinstance(plan, list) or not any(isinstance(r, dict) for r in plan):
        return None
    rule = speed_plan_rule(plan, now)
    return parse_size(rule.get("limit")) if rule else 0


def sanitize_speed_plan(raw) -> list:
    """Bozuk kurallari atar, alanlari normalestirir: gun kumesi adi, HH:MM
    pencere (ikisi de bos = tum gun) ve hiz metni ('2MB'; '0'/'' = sinirsiz)."""
    out = []
    if not isinstance(raw, list):
        return out
    for rule in raw:
        if not isinstance(rule, dict):
            continue
        try:
            parse_days(rule.get("days"))
        except ValueError:
            continue
        start = str(rule.get("from") or "").strip()
        stop = str(rule.get("to") or "").strip()
        if (start or stop) and not (_HM_RE.match(start) and _HM_RE.match(stop)):
            continue
        limit = rule.get("limit")
        text = "" if limit is None else str(limit).strip()
        if text not in ("", "0") and not _SIZE_TEXT_RE.match(text.replace(" ", "")):
            continue
        out.append({"days": str(rule.get("days") or "").strip().lower(),
                    "from": start, "to": stop, "limit": text or "0"})
    return out


def zip_preview(path, limit: int = 500):
    """Uyumluluk sarmali: [(isim, boyut, sikistirmis)] listesi. Arsiv
    mantigi ferdim/archive.py'de; eski istemciler icin imza korunur.
    Hata olursa (None, mesaj) dondurur."""
    try:
        from . import archive
        info = archive.describe(path, nested=False, limit=limit)
        entries = [(e["name"], e["size"], e["compressed"] or 0)
                   for e in info["entries"]]
        return entries, info["note"]
    except Exception as e:
        return None, str(e)
