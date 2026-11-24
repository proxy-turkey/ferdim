"""Statik site yayin yardimcisi: yayin dosyalarinin yanina butunluk
bildirimleri yazar.

Dosya basina uretilenler:
  - `<ad>.ferdim-merkle`  icerik tanimli parcalar + Merkle koku (cdc)
  - `<ad>.sha256`         coreutils bicimi "<ozet>  <ad>"
  - `<ad>.meta4`          RFC 5854: boyut, sha-256, merkle metaurl, adresler
Dizinin tamami icin tek `index.meta4` yazilir.

Cikti bayt-kararlidir: dosyalar ad sirasina gore islenir, tek zaman
damgasi dosya mtime'inden gelen `<published>` alanidir (RFC 3339 UTC) ve
icerigi degismeyen dosya yeniden yazilmaz. Ayni girdilerle ikinci
calistirma ayni baytlari uretir; statik site kopyalamasi bos diff verir.

Kapsam bilincli olarak dar: alt dizinlere inilmez, sembolik baglar
yayinlanmaz, kendi urettigimiz uzantilar girdi sayilmaz.
"""
from __future__ import annotations

import fnmatch
import os
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from . import cdc, util

NS = "urn:ietf:params:xml:ns:metalink"
SHA256_SUFFIX = ".sha256"
META4_SUFFIX = ".meta4"
INDEX_NAME = "index.meta4"
# metalink tarafinda ayni adlar bekleniyor (metaurl mediatype + hash type)
METAURL_TYPE = "application/x-ferdim-merkle"
MERKLE_ROOT_TYPE = "ferdim-merkle-root"
# kendi cikti uzantilarimiz: girdi olarak yayinlanmaz
OWN_SUFFIXES = (cdc.SUFFIX, SHA256_SUFFIX, META4_SUFFIX, ".part")


def _check_url(value: str, label: str) -> str:
    """Yalniz http(s) kabul edilir: kendi ayristiricimiz de bunlari alir,
    boylece uretilen belge geri okunabilir kalir."""
    v = str(value or "").strip()
    if not v.lower().startswith(("http://", "https://")):
        raise ValueError(f"{label} http(s) adresi olmali: {value!r}")
    return v


def _url(base: str, name: str) -> str:
    """Temel adres + yuzde-kodlanmis dosya adi (bosluk/unicode guvenli)."""
    return base.rstrip("/") + "/" + urllib.parse.quote(name, safe="")


def _rfc3339(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _write_if_changed(path: Path, text: str) -> bool:
    """Ayni icerik yeniden yazilmaz: mtime sabit kalir, tekrar calistirma
    yayin dizinini degistirmez. Yazim `.part` + os.replace (atomik)."""
    try:
        if path.read_text("utf-8") == text:
            return False
    except OSError:
        pass
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def _manifest(src: Path, man_path: Path, force: bool):
    """(Manifest, yeniden uretildi mi).

    Guncel bildirim yeniden kullanilir: boyutu dosyayla ayni ve dosyadan
    yasli degil. Bozuk/bayat bildirim sessizce yeniden uretilir; `force`
    her durumda yeniden uretir.
    """
    st = src.stat()
    if not force and man_path.is_file():
        try:
            if man_path.stat().st_mtime >= st.st_mtime:
                man = cdc.read_manifest(man_path)
                if man.size == st.st_size:
                    return man, False
        except (OSError, ValueError):
            pass
    man = cdc.make_manifest(src)
    cdc.write_manifest(man, man_path)
    return man, True


def _publishable(p: Path, pattern: str | None) -> bool:
    if p.name.endswith(OWN_SUFFIXES) or not p.is_file():
        return False
    return pattern is None or fnmatch.fnmatchcase(p.name, pattern)


@dataclass(frozen=True)
class Entry:
    """Bir yayin dosyasi: metalink <file> ogesinin butun alanlari."""
    name: str
    size: int
    sha256: str
    url: str
    mirrors: tuple[str, ...] = ()
    merkle_root: str = ""
    merkle_url: str = ""


def _file_el(entry: Entry) -> ET.Element:
    f = ET.Element("file", {"name": entry.name})
    ET.SubElement(f, "size").text = str(entry.size)
    ET.SubElement(f, "hash", {"type": "sha-256"}).text = entry.sha256
    if entry.merkle_root:
        ET.SubElement(f, "hash",
                      {"type": MERKLE_ROOT_TYPE}).text = entry.merkle_root
    if entry.merkle_url:
        ET.SubElement(f, "metaurl",
                      {"mediatype": METAURL_TYPE,
                       "priority": "1"}).text = entry.merkle_url
    for i, u in enumerate((entry.url, *entry.mirrors), start=1):
        ET.SubElement(f, "url", {"priority": str(i)}).text = u
    return f


def meta4(entries, published: str = "") -> str:
    """RFC 5854 belgesi. Oge ve oznitelik sirasi sabittir (bayt-kararli)."""
    root = ET.Element("metalink", {"xmlns": NS})
    if published:
        ET.SubElement(root, "published").text = published
    for entry in entries:
        root.append(_file_el(entry))
    ET.indent(root, space="  ")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="unicode") + "\n")


def publish(directory, base_url: str, mirrors=(), pattern: str | None = None,
            merkle: bool = True, force: bool = False) -> dict:
    """`directory` icindeki yayin dosyalari icin yan dosyalari uretir.

    Donen sozluk: {"dir", "index", "files": [{"name", "size", "sha256",
    "merkle_path", "merkle_chunks", "merkle_root", "sha256_path",
    "meta4_path"}]}. Hata durumlari ValueError (kullanici girdisi) ya da
    OSError (dosya sistemi)."""
    root = Path(directory).expanduser()
    if not root.is_dir():
        raise ValueError(f"dizin yok: {directory}")
    base = _check_url(base_url, "base-url")
    mbase = tuple(_check_url(m, "mirror") for m in mirrors)

    entries: list[Entry] = []
    files: list[dict] = []
    published = 0.0
    for src in sorted((p for p in root.iterdir()
                       if _publishable(p, pattern)), key=lambda p: p.name):
        st = src.stat()
        name = src.name
        man = None
        if merkle:
            man, _ = _manifest(src, src.with_name(name + cdc.SUFFIX), force)
        sha = util.file_digest(src, "sha256")
        sha_path = src.with_name(name + SHA256_SUFFIX)
        meta_path = src.with_name(name + META4_SUFFIX)
        entry = Entry(
            name=name, size=st.st_size, sha256=sha,
            url=_url(base, name),
            mirrors=tuple(_url(m, name) for m in mbase),
            merkle_root=man.root if man else "",
            merkle_url=_url(base, name + cdc.SUFFIX) if man else "",
        )
        _write_if_changed(sha_path, f"{sha}  {name}\n")
        _write_if_changed(meta_path,
                          meta4([entry], _rfc3339(st.st_mtime)))
        entries.append(entry)
        files.append({
            "name": name, "size": st.st_size, "sha256": sha,
            "merkle_path": str(src.with_name(name + cdc.SUFFIX))
                           if man else "",
            "merkle_chunks": len(man.chunks) if man else 0,
            "merkle_root": man.root if man else "",
            "sha256_path": str(sha_path),
            "meta4_path": str(meta_path),
        })
        published = max(published, st.st_mtime)

    if not entries:
        raise ValueError("yayinlanacak dosya yok"
                         + (f" (desen: {pattern})" if pattern else ""))
    index = root / INDEX_NAME
    _write_if_changed(index, meta4(entries, _rfc3339(published)))
    return {"dir": str(root), "index": str(index), "files": files}
