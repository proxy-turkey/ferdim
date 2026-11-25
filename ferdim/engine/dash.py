"""DASH (MPD) VOD indirici: her Period'un en iyi video temsilini sirayla
birlestirir, ek olarak en iyi ses temsilini KARDES dosyaya indirir (mux
yok; ffmpeg gerekmez). Assembler HLS ile aynidir.

Kapsam: on-demand/statik MPD - SegmentTemplate ($Number$ ya da
SegmentTimeline) veya SegmentList. Canli (dynamic) MPD desteklenmez.
"""
from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

from . import segments

MAX_SEGMENTS = 20_000            # tek MPD icin segment URL ust siniri
                                 # (~5.5 saat 1 sn'lik segment; ustu patolojik)

# ses temsili icin dosya uzantisi (mux yok: ses kardes dosyada kalir)
_AUDIO_MIME_EXT = (
    ("audio/mp4", ".m4a"), ("audio/webm", ".webm"),
    ("audio/mpeg", ".mp3"), ("audio/aac", ".aac"),
    ("audio/ogg", ".ogg"), ("audio/opus", ".opus"),
)
_AUDIO_EXTS = {e for _, e in _AUDIO_MIME_EXT}


class DashError(segments.StreamError):
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


def fetch_mpd(url: str, opt) -> str:
    """Sinirli boyutta MPD metni (manifest_too_large korumasi)."""
    return segments.fetch_manifest(url, opt)


def _mime(ad, rep) -> str:
    return (_attr(ad, "mimeType") or _attr(rep, "mimeType")
            or _attr(ad, "contentType") or _attr(rep, "contentType")
            or "").lower()


def _kind(ad, rep) -> str:
    """AdaptationSet/Representation turu: 'video', 'audio' ya da '' (bilinmez).

    Bilinmeyen tur geriye uyum icin video sayilir."""
    s = _mime(ad, rep)
    if "video" in s:
        return "video"
    if "audio" in s:
        return "audio"
    codecs = (_attr(rep, "codecs") or _attr(ad, "codecs") or "").lower()
    if any(c in codecs for c in ("avc", "hvc", "hev", "vp8", "vp9", "av01",
                                 "dvh")):
        return "video"
    if any(c in codecs for c in ("mp4a", "opus", "ac-3", "ec-3", "vorbis",
                                 "flac")):
        return "audio"
    return ""


def _score(kind: str, rep):
    if kind == "audio":
        return (int(_attr(rep, "bandwidth", 0) or 0),)
    return (int(_attr(rep, "width", 0) or 0),
            int(_attr(rep, "height", 0) or 0),
            int(_attr(rep, "bandwidth", 0) or 0))


def _pick(period, kind: str):
    """Bu Period'daki en iyi `kind` temsili (ad, rep) ya da (None, None)."""
    best = None
    for ad in _els(period, "AdaptationSet"):
        for rep in _els(ad, "Representation"):
            k = _kind(ad, rep)
            if k == "":
                if kind != "video":      # bilinmeyen: yalniz video geri donusu
                    continue
            elif k != kind:
                continue
            sc = _score(kind, rep)
            if best is None or sc > best[0]:
                best = (sc, ad, rep)
    return (best[1], best[2]) if best is not None else (None, None)


def _seconds(text) -> float | None:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", text or "")
    if not m:
        return None
    return (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
            + float(m.group(3) or 0))


def _audio_ext(ad, rep, init_url, seg_urls) -> str:
    mime = _mime(ad, rep)
    for prefix, ext in _AUDIO_MIME_EXT:
        if mime.startswith(prefix):
            return ext
    codecs = (_attr(rep, "codecs") or _attr(ad, "codecs") or "").lower()
    if "vorbis" in codecs or "opus" in codecs:
        return ".ogg"
    for u in [init_url] + list(seg_urls):
        if not u:
            continue
        path = urllib.parse.urlsplit(u).path
        suffix = ("." + path.rsplit(".", 1)[1].lower()) if "." in path else ""
        if suffix in _AUDIO_EXTS:
            return suffix
    return ".m4a"


def _rep_segments(root, base_url: str, period, ad, rep):
    """Secilen temsil icin (init_url, [seg_url]) dondurur."""
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
        rep_id = _attr(rep, "id", "")
        init_url = u(init_attr.replace("$RepresentationID$",
                                       rep_id)) if init_attr else None

        timeline = _el(tmpl, "SegmentTimeline")
        seg_urls: list[str] = []
        if timeline is not None:
            number = start
            for s in _els(timeline, "S"):
                # S@r sinirsiz tekrar edebilir: biriktirirken sinirla
                n = int(_attr(s, "r", "0") or 0) + 1
                if len(seg_urls) + n > MAX_SEGMENTS:
                    raise DashError("segment sayisi ust sinirin uzerinde")
                t = _attr(s, "t") or ""
                for _ in range(n):
                    media_n = media.replace("$RepresentationID$", rep_id)
                    media_n = media_n.replace("$Number$", str(number))
                    # re.sub degil: t yer tutucu sablonu degil, duz metin
                    media_n = media_n.replace("$Time$", t)
                    seg_urls.append(u(media_n))
                    number += 1
        else:
            duration = int(_attr(tmpl, "duration", "0") or 0)
            timescale = int(_attr(tmpl, "timescale", "1") or 1)
            total_dur = _seconds(_attr(period, "duration"))
            if total_dur is None and len(_els(root, "Period")) <= 1:
                # tek Period: MPD toplam suresi de kabul edilir; coklu
                # Period'da bu deger tum MPD'yi kapsar, kullanilmaz
                total_dur = _seconds(_attr(root, "mediaPresentationDuration"))
            if not duration or total_dur is None:
                raise DashError("SegmentTemplate duration/Period suresi yok")
            try:
                count = max(1, int(total_dur * timescale / duration))
            except OverflowError:
                raise DashError("segment sayisi ust sinirin uzerinde")
            if count > MAX_SEGMENTS:
                raise DashError("segment sayisi ust sinirin uzerinde")
            for i in range(count):
                media_n = media.replace("$RepresentationID$", rep_id)
                media_n = media_n.replace("$Number$", str(start + i))
                seg_urls.append(u(media_n))
        return init_url, seg_urls

    if seg_list is not None:
        init = _el(seg_list, "Initialization")
        init_url = u(_attr(init, "sourceURL")) if init is not None else None
        items = _els(seg_list, "SegmentURL")
        if not items:
            raise DashError("SegmentList bos")
        # SegmentList de ayni sinirlidir: liste dogrudan MPD'den gelir
        if len(items) > MAX_SEGMENTS:
            raise DashError("segment sayisi ust sinirin uzerinde")
        return init_url, [u(_attr(s, "media")) for s in items]

    raise DashError("SegmentTemplate/SegmentList bulunamadi")


def build_plan(mpd_text: str, mpd_url: str) -> dict:
    """Statik MPD'den indirme plani: video (tum Period'lar sirayla) + ses.

    Video: her Period'un en iyi temsili sirayla eklenir; ilk Period'un
    initialization'i basa yazilir, sonraki Period'larin initialization'i
    kendi segmentlerinin onune girer (tek dosyada dogru birlesim).

    Ses: en iyi ses temsili ayri bir kardes dosyaya iner (mux yok); farkli
    Period'larda ses varsa onlar da sirayla eklenir. `ext` dosya uzantisidir.
    """
    try:
        root = ET.fromstring(mpd_text)
    except ET.ParseError as e:
        raise DashError(f"MPD ayristirilamadi: {e}")
    if _attr(root, "type", "static") == "dynamic":
        raise DashError("canli (dynamic) MPD desteklenmiyor")

    base = _el(root, "BaseURL")
    base_url = urllib.parse.urljoin(
        mpd_url, (base.text or "").strip() if base is not None else mpd_url)
    periods = _els(root, "Period")
    if not periods:                 # cocuksuz eleman 'or' ile falsy olur
        periods = [root]

    video_init = None
    video_segs: list[str] = []
    video_found = False
    audio = None
    for period in periods:
        ad, rep = _pick(period, "video")
        if rep is not None:
            video_found = True
            init, segs = _rep_segments(root, base_url, period, ad, rep)
            if video_init is None and not video_segs:
                video_init = init
            elif init is not None:
                segs.insert(0, init)     # yeni Period'un initialization'i
            video_segs.extend(segs)
            if len(video_segs) > MAX_SEGMENTS:
                raise DashError("segment sayisi ust sinirin uzerinde")

        ad, rep = _pick(period, "audio")
        if rep is not None:
            init, segs = _rep_segments(root, base_url, period, ad, rep)
            if audio is None:
                audio = {"init_url": init, "segments": [],
                         "ext": _audio_ext(ad, rep, init, segs)}
            elif init is not None:
                segs.insert(0, init)
            audio["segments"].extend(segs)
            if len(audio["segments"]) > MAX_SEGMENTS:
                raise DashError("segment sayisi ust sinirin uzerinde")

    if not video_found:
        raise DashError("MPD icinde temsil (Representation) yok")
    if audio is not None and not audio["segments"]:
        audio = None                # ses temsili segment uretmedi: indirme yok
    return {"video": {"init_url": video_init, "segments": video_segs},
            "audio": audio}


def build_segment_list(mpd_text: str, mpd_url: str):
    """(init_url, [video seg_urls]) - eski imza (tek Period yolu korunur)."""
    plan = build_plan(mpd_text, mpd_url)
    video = plan["video"]
    return video["init_url"], video["segments"]
