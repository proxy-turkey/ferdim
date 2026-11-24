"""Arsiv destegi: ZIP (ic ice dahil) ve TAR listeleme, secmeli cikarma,
yarim kalan cikarmayi onarma. Yalniz stdlib (zipfile, tarfile).

Cikarma yolunda zorunlu guvenlik kurallari:
  * '..' bileseni, mutlak yol ve surucu oneki reddedilir,
  * hedef dizin disina isaret eden ust dizin/baglanti kullanilmaz,
  * tar sembolik/sert baglantilari ve aygit dosyalari hic olusturulmaz,
  * ic arsiv okumasi boyut ve sikistirma orani ile sinirlanir (zip bombasi),
  * tek cikarmada toplam bayt ve bos disk alani denetlenir.

Ic ice ZIP uyeleri "outer.zip!/inner.zip!/dosya" yoluyla gosterilir; ayni
yol cikarma isteginde de kabul edilir (arsiv adi oneki istege bagli)."""
from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import zipfile
import zlib
from collections import namedtuple
from pathlib import Path

MAX_DEPTH = 3                    # ust arsiv + en fazla iki ic arsiv
MAX_ENTRIES = 2000               # listelemede toplam kayit siniri
MAX_INNER = 256 * 1024 ** 2      # ic arsiv okuma boyutu (bomba korumasi)
MAX_TOTAL = 8 * 1024 ** 3        # tek cikarmada toplam bayt siniri
RATIO_MAX = 1000.0               # supheli sikistirma orani
RATIO_MIN_SIZE = 16 * 1024 ** 2  # oran denetimi icin en kucuk boyut
DISK_MARGIN = 64 * 1024 ** 2     # bos disk icin guvenlik payi
CHUNK = 256 * 1024
MANIFEST = ".ferdim-extract.json"
PART = ".part"
SPOOL = 4 * 1024 * 1024          # ic arsiv bellege sigarsa bellekte kalir
MAX_TASKS = 8                    # son kac cikarma gorevi saklanir

_STEM_SUFFIXES = (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tgz",
                  ".tbz2", ".txz", ".zip", ".tar")

_Meta = namedtuple("_Meta", "name size compressed crc link iszip")


class ArchiveError(Exception):
    """Kullaniciya gosterilebilir arsiv hatasi (ASCII Turkce)."""


# ------------------------------------------------------------------ gorunum
class _ZipView:
    """zipfile.ZipFile uzerinde tek bicimli okuma yuzeyi."""

    def __init__(self, zf: zipfile.ZipFile):
        self._zf = zf

    def entries(self):
        for i in self._zf.infolist():
            if i.is_dir():
                continue
            mode = i.external_attr >> 16
            yield _Meta(i.filename, i.file_size, i.compress_size, i.CRC,
                        stat.S_ISLNK(mode),
                        i.filename.lower().endswith(".zip"))

    def open(self, name: str):
        return self._zf.open(name)

    def close(self):
        self._zf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _TarView:
    """tarfile.TarFile uzerinde ayni yuzey; baglantilar 'link' sayilir."""

    def __init__(self, tf: tarfile.TarFile):
        self._tf = tf

    def entries(self):
        for m in self._tf:
            if m.isdir():
                continue
            yield _Meta(m.name, m.size, None, None, not m.isreg(),
                        m.name.lower().endswith(".zip"))

    def open(self, name: str):
        f = self._tf.extractfile(name)
        if f is None:
            raise ArchiveError("okunamaz uye: " + name)
        return f

    def close(self):
        self._tf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _view(path, fmt: str):
    if fmt == "zip":
        try:
            return _ZipView(zipfile.ZipFile(path))
        except (zipfile.BadZipFile, OSError) as e:
            raise ArchiveError(f"arsiv okunamadi: {e}") from e
    try:
        return _TarView(tarfile.open(path, "r:*"))
    except (tarfile.TarError, OSError, EOFError) as e:
        raise ArchiveError(f"arsiv okunamadi: {e}") from e


def _detect(path: Path) -> str:
    """Bicim: 'zip', 'tar' (gz/bz2/xz dahil) ya da '' (desteklenmez).

    Once dosya imzasina bakilir: tar icindeki kucuk bir ZIP uyenin merkezi
    dizini dosyanin sonuna yakin olabilir, tek basina is_zipfile yaniltir."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError as e:
        raise ArchiveError(f"arsiv okunamadi: {e}") from e
    if magic in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08") \
            and zipfile.is_zipfile(path):
        return "zip"
    try:
        with tarfile.open(path, "r:*") as tf:
            for _ in tf:
                break
        return "tar"
    except (tarfile.TarError, OSError, EOFError):
        pass
    try:
        if zipfile.is_zipfile(path):      # onekli (kendinden cikar) zip
            return "zip"
    except OSError as e:
        raise ArchiveError(f"arsiv okunamadi: {e}") from e
    return ""


# ---------------------------------------------------------------- listeleme
def _ratio(meta: _Meta) -> float:
    return (meta.size / meta.compressed) if meta.compressed else 0.0


def _copy_limited(src, dst, limit: int) -> int:
    n = 0
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            return n
        n += len(chunk)
        if n > limit:
            raise ArchiveError("ic arsiv boyut siniri asildi")
        dst.write(chunk)


def _expand(view, meta: _Meta, prefix: str, chain: list, depth: int,
            out: list, state: dict) -> None:
    """Ic arsivi gecici akisa kopyalayip ust seviyeyle ayni sekilde gezer."""
    buf = tempfile.SpooledTemporaryFile(max_size=SPOOL)
    try:
        with view.open(meta.name) as src:
            _copy_limited(src, buf, MAX_INNER)
        buf.seek(0)
        if not zipfile.is_zipfile(buf):
            raise ArchiveError("ic arsiv okunamadi")
        buf.seek(0)
        with _ZipView(zipfile.ZipFile(buf)) as sub:
            _walk(sub, prefix, chain, depth, out, state)
    finally:
        buf.close()


def _walk(view, prefix: str, chain: list, depth: int, out: list,
          state: dict) -> None:
    """chain: ic ice kapsayici adlari (kanonik anahtar icin)."""
    for meta in view.entries():
        if len(out) >= state["limit"]:
            state["more"] += 1
            continue
        bomb = _ratio(meta) > RATIO_MAX and meta.size > RATIO_MIN_SIZE
        rec = {"path": f"{prefix}!/{meta.name}", "name": meta.name,
               "key": "!".join(chain + [meta.name]), "size": meta.size,
               "compressed": meta.compressed, "crc": meta.crc,
               "depth": depth, "kind": "link" if meta.link else "file",
               "bomb": bomb}
        out.append(rec)
        if not state["nested"] or not meta.iszip or meta.link:
            continue
        if depth >= MAX_DEPTH:
            continue
        if meta.size > MAX_INNER:
            rec["note"] = "ic arsiv boyut siniri"
            continue
        if bomb:
            rec["note"] = "ic arsiv sikistirma orani supheli"
            continue
        try:
            _expand(view, meta, rec["path"], chain + [meta.name], depth + 1,
                    out, state)
        except ArchiveError as e:
            rec["note"] = str(e)[:120]
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as e:
            rec["note"] = f"ic arsiv okunamadi: {e}"[:120]


def describe(path, nested: bool = True, limit: int | None = None) -> dict:
    """Arsiv icerigini listeler.

    Donen: {'archive', 'format', 'entries': [...], 'note', 'total'}.
    Kayit yolu ust arsiv adiyla baslar: 'outer.zip!/a.txt'; ic ice uyelerde
    'outer.zip!/inner.zip!/b.txt' (derinlik siniri MAX_DEPTH)."""
    path = Path(path)
    if not path.is_file():
        raise ArchiveError("arsiv yok")
    fmt = _detect(path)
    if not fmt:
        raise ArchiveError("desteklenmeyen arsiv bicimi (7z/rar desteklenmez)")
    state = {"nested": bool(nested), "more": 0,
             "limit": int(limit if limit is not None else MAX_ENTRIES)}
    entries: list = []
    with _view(path, fmt) as view:
        _walk(view, path.name, [], 1, entries, state)
    note = f"+{state['more']}…" if state["more"] else ""
    return {"archive": path.name, "format": fmt, "entries": entries,
            "note": note, "total": len(entries)}


# -------------------------------------------------------- yollar ve kayit
def _stem(name: str) -> str:
    low = name.lower()
    for suffix in _STEM_SUFFIXES:
        if low.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return os.path.splitext(name)[0] or name


def default_dest(path, dest=None) -> Path:
    """Cikarma hedefi: verilen dizin ya da arsiv yaninda '<ad>/'."""
    if dest is not None and str(dest).strip():
        return Path(str(dest)).expanduser()
    p = Path(path)
    return p.parent / _stem(p.name)


def _safe_parts(rel: str) -> list:
    """Yolu bilesenlere ayirir; kacis denemelerini reddeder."""
    p = rel.replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or p.startswith("~"):
        raise ArchiveError("guvensiz yol: " + rel)
    out = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or "\x00" in part:
            raise ArchiveError("guvensiz yol: " + rel)
        out.append(part)
    if not out:
        raise ArchiveError("gecersiz uye: " + rel)
    return out


def _target(dest: Path, parts: list) -> Path:
    """Hedef yol; ara bilesenlerden biri baglantiysa reddedilir."""
    cur = dest
    for part in parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise ArchiveError("guvensiz baglanti: " + str(cur))
    return cur / parts[-1]


def _norm_member(member, archive: str) -> str:
    """Listeleme yolunu ('outer.zip!/inner.zip!/a') kanonik anahtara cevirir
    ('inner.zip!a'); dogrudan uye adi da kabul edilir."""
    text = str(member).strip()
    for prefix in (archive + "!/", archive + "!"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return "!".join(seg[1:] if seg.startswith("/") else seg
                    for seg in text.split("!"))


def _rel_parts(key: str) -> list:
    """'inner.zip!/docs/a.txt' -> ['inner', 'docs', 'a.txt'] (arsiv adi
    uzantisi dizin adindan duser); mutlak bilesen reddedilir."""
    segs = [s for s in key.replace("\\", "/").split("!") if s]
    if not segs or any(s.startswith("/") for s in segs):
        raise ArchiveError("guvensiz yol: " + key)
    dirs = []
    for seg in segs[:-1]:
        base = seg.rstrip("/").rsplit("/", 1)[-1]
        if base.lower().endswith(".zip"):
            base = base[:-4]
        dirs.append(base or "arsiv")
    return _safe_parts("/".join(dirs + [segs[-1]]))


def load_manifest(dest) -> dict:
    try:
        data = json.loads((Path(dest) / MANIFEST).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("members"), dict):
        return {}
    return data


def has_manifest(dest) -> bool:
    return bool(load_manifest(dest))


def _save_manifest(dest: Path, data: dict) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / (MANIFEST + PART)
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    os.replace(tmp, dest / MANIFEST)


def _file_crc(path: Path) -> int:
    crc = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xffffffff


def _intact(dest: Path, rec: dict) -> bool:
    """Kayittaki uye yerinde ve saglam mi? (boyut + varsa CRC32)."""
    try:
        target = _target(dest, _safe_parts(str(rec.get("target") or "")))
        size = target.stat().st_size
    except (ArchiveError, OSError):
        return False
    if size != int(rec.get("size", -1)):
        return False
    crc = rec.get("crc")
    if crc is None:
        return True
    try:
        return _file_crc(target) == int(crc) & 0xffffffff
    except OSError:
        return False


@contextlib.contextmanager
def _member_stream(path: Path, fmt: str, key: str):
    """Uyeyi akis olarak acar; ic ice zincir gecici akislara cozulur."""
    segs = key.split("!")
    views, spools = [], []
    try:
        view = _view(path, fmt)
        views.append(view)
        for name in segs[:-1]:
            buf = tempfile.SpooledTemporaryFile(max_size=SPOOL)
            spools.append(buf)
            with view.open(name) as src:
                _copy_limited(src, buf, MAX_INNER)
            buf.seek(0)
            if not zipfile.is_zipfile(buf):
                raise ArchiveError("ic arsiv okunamadi: " + name)
            buf.seek(0)
            view = _ZipView(zipfile.ZipFile(buf))
            views.append(view)
        with view.open(segs[-1]) as f:
            yield f
    finally:
        for v in reversed(views):
            try:
                v.close()
            except Exception:
                pass
        for b in spools:
            try:
                b.close()
            except Exception:
                pass


def _fail(msg: str, dest=None, code: str = "") -> dict:
    out = {"ok": False, "error": msg, "dest": str(dest or ""),
           "extracted": 0, "skipped": 0, "errors": [], "bytes": 0}
    if code:
        out["code"] = code
    return out


def _selection(entries: list, archive: str, members, repair: bool,
               manifest: dict, dest: Path) -> tuple:
    """(secilecekler, atlanan, hatalar) - onarimda yalniz bozuk uyeler."""
    by_key = {e["key"]: e for e in entries}
    errors: list = []
    selected: list = []
    skipped = 0
    if repair:
        for key, rec in manifest["members"].items():
            entry = by_key.get(key)
            if not entry:
                errors.append(f"{key}: arsivde yok")
                continue
            if _intact(dest, rec):
                skipped += 1
                continue
            selected.append((key, entry))
        return selected, skipped, errors
    if members:
        seen = set()
        for member in members:
            key = _norm_member(member, archive)
            if not key or key in seen:
                continue
            seen.add(key)
            entry = by_key.get(key)
            if not entry:
                errors.append(f"{member}: arsivde yok")
            elif entry["kind"] != "file":
                skipped += 1
            else:
                selected.append((key, entry))
        return selected, skipped, errors
    for key, entry in by_key.items():
        if entry["kind"] != "file":
            skipped += 1
            continue
        selected.append((key, entry))
    return selected, skipped, errors


def extract(path, members=None, dest=None, repair: bool = False,
            progress=None) -> dict:
    """Secilen uyeleri (bos = tumu) hedef dizine cikarir.

    Her uye 'ad.part' dosyasina yazilip os.replace ile yerine konur; basarili
    her uyeden sonra .ferdim-extract.json guncellenir, boylece kesilen bir
    cikarma 'repair' ile kaldigi yerden onarilabilir."""
    path = Path(path)
    if not path.is_file():
        return _fail("arsiv yok")
    try:
        info = describe(path, nested=True)
    except ArchiveError as e:
        return _fail(str(e))
    entries = info["entries"]
    target = default_dest(path, dest)
    if repair:
        manifest = load_manifest(target)
        if not manifest:
            return _fail("cikarma kaydi yok", target, code="no_manifest")
    else:
        manifest = {"version": 1, "archive": path.name,
                    "created": time.time(), "members": {}}
    selected, skipped, errors = _selection(entries, path.name, members,
                                          repair, manifest, target)
    planned: list = []
    plan = 0
    for key, entry in selected:
        try:
            rel = "/".join(_rel_parts(key))
        except ArchiveError as e:
            errors.append(f"{key}: {e}")
            continue
        planned.append((key, entry, rel))
        plan += max(0, int(entry["size"]))
    if plan > MAX_TOTAL:
        return _fail(f"cikarma boyut siniri asildi ({plan} bayt)", target)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _fail(f"hedef dizin olusturulamadi: {e}", target)
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        free = None
    if free is not None and plan + DISK_MARGIN > free:
        return _fail("disk alani yetmiyor", target)
    if not repair:
        # planlanan tum uyeler bastan yazilir: ilk uyede kesilse bile
        # 'repair' eksikleri (arsivdeki boyut/CRC ile) tamamlayabilir
        manifest["members"] = {
            key: {"size": int(entry["size"]), "crc": entry.get("crc"),
                  "target": rel, "at": 0}
            for key, entry, rel in planned}
        _save_manifest(target, manifest)

    result = {"ok": True, "dest": str(target), "extracted": 0,
              "skipped": skipped, "errors": errors, "bytes": 0,
              "repair": bool(repair)}
    total = len(planned)
    if progress:
        progress(0, total, 0, "")
    for i, (key, entry, rel) in enumerate(planned, 1):
        try:
            parts = _rel_parts(key)
            tgt = _target(target, parts)
            tgt.parent.mkdir(parents=True, exist_ok=True)
            part = tgt.with_name(tgt.name + PART)
            crc, size = 0, 0
            try:
                with _member_stream(path, info["format"], key) as src, \
                        open(part, "wb") as out:
                    while True:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        if result["bytes"] + size > MAX_TOTAL:
                            raise ArchiveError("cikarma boyut siniri asildi")
                        crc = zlib.crc32(chunk, crc)
                        out.write(chunk)
                os.replace(part, tgt)
            except BaseException:
                try:
                    part.unlink()
                except OSError:
                    pass
                raise
            result["bytes"] += size
            result["extracted"] += 1
            manifest["members"][key] = {"size": size,
                                        "crc": crc & 0xffffffff,
                                        "target": rel, "at": time.time()}
            _save_manifest(target, manifest)
        except (ArchiveError, OSError, zipfile.BadZipFile, tarfile.TarError,
                EOFError, ValueError) as e:
            errors.append(f"{key}: {e}")
        if progress:
            progress(i, total, result["bytes"], key)
    if errors:
        result["ok"] = False
    return result


# ------------------------------------------------------- arka plan gorevleri
_TASKS: dict = {}
_TLOCK = threading.Lock()
_SEQ = itertools.count(1)


def _prune_locked() -> None:
    if len(_TASKS) < MAX_TASKS:
        return
    for key in sorted(_TASKS, key=lambda k: _TASKS[k].get("started", 0)
                      )[:len(_TASKS) - MAX_TASKS + 1]:
        _TASKS.pop(key, None)


def task_status(task_id: str) -> dict:
    with _TLOCK:
        task = _TASKS.get(task_id)
        if not task:
            return {"ok": False, "error": "gorev yok"}
        return {"ok": True, **{k: task.get(k) for k in (
            "id", "kind", "state", "done", "total", "bytes", "current",
            "dest", "error", "result")}}


def _run_task(task_id: str, path, members, target, repair: bool) -> None:
    def progress(done, total, nbytes, current):
        with _TLOCK:
            task = _TASKS.get(task_id)
            if task:
                task.update(done=int(done), total=int(total),
                            bytes=int(nbytes), current=str(current))

    try:
        result = extract(path, members=members, dest=target, repair=repair,
                         progress=progress)
    except Exception as e:      # motor hatasi gorevi kilitler
        result = _fail(str(e), target)
    with _TLOCK:
        task = _TASKS.get(task_id)
        if task:
            task["result"] = result
            task["state"] = "done" if result.get("ok") else "error"
            task["error"] = "" if result.get("ok") else (
                result.get("error") or (result.get("errors") or [""])[0])


def start_extract(path, members=None, dest=None, repair: bool = False) -> dict:
    """Cikarmayi arka planda baslatir; gorev ozeti dondurur.
    Ilerleme task_status() ile izlenir (web/CLI ayni sozlesmeyi kullanir)."""
    target = default_dest(path, dest)
    with _TLOCK:
        _prune_locked()
        task_id = f"a{next(_SEQ)}"
        task = {"id": task_id, "kind": "repair" if repair else "extract",
                "state": "running", "done": 0, "total": 0, "bytes": 0,
                "current": "", "dest": str(target), "error": "",
                "result": None, "started": time.time()}
        _TASKS[task_id] = task
    thread = threading.Thread(target=_run_task,
                              args=(task_id, path, members, target, repair),
                              name=f"ferdim-arsiv-{task_id}", daemon=True)
    thread.start()
    return task_status(task_id)
