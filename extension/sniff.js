/* FerDim yanit siniflandirici - saf fonksiyon, tarayici API'si yok.
 * background.js bunu yukler (manifest: background.scripts sirasi);
 * tests/test_extension_sniff.py node ile dogrudan cagirir.
 *
 * Girdi yalnizca URL + yanit basliklari. Siteye ozel kural YOKTUR ve
 * eklenmemelidir: ayni sinyaller her sunucuda gecerlidir.
 *
 *   classify(req, opts) -> null | {
 *     url, kind, role, segmentish, filename, size, mime }
 *     kind:      hls | dash | video | audio | archive | file
 *     role:      manifest | direct | segment
 *     segmentish: HLS/DASH parcasi gibi gorunen ad (manifest gelince atilir)
 */
"use strict";

const FerDimSniff = (() => {
  const HLS_TYPES = new Set([
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
  ]);
  const DASH_TYPES = new Set([
    "application/dash+xml",
    "video/vnd.mpeg.dash.mpd",
  ]);
  const ARCHIVE_TYPES = new Set([
    "application/zip", "application/x-zip-compressed",
    "application/x-iso9660-image", "application/x-tar",
    "application/gzip", "application/x-gzip", "application/x-compressed-tar",
    "application/x-7z-compressed", "application/x-rar-compressed",
    "application/vnd.rar", "application/x-bzip2", "application/x-xz",
    "application/zstd", "application/x-apple-diskimage",
  ]);
  const PAGE_TYPES = new Set([
    "text/html", "application/xhtml+xml", "application/json",
    "application/xml", "text/xml", "text/css", "text/javascript",
    "application/javascript",
  ]);
  /* istek filtresi: HTTP aralik yaniti + bu uzantilar => parca istegi */
  const SEGMENT_EXT = new Set([
    "ts", "m4s", "m4f", "cmfv", "cmfa", "cmft", "aac", "dash",
  ]);
  const VIDEO_EXT = new Set([
    "mp4", "mkv", "webm", "flv", "avi", "mov", "m4v", "ts", "3gp",
    "ogv", "mpeg", "mpg", "m2ts", "wmv",
  ]);
  const AUDIO_EXT = new Set([
    "mp3", "m4a", "flac", "ogg", "opus", "wav", "aac", "wma", "mka", "oga",
  ]);
  const ARCHIVE_EXT = new Set([
    "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "iso", "img",
    "dmg", "cab", "lz4",
  ]);
  /* indirme sinyali sayilan icerik turleri */
  const GENERIC_TYPES = new Set([
    "", "application/octet-stream", "binary/octet-stream",
    "application/download", "application/force-download",
    "application/unknown",
  ]);

  function header(headers, name) {
    const list = headers || [];
    for (const h of list) {
      if (!h) continue;
      if (String(h.name || "").toLowerCase() === name) {
        return typeof h.value === "string" ? h.value : "";
      }
    }
    return "";
  }

  function baseName(url) {
    try {
      const seg = new URL(url).pathname.split("/").filter(Boolean).pop() || "";
      return decodeURIComponent(seg);
    } catch { return ""; }
  }

  function extOf(name) {
    const m = /\.([a-z0-9]{1,8})$/i.exec(String(name || "").split("?")[0]);
    return m ? m[1].toLowerCase() : "";
  }

  /* Content-Disposition: filename / filename* (RFC 5987) + attachment */
  function disposition(cd) {
    const raw = String(cd || "");
    const out = { attachment: /^\s*attachment\b/i.test(raw), filename: "" };
    if (!raw) return out;
    const star = /filename\*\s*=\s*([^;]+)/i.exec(raw);
    if (star) {
      const val = star[1].trim().replace(/^"|"$/g, "");
      const parts = val.split("'");
      let name = parts.length >= 3 ? parts.slice(2).join("'") : val;
      try { name = decodeURIComponent(name); } catch { /* ham birak */ }
      out.filename = name;
    } else {
      const plain = /filename\s*=\s*("([^"]*)"|[^;]+)/i.exec(raw);
      if (plain) {
        out.filename = (plain[2] !== undefined ? plain[2] : plain[1]).trim();
      }
    }
    out.filename = String(out.filename || "").split(/[\\/]/).pop() || "";
    return out;
  }

  function rangeTotal(cr) {
    const m = /\/\s*(\d+)\s*$/.exec(String(cr || ""));
    return m ? parseInt(m[1], 10) : 0;
  }

  function kindFor(mime, ext) {
    if (HLS_TYPES.has(mime)) return "hls";
    if (DASH_TYPES.has(mime)) return "dash";
    if (ARCHIVE_TYPES.has(mime)) return "archive";
    if (mime.startsWith("video/")) return "video";
    if (mime.startsWith("audio/")) return "audio";
    if (ext === "m3u8") return "hls";
    if (ext === "mpd") return "dash";
    if (ARCHIVE_EXT.has(ext)) return "archive";
    if (VIDEO_EXT.has(ext)) return "video";
    if (AUDIO_EXT.has(ext)) return "audio";
    return "";
  }

  function classify(req, opts) {
    const r = req || {};
    const url = typeof r.url === "string" ? r.url : "";
    if (!/^https?:\/\//i.test(url)) return null;
    const headers = r.headers || [];
    const mime = (header(headers, "content-type") || "")
      .split(";")[0].trim().toLowerCase();

    const cd = disposition(header(headers, "content-disposition"));
    const name = cd.filename || baseName(url);
    if (!name) return null;
    const ext = extOf(name);

    let kind = kindFor(mime, ext);
    // sayfa gorseli/HTML asla medya degil; yalnizca acik manifest uzantisi
    // (x.m3u8 / x.mpd) bu kuralin disinda kalir
    const page = PAGE_TYPES.has(mime) || mime.startsWith("image/");
    if (page && kind !== "hls" && kind !== "dash") return null;
    if (!kind) {
      // indirme sinyali: acik ek (attachment) ya da ikili tur + dosya adi
      const signal = (cd.attachment && cd.filename) ||
                     (GENERIC_TYPES.has(mime) && cd.filename);
      if (!signal) return null;
      kind = ARCHIVE_EXT.has(ext) ? "archive" : "file";
    }

    const partial = r.status === 206 ||
      header(headers, "content-range") !== "";
    const segmentish = SEGMENT_EXT.has(ext);
    let role;
    if (kind === "hls" || kind === "dash") role = "manifest";
    else if (segmentish && partial) role = "segment";
    else role = "direct";

    const total = rangeTotal(header(headers, "content-range"));
    const length = parseInt(header(headers, "content-length") || "", 10);
    const size = total || (Number.isFinite(length) ? length : 0);

    // esik yalnizca video/dosya/arsiv icin; ses ve manifest her zaman gecer
    const o = opts || {};
    const minBytes = Math.max(0, Number(o.minBytes) || 0);
    const capped = kind === "video" || kind === "archive" || kind === "file";
    if (capped && minBytes && size && size < minBytes) return null;

    return {
      url,
      kind,
      role,
      segmentish: segmentish && role !== "manifest",
      filename: name,
      size: size || 0,
      mime: mime || "application/octet-stream",
    };
  }

  return { classify };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = FerDimSniff;
}
