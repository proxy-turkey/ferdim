"""Icerik-tanimli parcalama (FastCDC) ve Merkle dogrulamasi.

- Gear tablosu SABIT tohumdan uretilir: ayni dosya her makinede ayni
  parcalara ayrilir (surumlenebilir bir bicim).
- Parca sinirlari icerige baglidir: dosyaya bayt eklenince sonraki
  parcalarin cogu ayni kalir, yalnizca degisen bolge yeniden iner.
- Bildirim `.ferdim-merkle` (yayinci dosyanin yanina koyabilir):
  {v, algo, size, root, chunks: [[offset, length, hex]...]}
- Dogrulama parca ofsetlerini bildirimden alir (yeniden parcalamaz):
  hashlib hizinda calisir, indirme bitisinde pahali degildir.

Merkle make tarafi saf Python'da yavas kalirdi: bayt bayt yuruyen gear
dongusu yerine (bkz. _first_cut) sinir ADAYLARI toplu aritmetikle elenir.
Uretilen sinirlar klasik algoritmayla BAYT BAYT AYNIDIR; yayinlanmis
bildirimler gecerliligini korur.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import os
from dataclasses import dataclass
from pathlib import Path

from . import util

ALGO = "fastcdc-sha256"
SUFFIX = ".ferdim-merkle"
TMP_PREFIX = ".ferdim-"      # atomik yazimda gecici dosya oneki
MIN_CHUNK = 256 << 10        # 256 KiB
AVG_CHUNK = 1 << 20          # 1 MiB
MAX_CHUNK = 4 << 20          # 4 MiB
MAX_MANIFEST = 8 << 20       # bildirim ust siniri (bayt)
M64 = (1 << 64) - 1
_SEED = b"ferdim-cdc-v1"
_READ = 1 << 20


def _gear_table() -> tuple[int, ...]:
    """256 girdilik gear tablosu: sabit tohum + sha256 (deterministik)."""
    out = []
    for i in range(256):
        d = hashlib.sha256(_SEED + bytes((i,))).digest()
        out.append(int.from_bytes(d[:8], "little"))
    return tuple(out)


GEAR = _gear_table()


def _masks(avg_size: int) -> tuple[int, int]:
    """FastCDC normalizasyonu: ortalamaya kadar siki, sonrasinda gevsek
    maske. Beklenen parca boyu ~ avg_size olur."""
    bits = max(2, int(avg_size).bit_length() - 1)
    return (1 << (bits + 2)) - 1, (1 << (bits - 2)) - 1


# --------------------------------------------------------------- hizli tarama
# h_j (j konumundaki gear ozeti) yalnizca son baytlara baglidir: h_j'nin
# dusuk `n` bitini b[j-n..j-1] belirler. Bu yuzden sinir ADAYLARI bayt bayt
# yurumek yerine toplu aritmetikle elenir:
#   - her bayt gear'in dusuk 8 bitine cevrilir (bytes.translate, C hizi)
#   - her bayt 16 bitlik dilime acilir (utf-16-le, C hizi)
#   - acc * _POLY carpimi her konumun (h mod 2^8) degerini tek islemde verir
#   - sifir dilimler bytes.find ile atlanir (C hizi)
# On eleme yalniz GEREKLI kosulu ("dusuk 8 bit sifir") test eder: sinir
# kacmaz, her aday tam maske ile tek tek dogrulanir. _first_cut bu yolu
# yalniz buyuk bolgelerde kullanir; sonuc klasik donguyle aynidir.

_WIN = 8                     # on eleme penceresi (bayt, 2^8 kosulu icin)
_POLY = sum(1 << (17 * m) for m in range(_WIN))   # dilime 2^m agirlik
_GEAR_LO = bytes(g & 0xFF for g in GEAR)
_MAP_LO = bytes.maketrans(bytes(range(256)), _GEAR_LO)
_BLOCK = 1 << 14             # on eleme blok boyu (bayt)
_BULK_MIN = 1 << 15          # bundan kisa bolgede klasik dongu
_MAX_CAND = _BLOCK // 32     # blok basina aday siniri (asilirsa klasik yol)


def _candidates(part: bytes):
    """`part` icindeki aday konumlarin dilim ofsetleri (artan).

    Bir dilim, o dilimle biten konumun (h mod 2^8) degeridir
    (konum = taban + ofset + 1). Dilim degeri
    sum(2^m * (gear[b] & 0xFF)) < 2^16 oldugundan komsu dilime tasmaz:
    sifir dilim = iki sifir bayt (kucuk-endian dilim)."""
    n = len(part)
    acc = int.from_bytes(part.translate(_MAP_LO).decode("latin-1")
                         .encode("utf-16-le"), "little")
    raw = (acc * _POLY).to_bytes(2 * (n + _WIN - 1), "little")
    pos = 2 * (_WIN - 1)     # ilk tam pencere burada biter
    end = 2 * n
    while pos < end:
        i = raw.find(b"\x00", pos)
        if i < 0 or i >= end:
            return
        if i & 1:            # dilimin ust bayti: aday degil
            pos = i + 1
            continue
        yield i >> 1
        pos = i + 2


def _win_hash(buf, end: int, nbits: int) -> int:
    """`end` konumundan onceki `nbits` baytin gear ozeti (h_end)."""
    h = 0
    for j in range(end - nbits, end):
        h = ((h << 1) + GEAR[buf[j]]) & M64
    return h


def _scan_classic(buf, lo: int, hi: int, mask: int, floor: int) -> int:
    """Bayt bayt dongu: [lo, hi] konumlarinda ilk sinir (yoksa 0).

    h `floor` konumunda sifirlanir; `lo`'dan onceki konumlar yalniz
    hesaplanir, test edilmez."""
    h = 0
    j = floor
    while j < hi:
        h = ((h << 1) + GEAR[buf[j]]) & M64
        j += 1
        if j >= lo and not h & mask:
            return j
    return 0


def _bulk_cut(buf, lo: int, hi: int, mask: int, nbits: int) -> int:
    """Toptan tarama: [lo, hi] konumlarinda ilk sinir (yoksa 0).

    `lo` konumunun tam penceresi vardir (cagiran garanti eder)."""
    start = lo
    while start <= hi:
        stop = min(hi, start + _BLOCK - 1)
        base = start - _WIN
        part = buf[base:stop + 1]
        if not isinstance(part, bytes):
            part = bytes(part)
        seen = 0
        for r in _candidates(part):
            p = base + r + 1         # dilimin son bayti = konum - 1
            if p > stop:
                # part bir bayt fazla uzar: son aday blok sinirini asar.
                # Bu konum sonraki blogun ilk adayidir (son blogun disinda
                # kalir): burada test edilirse parca max_size+1 bayt olur.
                break
            if not _win_hash(buf, p, nbits) & mask:
                return p
            seen += 1
            if seen > _MAX_CAND:
                # adaylar yogun (koti niyetli/duzenli veri): dogrulama
                # maliyeti klasik dongunun uzerine cikar
                return _scan_window(buf, start, hi, mask, nbits)
        start = stop + 1
    return 0


def _scan_window(buf, start: int, hi: int, mask: int, nbits: int) -> int:
    """Klasik dongu, h son `nbits` bayttan kurularak: [start, hi] araligi.

    Test yalnizca h'nin dusuk `nbits` bitine baktigi icin hazir pencere
    ozeti yeterlidir (ust bitler onemsiz)."""
    h = _win_hash(buf, start, nbits)
    if not h & mask:
        return start
    j = start
    while j < hi:
        h = ((h << 1) + GEAR[buf[j]]) & M64
        j += 1
        if not h & mask:
            return j
    return 0


def _first_cut(buf, lo: int, hi: int, mask: int, floor: int) -> int:
    """[lo, hi] konumlarinda (h & mask) == 0 olan ilk konum (yoksa 0).

    h `floor` konumunda sifirlanir. Konumun dusuk `mask.bit_length()`
    biti son o kadar bayttan geldigi icin pencere dolduktan sonra
    konumlar bagimsiz dogrulanabilir (toptan tarama)."""
    if lo > hi:
        return 0
    nbits = mask.bit_length()
    p0 = max(lo, floor + nbits, _WIN)     # tam pencereli ilk konum
    if lo < p0:
        cut = _scan_classic(buf, lo, min(hi, p0 - 1), mask, floor)
        if cut or p0 > hi:
            return cut
    # on eleme "dusuk 8 bit sifir" der: maske 8 bitten kisaysa aday kacirir
    if hi - p0 + 1 >= _BULK_MIN and nbits >= 8:
        return _bulk_cut(buf, p0, hi, mask, nbits)
    return _scan_classic(buf, p0, hi, mask, floor)


def chunk_ranges(buf, min_size: int = MIN_CHUNK, avg_size: int = AVG_CHUNK,
                 max_size: int = MAX_CHUNK):
    """Icerik-tanimli parca araliklari: (bas, son) uretir.

    `buf` bayt benzeri (bytes, memoryview, mmap): yalnizca okunur.
    Son parca kisa olabilir; digerleri min_size..max_size arasindadir.
    """
    mask_s, mask_l = _masks(avg_size)
    n = len(buf)
    if min_size < 1:
        min_size = 1
    if max_size < min_size:
        max_size = min_size
    i = 0
    while i < n:
        left = n - i
        if left <= min_size:
            yield i, n
            return
        limit = n if left <= max_size else i + max_size
        mid = min(i + avg_size, limit)
        floor = i + min_size
        # ilk faz siki, ikinci faz gevsek maske; iki faz da ayni h'yi surer
        cut = _first_cut(buf, floor + 1, mid, mask_s, floor)
        if not cut:
            cut = _first_cut(buf, mid + 1, limit, mask_l, floor)
            if not cut:
                cut = limit
        yield i, cut
        i = cut


def _open_mapped(path):
    """(dosya, arabellek) - bos dosyada mmap kurulmaz."""
    f = open(path, "rb")
    size = os.fstat(f.fileno()).st_size
    if size == 0:
        return f, b""
    try:
        return f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    except Exception:
        f.close()
        raise


def chunk_hashes(path, min_size: int = MIN_CHUNK, avg_size: int = AVG_CHUNK,
                 max_size: int = MAX_CHUNK) -> list[tuple[int, int, str]]:
    """Dosyayi icerik-tanimli parcalara ayirir: [(offset, uzunluk, hex)]."""
    f, buf = _open_mapped(path)
    try:
        return [(s, e - s, hashlib.sha256(buf[s:e]).hexdigest())
                for s, e in chunk_ranges(buf, min_size, avg_size, max_size)]
    finally:
        if isinstance(buf, mmap.mmap):
            buf.close()
        f.close()


def merkle_root(hexes) -> str:
    """Ikili Merkle koku. Tek kalan dugum ust katmana tasinir; yaprak/ic
    ayrimi ve parca sayisi kok icine yazilir (kirpma/genisletme belirsiz
    kalmasin)."""
    nodes = [bytes.fromhex(h) for h in hexes]
    count = len(nodes)
    if not nodes:
        return hashlib.sha256(_SEED + (0).to_bytes(8, "big")).hexdigest()
    while len(nodes) > 1:
        nxt = [hashlib.sha256(b"\x01" + nodes[k] + nodes[k + 1]).digest()
               for k in range(0, len(nodes) - 1, 2)]
        if len(nodes) % 2:
            nxt.append(nodes[-1])
        nodes = nxt
    return hashlib.sha256(b"\x00" + count.to_bytes(8, "big")
                          + nodes[0]).hexdigest()


def _hex64(value) -> str:
    s = str(value or "").strip().lower()
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        raise ValueError("gecersiz parca ozeti")
    return s


@dataclass(frozen=True)
class Manifest:
    """`.ferdim-merkle` bildirimi: parca ofsetleri + Merkle koku."""
    size: int
    root: str
    chunks: tuple[tuple[int, int, str], ...]
    algo: str = ALGO
    v: int = 1

    @classmethod
    def of(cls, chunks, size: int | None = None) -> "Manifest":
        ch = tuple(chunks)
        total = sum(ln for _, ln, _ in ch) if size is None else int(size)
        return cls(size=total, root=merkle_root([h for _, _, h in ch]),
                   chunks=ch)

    @classmethod
    def parse(cls, text: str) -> "Manifest":
        """Bildirimi dogrular. Bozuk/eksik/tutarsiz bildirim ValueError:
        uydurma bir bildirimle indirilen dosyayi 'dogrulanmis' saymayiz."""
        try:
            data = json.loads(text)
        except (ValueError, RecursionError) as e:
            # derin ic ice JSON'da json.loads RecursionError sizdirir:
            # sozlesme tek tur (ValueError) kalsin
            raise ValueError(f"gecersiz JSON: {e}") from None
        if not isinstance(data, dict):
            raise ValueError("bildirim sozluk degil")
        if data.get("v") != 1:
            raise ValueError("desteklenmeyen surum")
        if data.get("algo") != ALGO:
            raise ValueError("desteklenmeyen algoritma")
        size = data.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("gecersiz boyut")
        raw = data.get("chunks")
        if not isinstance(raw, list):
            raise ValueError("parca listesi yok")
        chunks = []
        pos = 0
        for item in raw:
            if not (isinstance(item, list) and len(item) == 3):
                raise ValueError("gecersiz parca kaydi")
            off, ln, hx = item
            if not (isinstance(off, int) and isinstance(ln, int)):
                raise ValueError("gecersiz parca ofseti")
            if off != pos or ln <= 0:
                raise ValueError("parcalar bitisik degil")
            if ln > MAX_CHUNK:
                raise ValueError("parca boyu ust sinirin uzerinde")
            chunks.append((off, ln, _hex64(hx)))
            pos += ln
        if pos != size:
            raise ValueError("parca boylari toplami dosya boyuna esit degil")
        # amplifikasyon siniri: uydurma bildirim binlerce kucuk parca
        # bildirip dogrulamayi/onarimi sismesin. Son parca kisa olabilir,
        # digerleri MIN_CHUNK'tan kucuk olamaz (uretilen bildirim boyle).
        if len(chunks) > size // MIN_CHUNK + 1:
            raise ValueError("parca sayisi dosya boyuna gore fazla")
        for _, ln, _ in chunks[:-1]:
            if ln < MIN_CHUNK:
                raise ValueError("parca boyu alt sinirin altinda")
        root = _hex64(data.get("root"))
        if merkle_root([h for _, _, h in chunks]) != root:
            raise ValueError("kok parca listesiyle uyusmuyor")
        return cls(size=size, root=root, chunks=tuple(chunks))

    def dumps(self) -> str:
        return json.dumps({
            "v": self.v, "algo": self.algo, "size": self.size,
            "root": self.root,
            "chunks": [[off, ln, hx] for off, ln, hx in self.chunks],
        }, separators=(",", ":"), ensure_ascii=True)

    def offset_of(self, pos: int) -> int:
        """`pos` baytini iceren parcanin (offset, uzunluk) degeri."""
        for off, ln, _ in self.chunks:
            if off <= pos < off + ln:
                return off, ln
        return -1, 0


def make_manifest(path, min_size: int = MIN_CHUNK, avg_size: int = AVG_CHUNK,
                  max_size: int = MAX_CHUNK) -> Manifest:
    """Dosyadan bildirim uretir (yayinci tarafi)."""
    chunks = chunk_hashes(path, min_size, avg_size, max_size)
    return Manifest.of(chunks, os.path.getsize(path))


def read_manifest(path) -> Manifest:
    return Manifest.parse(Path(path).read_text("utf-8", "replace"))


def write_atomic(path, text: str) -> Path:
    """Metni atomik yazar (paylasilan `util.atomic_write_bytes` yolu).

    Gecici dosya hedef dizinde `.ferdim-` onekiyle BENZERSIZ acilir (mevcut
    bir adi/sembolik bagi asla izlemez), fsync + 0644, sonra os.replace
    hedefin uzerine koyar, dizin de fsync edilir. replace bagin KENDISINI
    degistirir: onceden kurulmus bir bag varsa gosterdigi dosya degismez.
    """
    p = Path(path)
    util.atomic_write_bytes(p, text.encode("utf-8"), mode=0o644,
                            prefix=TMP_PREFIX)
    return p


def write_manifest(man: Manifest, path) -> Path:
    return write_atomic(path, man.dumps() + "\n")


def verify_file(path, man: Manifest, halt=None):
    """(boyut uygun mu, bozuk parcalar). Ofsetler bildirimden gelir:
    yeniden parcalamaya gerek yok, okuma siraldir.

    `halt`: dogrulama durdurulmali mi (duraklat/iptal). Her parca ve her
    okuma blogu basinda sorulur; True donerse dogrulama kesilir ve None
    doner - boylece duraklatma uzun bir dogrulamayi beklemez."""
    if halt is not None and halt():
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, []
    if size != man.size:
        return False, []
    bad = []
    with open(path, "rb") as f:
        for off, ln, hx in man.chunks:
            if halt is not None and halt():
                return None
            f.seek(off)
            h = hashlib.sha256()
            left = ln
            while left > 0:
                b = f.read(min(_READ, left))
                if not b:
                    return False, []
                h.update(b)
                left -= len(b)
                if halt is not None and halt():
                    return None
            if h.hexdigest() != hx:
                bad.append((off, ln))
    return True, bad


def index_file(path, min_size: int = MIN_CHUNK, avg_size: int = AVG_CHUNK,
               max_size: int = MAX_CHUNK) -> dict[str, tuple[int, int]]:
    """Yerel dosyanin parca ozeti -> (offset, uzunluk). Ayni icerikli parca
    dosyanin neresinde olursa olsun bulunur (ekleme/silme kaydirir)."""
    index: dict[str, tuple[int, int]] = {}
    for off, ln, hx in chunk_hashes(path, min_size, avg_size, max_size):
        index.setdefault(hx, (off, ln))
    return index


def _fetch_range(fd: int, url: str, opt, start: int, end: int,
                 etag: str, last_modified: str) -> None:
    """Tek Range baglantisini `fd` uzerinde `start` ofsetine yazar."""
    from .engine import httpcli
    try:
        resp = httpcli.open_range(url, opt, start, end, etag, last_modified,
                                  timeout=opt.timeout)
    except httpcli.RangeInvalid:
        raise ValueError("sunucu Range'i yok saydi") from None
    try:
        pos = start
        while pos <= end:
            b = resp.read(min(_READ, end - pos + 1))
            if not b:
                raise ValueError("baglanti erken kapandi")
            os.pwrite(fd, b, pos)
            pos += len(b)
    finally:
        resp.close()


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Bitisik araliklari tek istekte birlestirir."""
    out: list[tuple[int, int]] = []
    for off, ln in ranges:
        if out and out[-1][0] + out[-1][1] == off:
            out[-1] = (out[-1][0], out[-1][1] + ln)
        else:
            out.append((off, ln))
    return out


def update_file(src, url: str, man: Manifest, out=None, opt=None,
                min_size: int = MIN_CHUNK, avg_size: int = AVG_CHUNK,
                max_size: int = MAX_CHUNK) -> dict:
    """Yeni surumu, yerel dosyadaki AYNI parcalari koruyarak birlestirir.

    Yalnizca eslesmeyen parcalarin bayt araliklari HTTP Range ile indirilir
    (tek akis). Cikti dogrulanmadan hedefin yerine konmaz: hata halinde
    `.ferdim-part` dosyasi kalir, kaynak dosya bozulmaz.
    """
    from .engine import httpcli
    src = Path(src)
    dest = Path(out) if out else src
    index = index_file(src, min_size, avg_size, max_size)
    reused = 0
    missing: list[tuple[int, int]] = []
    for off, ln, hx in man.chunks:
        loc = index.get(hx)
        if loc is not None and loc[1] == ln:
            reused += ln
        else:
            missing.append((off, ln))
    missing = _merge(missing)
    opt = opt or httpcli.DlOptions(timeout=30)
    tmp = dest.with_name(dest.name + ".ferdim-part")
    fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    fetched = 0
    try:
        os.ftruncate(fd, man.size)
        if reused:
            with open(src, "rb") as sf:
                for off, ln, hx in man.chunks:
                    loc = index.get(hx)
                    if loc is None or loc[1] != ln:
                        continue
                    sf.seek(loc[0])
                    os.pwrite(fd, sf.read(loc[1]), off)
        if missing:
            p = httpcli.probe(url, opt)
            if p.total != man.size:
                raise ValueError("uzak dosya bildirimle ayni boyutta degil")
            if not p.accept_ranges:
                raise ValueError("sunucu Range desteklemiyor")
            for off, ln in missing:
                _fetch_range(fd, url, opt, off, off + ln - 1,
                             p.etag, p.last_modified)
                fetched += ln
        os.fsync(fd)
    finally:
        os.close(fd)
    size_ok, bad = verify_file(tmp, man)
    if not size_ok or bad:
        raise ValueError(f"birlesen dosya dogrulanamadi ({len(bad)} parca)")
    os.replace(tmp, dest)
    return {"path": str(dest), "reused": reused, "fetched": fetched,
            "chunks": len(man.chunks), "requests": len(missing),
            "root": man.root}
