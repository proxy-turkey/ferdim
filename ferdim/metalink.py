"""Metalink / .meta4 (RFC 5854/6249) ayristirici.

Amac: tek dosyaya ait URL listesi + saglama degerini JobSpec'e cevirmek.

FerDim eklentisi (standart disi, yalniz bilinmeyen alan olarak tasinir):
`<metaurl mediatype="application/x-ferdim-merkle">URL</metaurl>` parca
bildirimini, `<hash type="ferdim-merkle-root">` ise beklenen Merkle kokunu
bildirir. Ikisi de istege baglidir; baska araclar bu alanlari yok sayar.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

MERKLE_MEDIA = "application/x-ferdim-merkle"
MERKLE_ROOT = "ferdim-merkle-root"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _media_type(value) -> str:
    """`application/x-ferdim-merkle; charset=utf-8` -> medya tipi."""
    return str(value or "").split(";", 1)[0].strip().lower()


def _http_url(value) -> str:
    u = str(value or "").strip()
    return u if u.startswith(("http://", "https://")) else ""


def _merkle_root(value) -> str:
    """Beyan edilen Merkle koku; gecersizse yok sayilir (bozuk sha256
    degeri gibi: dogrulama yine parca ozetleriyle yapilir)."""
    v = str(value or "").strip().lower()
    return v if _HEX64.match(v) else ""


def parse(text: str) -> list[dict]:
    """[{filename, size, urls, sha256, md5, merkle, merkle_root}] dondurur."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"metalink ayristirilamadi: {e}") from e

    def tag(el):
        t = el.tag
        if isinstance(t, str) and t.startswith("{"):
            return t.split("}", 1)[1]
        return t

    def find_all(parent, name):
        return [c for c in parent.iter() if tag(c) == name]

    files = []
    for f in root.iter():
        if tag(f) != "file":
            continue
        name = f.get("name") or "download.bin"
        size = 0
        size_el = [c for c in f if tag(c) == "size"]
        if size_el and (size_el[0].text or "").strip().isdigit():
            size = int(size_el[0].text.strip())
        urls = []
        hashes: dict[str, str] = {}     # algoritma -> ozet (desteklenenler)
        merkle = ""
        merkle_root = ""
        for c in f.iter():
            t = tag(c)
            if t == "url":
                u = (c.text or "").strip()
                if u.startswith(("http://", "https://")):
                    # tercih sirasi: en dusuk preference once
                    urls.append((int(c.get("preference", "99999") or 99999), u))
            elif t == "metaurl":
                # FerDim: parca bildirimi (mediatype ile secilir)
                if not merkle and \
                        _media_type(c.get("mediatype")) == MERKLE_MEDIA:
                    merkle = _http_url(c.text)
            elif t == "hash":
                if (c.get("type") or "").strip().lower() == MERKLE_ROOT:
                    merkle_root = merkle_root or _merkle_root(c.text)
                    continue
                val = (c.text or "").strip().lower()
                got = _norm({(c.get("type") or "").lower(): val})
                hashes.update(got)
        urls = [u for _, u in sorted(urls, key=lambda p: p[0])]
        if not urls:
            continue
        files.append({"filename": name, "size": size, "urls": urls,
                      "sha256": hashes.get("sha256", ""),
                      "md5": hashes.get("md5", ""),
                      "hashes": hashes,
                      "merkle": merkle, "merkle_root": merkle_root})
    if not files:
        raise ValueError("metalink icinde dosya/url bulunamadi")
    return files


def _norm(d: dict) -> dict:
    from .util import normalize_checksums
    return normalize_checksums(d)


def looks_like_metalink(url: str, content_type: str = "") -> bool:
    u = (url or "").lower().split("?", 1)[0]
    return u.endswith((".metalink", ".meta4")) or \
        "metalink" in (content_type or "").lower()
