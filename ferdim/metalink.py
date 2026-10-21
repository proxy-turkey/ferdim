"""Metalink / .meta4 (RFC 5854/6249) ayristirici.

Amaç: tek dosyaya ait URL listesi + saglama degerini JobSpec'e cevirmek.
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
        sha256 = md5 = ""
        for c in f.iter():
            t = tag(c)
            if t == "url":
                u = (c.text or "").strip()
                if u.startswith(("http://", "https://", "ftp://")):
                    # tercih sirasi: en dusuk preference once
                    urls.append((int(c.get("preference", "99999") or 99999), u))
            elif t == "hash":
                htype = (c.get("type") or "").lower().replace("-", "")
                val = (c.text or "").strip().lower()
                if htype == "sha256" and len(val) == 64:
                    sha256 = val
                elif htype == "md5" and len(val) == 32:
                    md5 = val
        urls = [u for _, u in sorted(urls)]
        if not urls:
            continue
        files.append({"filename": name, "size": size, "urls": urls,
                      "sha256": sha256, "md5": md5})
    if not files:
        raise ValueError("metalink icinde dosya/url bulunamadi")
    return files


def looks_like_metalink(url: str, content_type: str = "") -> bool:
    u = (url or "").lower().split("?", 1)[0]
    return u.endswith((".metalink", ".meta4")) or \
        "metalink" in (content_type or "").lower()
