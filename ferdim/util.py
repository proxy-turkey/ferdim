"""Dosya adi, kategori ve bicimlendirme yardimcilari."""
from __future__ import annotations

import email.message
import email.policy
import email.utils
import errno
import json
import os
import re
import tempfile
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


NAME_MAX_BYTES = 255          # tek dosya adi bileseninin bayt siniri

# Gorsel karisiklik/aldatma ve terminal kacisi: bidi kontrolleri ve
# kategori Cc/Cf/Cs (kontrol, bicim, vekil) adlarda hicbir zaman yer almaz.
_BIDI_CHARS = frozenset(
    list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))
    + [0x200E, 0x200F])
_UNSAFE_CATS = frozenset(("Cc", "Cf", "Cs"))


def _strip_unsafe(name: str) -> str:
    """Kontrol/bicim/vekil karakterleri ve bidi kontrollerini atar."""
    return "".join(
        ch for ch in name
        if ord(ch) not in _BIDI_CHARS and unicodedata.category(ch)
        not in _UNSAFE_CATS)


def _clip_utf8(text: str, budget: int) -> str:
    """UTF-8 bayt butcesine kirpar; cok baytli karakteri ASLA bolmez."""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    return raw[:max(0, budget)].decode("utf-8", "ignore")


def _truncate_filename(name: str, limit: int = NAME_MAX_BYTES) -> str:
    """255 BAYT siniri, uzanti korunur (tipik dosya sistemi kurali).

    Uzanti (son noktadan sonrasi) oldugu gibi tutulur; govde kalan bayt
    butcesine kirpilir. Uzantinin kendisi siniri asarsa uzanti ayrimi
    yapilmaz, tum ad kirpilir. Kirpma sonunda kalan bosluk/nokta atilir;
    sonuc bos kalirsa cagiran 'download.bin' kullanir."""
    if len(name.encode("utf-8")) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return _clip_utf8(name, limit).strip(" .")
    ext_part = "." + ext
    ext_len = len(ext_part.encode("utf-8"))
    if ext_len >= limit:
        return _clip_utf8(name, limit).strip(" .")
    stem = _clip_utf8(stem, limit - ext_len).strip(" .")
    return (stem + ext_part) if stem else _clip_utf8(name, limit).strip(" .")


def _suffixed_name(name: str, i: int,
                   limit: int = NAME_MAX_BYTES) -> str:
    """'(i)' eki eklendikten sonra da sinirda kalan ad (govde kirpilir).

    Uzanti korunur; ek ve uzanti icin yer yoksa ad butun olarak kirpilir."""
    stem, dot, ext = name.partition(".")
    if not dot:
        stem, ext = name, ""
    ext_part = ("." + ext) if ext else ""
    tag = f" ({i})"
    room = limit - len((tag + ext_part).encode("utf-8"))
    if room < 1:
        return _clip_utf8(stem + tag + ext_part, limit).strip(" .") \
            or "download.bin"
    stem = _clip_utf8(stem, room).strip(" .") or "f"
    return stem + tag + ext_part


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKC", name).strip()
    # Kontrol/bicim/vekil ve bidi karakterleri: kirpmadan ONCE atilir.
    name = _strip_unsafe(name.replace("\x00", ""))
    # Bolum ayracilari ve dosya sistemlerine aykiri karakterleri temizle
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    # Sinir KARAKTER degil BAYT: Turkce/emoji adlar 255 baytta kalir,
    # uzanti korunur, cok baytli karakter bolunmez.
    return _truncate_filename(name) or "download.bin"


def filename_from_url(url: str) -> str:
    path = unquote(urlsplit(url).path)
    base = os.path.basename(path)
    return sanitize_filename(base) if base else "download.bin"


def _cd_from_email(value: str) -> str | None:
    """RFC 6266 basligini email.message ile ayristirir (RFC 2231/5987).

    compat32 politikasi parametreleri HAM tutar; boylece `filename*`
    (genisletilmis, her charset) duz `filename`den AYRI gorulur ve ona
    tercih edilebilir. Genisletilmis deger collapse_rfc2231_value ile
    cozulur (bilinmeyen charset'te latin-1'e duser)."""
    try:
        msg = email.message.EmailMessage(policy=email.policy.compat32)
        msg["Content-Disposition"] = value
        plain = None
        for name, param in msg.get_params(header="content-disposition") or ():
            if name != "filename":
                continue
            if isinstance(param, tuple):
                return email.utils.collapse_rfc2231_value(param)
            if plain is None:
                plain = param
        return plain
    except Exception:
        return None


def _cd_from_regex(value: str) -> str | None:
    """Eski davranis: yalniz UTF-8 filename* ve duz filename."""
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", value)
    if m:
        return unquote(m.group(1).strip('" '))
    m = re.search(r'filename="?([^";]+)"?', value)
    if m:
        return unquote(m.group(1).strip())
    return None


def parse_content_disposition(value: str | None) -> str | None:
    """Content-Disposition'dan dosya adi (RFC 6266 / RFC 5987).

    Once email.message ile ayristirilir: `filename*` (her charset) duz
    `filename`ye tercih edilir. Ayristirma sonuc vermezse eski regex
    yedek olarak denenir."""
    if not value:
        return None
    raw = _cd_from_email(value)
    if raw is None:
        raw = _cd_from_regex(value)
    if raw is None:
        return None
    raw = raw.strip().strip('"').strip()
    return sanitize_filename(raw) if raw else None


def unique_path(directory: Path, name: str) -> Path:
    """Hedef dizinde benzersiz bir ad SAHIPLENIR (bos yer tutucu acilir).

    Kontrol-sonra-kullan yarisi yoktur: ad O_CREAT|O_EXCL ile acilir, iki is
    ayni adi sectiginde yalniz biri kazanir, digeri '(1)' ile devam eder -
    bitiste biri digerinin dosyasinin uzerine yazamaz. Var olan ad (dizin ya
    da sembolik bag dahil) EEXIST verir. Dondurulen yol cagirana aittir:
    cagiran onu kendi icerigiyle doldurur (os.replace) ya da vazgecince
    siler. Hedef dizin onceden var olmalidir."""
    i = 0
    while True:
        cand = directory / name if i == 0 else \
            directory / _suffixed_name(name, i)
        try:
            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            i += 1
            continue
        except OSError as e:
            if e.errno == errno.ENAMETOOLONG:
                # sanitize edilmemis cagri: adi sinirda kirp ve yeniden dene
                shorter = _truncate_filename(name)
                if shorter == name:
                    raise
                name = shorter
                continue
            raise
        os.close(fd)
        return cand


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600,
                       prefix: str | None = None) -> None:
    """Baytlari atomik + dayanikli yazar (tum durum dosyalarinin tek yolu).

    Gecici dosya mkstemp ile BENZERSIZ adla hedef dizinde acilir: sabit bir
    '.tmp' adi yoktur, bu yuzden ayni anda yazan iki surec birbirinin canli
    gecici dosyasini silip yarim/eski icerik yayinlayamaz. Icerik
    os.replace'ten ONCE fsync edilir, sonra dizin fsync edilir; herhangi bir
    hata gecici dosyayi siler. `prefix` yalniz gecici adin onekidir
    (varsayilan: hedef adi); `mode` son dosyanin iznidir.
    """
    from .paths import fsync_dir      # dongusel import: yalniz cagri aninda
    p = Path(path)
    fd = -1
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(p.parent),
                                   prefix=prefix if prefix is not None
                                   else p.name + ".",
                                   suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            fd = -1
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None
        fsync_dir(p.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def atomic_write_json(path: Path, obj) -> None:
    """Atomik + dayanikli JSON yazimi (0600: kimlik bilgisi tasiyabilir).

    Paylasilan `atomic_write_bytes` yolunu kullanir: benzersiz gecici ad,
    os.replace'ten once fsync, sonra dizin fsync. Guc kesintisi yarim ya da
    sifir dolu bir durum dosyasi birakmaz.
    """
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       mode=0o600)


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


_ZONE_CACHE: dict = {}


def _zone_from_key(key):
    """IANA anahtari ya da TZif dosya yolu -> ZoneInfo (yoksa None)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    key = str(key or "").strip()
    if key.startswith(":"):
        key = key[1:]
    if not key:
        return None
    if key in _ZONE_CACHE:
        return _ZONE_CACHE[key]
    try:
        if os.path.isfile(key):
            with open(key, "rb") as f:
                zone = ZoneInfo.from_file(f)
        else:
            zone = ZoneInfo(key)
    except (OSError, ValueError, KeyError, ZoneInfoNotFoundError):
        return None
    _ZONE_CACHE[key] = zone
    return zone


def local_zone():
    """Yerel saat dilimi: DST gecislerini modelleyen IANA bolgesi.

    Once TZ ortam degiskeni (IANA anahtari ya da TZif yolu), sonra
    /etc/localtime sembolik bag hedefi denenir; ikisi de cozulemezse
    sabit ofsetli yerel saat dilimine dusulur (gecis yok, eski davranis)."""
    import datetime as _dt
    raw_tz = (os.environ.get("TZ") or "").strip()
    if raw_tz:
        zone = _zone_from_key(raw_tz)
        if zone is not None:
            return zone
        # TZ gecerli bir IANA anahtari degil (POSIX kurali olabilir):
        # libc'nin cozdugu sabit ofsete dus, /etc/localtime'a DONME.
        return _dt.datetime.now().astimezone().tzinfo
    try:
        target = os.path.realpath("/etc/localtime")
        marker = os.sep + "zoneinfo" + os.sep
        if marker in target:
            zone = _zone_from_key(target.split(marker, 1)[1])
            if zone is not None:
                return zone
        if os.path.isfile("/etc/localtime"):
            zone = _zone_from_key("/etc/localtime")
            if zone is not None:
                return zone
    except OSError:
        pass
    return _dt.datetime.now().astimezone().tzinfo


def _now_local(zone=None):
    """Su an, yerel (tercihen DST'li) saat diliminde bilincli datetime."""
    import datetime as _dt
    return _dt.datetime.now(zone or local_zone())


def _resolve_wall(day, minutes: int, zone):
    """Duvarsaati -> bilincli (aware) an; DST POLITIKASI burada uygulanir.

    - Ileri sicrama (olmayan saat): pencerenin o siniri, bosluktan SONRAKI
      ilk gecerli ana kayar (Berlin'de 02:30 -> 03:00 CEST).
    - Geri alma (belirsiz saat): ILK olusum (fold=0) kullanilir; ikinci
      tekrar pencereyi yeniden ACMAZ.
    """
    import datetime as _dt
    naive = _dt.datetime.combine(
        day, _dt.time(minutes // 60, minutes % 60))
    cand = naive.replace(tzinfo=zone)
    if cand.astimezone(_dt.timezone.utc).astimezone(zone).replace(
            tzinfo=None) == naive:
        return cand                      # var; belirsizse fold=0
    for step in range(1, 241):           # bosluk en fazla birkac saat
        probe = naive + _dt.timedelta(minutes=step)
        c = probe.replace(tzinfo=zone)
        if c.astimezone(_dt.timezone.utc).astimezone(zone).replace(
                tzinfo=None) == probe:
            return c                     # bosluktan sonraki ilk gecerli an
    return cand


def _window_open_tz(start_min: int, stop_min: int, now, days, zone) -> bool:
    """Bilincli saat diliminde an karsilastirmali pencere testi.

    Karsilastirma UTC'ye cevrilerek yapilir: ayni tzinfo'lu iki aware
    datetime karsilastirmasinda Python `fold`u YOK SAYAR; geri alma
    saatinde bu pencereyi yanlislikla yeniden acardi."""
    import datetime as _dt
    utc = _dt.timezone.utc
    local = (now.astimezone(zone) if now.tzinfo
             else now.replace(tzinfo=zone)).astimezone(utc)
    today = local.astimezone(zone).date()
    one = _dt.timedelta(days=1)

    def at(day, minutes):
        return _resolve_wall(day, minutes, zone).astimezone(utc)

    if start_min <= stop_min:
        if days is not None and local.astimezone(zone).weekday() not in days:
            return False
        return at(today, start_min) <= local < at(today, stop_min)
    # Gece yarisini asan pencere: bugun baslayan ve dun baslayan adaylar.
    wd = local.astimezone(zone).weekday()
    if days is None or wd in days:
        if at(today, start_min) <= local < at(today + one, stop_min):
            return True
    if days is None or (wd - 1) % 7 in days:
        if at(today - one, start_min) <= local < at(today, stop_min):
            return True
    return False


def window_open(start, stop, now=None, days=None, tz=None) -> bool:
    """'02:00'-'08:00' penceresi su an acik mi? Gece yarisini asan pencere
    desteklenir; bozuk giris pencere yok sayilir (her zaman acik).

    days verilirse (0 = pazartesi) pencere yalniz o gunlerde aciktir. Gece
    yarisini asan pencerede gun, pencerenin BASLADIGI gundur: 'cuma
    22:00-02:00' cumartesi 01:00'de de aciktir.

    DST POLITIKASI:
    - `now` verilmezse (uretim yolu) yerel IANA bolgesi (`local_zone()`)
      cozulur ve bilincli an karsilastirmasi kullanilir; boylece gecisler
      gercekten modellenir.
    - ileri sicrama: o gun VAR OLMAYAN bir pencere siniri, bosluktan
      sonraki ilk gecerli ana kayar (02:30 -> 03:00). Iki sinir da
      bosluktaysa pencere o gun bostur.
    - geri alma: belirsiz duvarsaati ILK olusumuyla (fold=0) cozulur ve
      pencere ikinci tekrarda yeniden ACILMAZ.
    - acikca NAIVE `now` verilirse eski duvarsaati davranisi korunur
      (testler/geriye uyumluluk).
    """
    if now is None:
        zone = tz or local_zone()
        now = _now_local(zone)
    else:
        zone = tz or (now.tzinfo if getattr(now, "tzinfo", None) is not None
                      else None)
    if days is not None:
        days = frozenset(days)
        if not days:
            return False
    try:
        s, e = _hm(start), _hm(stop)
    except Exception:
        return days is None or now.weekday() in days
    if zone is not None:
        return _window_open_tz(s, e, now, days, zone)
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
    """Ilk eslesen speed_plan kurali (ilk eslesen kazanir); yoksa None.

    `now` verilmezse yerel DST'li an kullanilir (uretim yolu)."""
    now = now if now is not None else _now_local()
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
