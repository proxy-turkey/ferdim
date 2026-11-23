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
    p = directory / name
    if not p.exists():
        return p
    stem, dot, ext = name.partition(".")
    if not dot:
        stem, ext = name, ""
    i = 1
    while True:
        cand = directory / f"{stem} ({i}){('.' + ext) if ext else ''}"
        if not cand.exists():
            return cand
        i += 1


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


def file_digests(path, algos) -> dict:
    """Dosyayi TEK okumayla birden fazla algoritmayla ozetler. `algos`
    algoritma adlarinin listesi ya da {algo: ozet} sozlugu olabilir; yalniz
    DIGEST_LEN'deki algoritmalar kabul edilir."""
    import hashlib
    hs = {}
    for a in (algos or {}):
        name = str(a).lower().replace("-", "")
        if name in DIGEST_LEN and name not in hs:
            hs[name] = hashlib.new(name)
    if not hs:                 # gecerli algoritma yok: dosyayi acmaya bile gerek yok
        return {}
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            for h in hs.values():
                h.update(chunk)
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


def window_open(start, stop, now=None) -> bool:
    """'02:00'-'08:00' penceresi su an acik mi? Gece yarisini asan pencere
    desteklenir; bozuk giris pencere yok sayilir (her zaman acik)."""
    try:
        s, e = _hm(start), _hm(stop)
    except Exception:
        return True
    import datetime as _dt
    now = now or _dt.datetime.now()
    cur = now.hour * 60 + now.minute
    if s <= e:
        return s <= cur < e
    return cur >= s or cur < e


def zip_preview(path, limit: int = 500):
    """ZIP icindekiler listesi: [(isim, boyut, sikistirmis)]. Hata olursa
    (None, mesaj) dondurur."""
    try:
        import zipfile
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            entries = [(i.filename, i.file_size, i.compress_size)
                       for i in infos[:limit] if not i.is_dir()]
            more = f"+{len(infos) - limit}…" if len(infos) > limit else ""
            return entries, more
    except Exception as e:
        return None, str(e)
