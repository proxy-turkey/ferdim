"""DASH (MPD) VOD indirici: en iyi temsili secer, init + segmentleri
paralel cekip sirayla birlestirir (HLS ile ayni assembler).

Kapsam: on-demand/statik MPD — SegmentTemplate ($Number$ ya da
SegmentTimeline) veya SegmentList. Canli (dynamic) MPD desteklenmez.
"""
from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

from . import httpcli


class DashError(Exception):
    pass


def is_dash(url: str, content_type: str) -> bool:
    u = (url or "").lower().split("?", 1)[0]
    return u.endswith(".mpd") or "dash+xml" in (content_type or "").lower()


def _ns_split(tag: str):
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _el(parent, name):
    for child in parent:
        if _ns_split(child.tag) == name:
            return child
    return None


def _els(parent, name):
    return [c for c in parent if _ns_split(c.tag) == name]


def _attr(el, name, default=None):
    return el.get(name, default) if el is not None else default


def fetch_mpd(url: str, opt: httpcli.DlOptions) -> str:
    opener = httpcli._make_opener(opt)
    with opener.open(httpcli.request_of(url, opt), timeout=opt.timeout) as r:
        return r.read().decode("utf-8", "replace")


def build_segment_list(mpd_text: str, mpd_url: str):
    """(init_url, [seg_urls], temsil bilgisi) dondurur; video tercih edilir."""
    try:
        root = ET.fromstring(mpd_text)
    except ET.ParseError as e:
        raise DashError(f"MPD ayristirilamadi: {e}")
    if _attr(root, "type", "static") == "dynamic":
        raise DashError("canli (dynamic) MPD desteklenmiyor")

    base = _el(root, "BaseURL")
    base_url = urllib.parse.urljoin(
        mpd_url, (base.text or "").strip() if base is not None else mpd_url)

    best = None  # (score, adaptation, rep)
    period = _el(root, "Period") or root
    for ad in _els(period, "AdaptationSet") or [period]:
        mime = (_attr(ad, "mimeType") or _attr(ad, "contentType") or "").lower()
        for rep in _els(ad, "Representation"):
            if best is None and mime not in ("video", "") and "video" not in mime:
                continue
            score = (int(_attr(rep, "width", 0) or 0),
                     int(_attr(rep, "height", 0) or 0),
                     int(_attr(rep, "bandwidth", 0) or 0))
            if best is None or score > best[0]:
                best = (score, ad, rep)
    if best is None:
        raise DashError("MPD icinde temsil (Representation) yok")
    _, ad, rep = best

    # Dikkat: ElementTree'de cocuksuz eleman 'or' ile falsy olur; her zaman
    # 'is not None' kullan
    tmpl = _el(rep, "SegmentTemplate")
    if tmpl is None:
        tmpl = _el(ad, "SegmentTemplate")
    seg_list = _el(rep, "SegmentList")
    if seg_list is None:
        seg_list = _el(ad, "SegmentList")

    def u(x):
        return urllib.parse.urljoin(base_url, x)

    if tmpl is not None:
        media = _attr(tmpl, "media") or ""
        init_attr = _attr(tmpl, "initialization") or ""
        start = int(_attr(tmpl, "startNumber", "1") or 1)
        init_url = u(init_attr.replace("$RepresentationID$",
                                      _attr(rep, "id", ""))) if init_attr else None

        timeline = _el(tmpl, "SegmentTimeline")
        seg_urls: list[str] = []
        if timeline is not None:
            number = start
            for s in _els(timeline, "S"):
                t = _attr(s, "t")
                n = int(_attr(s, "r", "0") or 0) + 1
                for _ in range(n):
                    media_n = media.replace("$RepresentationID$",
                                            _attr(rep, "id", ""))
                    media_n = media_n.replace("$Number$", str(number))
                    media_n = re.sub(r"\$Time\$", t or "", media_n)
                    seg_urls.append(u(media_n))
                    number += 1
        else:
            duration = int(_attr(tmpl, "duration", "0") or 0)
            timescale = int(_attr(tmpl, "timescale", "1") or 1)
            total_dur = None
            for p in _els(root, "Period"):
                d = _attr(p, "duration")
                if d:
                    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", d)
                    if m:
                        total_dur = (int(m.group(1) or 0) * 3600
                                     + int(m.group(2) or 0) * 60
                                     + float(m.group(3) or 0))
                        break
            if not duration or total_dur is None:
                raise DashError("SegmentTemplate duration/Period suresi yok")
            count = max(1, int(total_dur * timescale / duration))
            for i in range(count):
                media_n = media.replace("$RepresentationID$",
                                        _attr(rep, "id", ""))
                media_n = media_n.replace("$Number$", str(start + i))
                seg_urls.append(u(media_n))
        return init_url, seg_urls

    if seg_list is not None:
        init = _el(seg_list, "Initialization")
        init_url = u(_attr(init, "sourceURL")) if init is not None else None
        seg_urls = [u(_attr(s, "media")) for s in _els(seg_list, "SegmentURL")]
        if not seg_urls:
            raise DashError("SegmentList bos")
        return init_url, seg_urls

    raise DashError("SegmentTemplate/SegmentList bulunamadi")
