"""Dosya adi, kategori ve bicimlendirme yardimcilari."""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

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


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKC", name).strip()
    name = name.replace("\x00", "")
    # Bolum ayracilari ve dosya sistemlerine aykiri karakterleri temizle
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:200] or "download.bin"


def filename_from_url(url: str) -> str:
    path = unquote(urlsplit(url).path)
    base = os.path.basename(path)
    return sanitize_filename(base) if base else "download.bin"


def parse_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", value)
    if m:
        return sanitize_filename(unquote(m.group(1).strip('" ')))
    m = re.search(r'filename="?([^";]+)"?', value)
    if m:
        return sanitize_filename(unquote(m.group(1).strip()))
    return None


def unique_path(directory: Path, name: str) -> Path:
    p = directory / name
    if not p.exists():
        return p
    stem, dot, ext = name.partition(".")
    if not dot:
        stem, ext = name, ""
    i = 1
    while True:
        cand = directory / f"{stem} ({i}){('.' + ext) if ext else ''}"
        if not cand.exists():
            return cand
        i += 1


def atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), "utf-8")
    tmp.replace(path)


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


def parse_size(text: str | None):
    """'2MB', '512kb', '1048576' -> bayt. None/0 donen deger sinirsizdir."""
    if not text:
        return 0
    s = str(text).strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(b|kb|k|mb|m|gb|g|kib|mib|gib)?", s)
    if not m:
        try:
            return int(float(s))
        except ValueError:
            return 0
    val = float(m.group(1))
    mult = {"b": 1, "kb": 1000, "k": 1000, "mb": 1000**2, "m": 1000**2,
            "gb": 1000**3, "g": 1000**3, "kib": 1024, "mib": 1024**2,
            "gib": 1024**3}.get(m.group(2) or "b", 1)
    return int(val * mult)


def now_ms() -> int:
    return int(time.time() * 1000)
