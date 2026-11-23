"""Metalink / .meta4 (RFC 5854/6249) ayristirici.

Amac: tek dosyaya ait URL listesi + saglama degerini JobSpec'e cevirmek.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET


def parse(text: str) -> list[dict]:
    """[{filename, size, urls, sha256, md5}] dondurur."""
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
        for c in f.iter():
            t = tag(c)
            if t == "url":
                u = (c.text or "").strip()
                if u.startswith(("http://", "https://")):
                    # tercih sirasi: en dusuk preference once
                    urls.append((int(c.get("preference", "99999") or 99999), u))
            elif t == "hash":
                val = (c.text or "").strip().lower()
                got = _norm({(c.get("type") or "").lower(): val})
                hashes.update(got)
        urls = [u for _, u in sorted(urls, key=lambda p: p[0])]
        if not urls:
            continue
        files.append({"filename": name, "size": size, "urls": urls,
                      "sha256": hashes.get("sha256", ""),
                      "md5": hashes.get("md5", ""),
                      "hashes": hashes})
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
