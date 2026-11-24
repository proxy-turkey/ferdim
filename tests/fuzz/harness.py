"""Deterministik mutasyon fuzzer'i (yalniz stdlib).

Tasarim notlari:
  * Ayni `seed` ayni girdi dizisini uretir; mutasyon sayaci disinda
    rastgelelik yok. CI'da gorulen yeni bir hata sinifi boylece
    `--seed N` ile tekrar uretilebilir.
  * Girdi basina sure siniri `signal.setitimer` ile uygulanir; sinir
    asilirsa `FuzzTimeout` yukselir. `FuzzTimeout` bilerek `Exception`
    degil `BaseException` altidir: parser'in genis `except Exception`
    bloklari (ya da `except OSError`) sure sinirini yutamaz.
  * Beklenen istisnalar hedefe ozel bir tuple ile verilir (module ozel
    hata siniflari, ValueError, BadZipFile...). Bunun disindaki her sey
    bulgudur: IndexError, KeyError, TypeError, RecursionError, zlib.error,
    re.error, asiri bellek, sure asimi.
  * Sinif kimligi `target:exc_id` seklindedir (traceback metni degil);
    `known.json` bu kimlikleri tasir, boylece CI yalnizca YENI sinifta
    kirmizi olur.

Bilinen sinirlar:
  * Ana is parcacigi disinda `setitimer` kurulmaz; orada yalnizca
    sonradan gecen sure olculur (asili kalan girdi kesilemez). Testler
    ve CLI ana is parcaciginda kosar.
  * Bellek olcumu `tracemalloc` ile istege baglidir (`--mem-mb`); kapali
    oldugunda yalniz girdi boyu ve girdi basina sure siniri korur.
"""
from __future__ import annotations

import contextlib
import json
import random
import re
import signal
import shutil
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

KNOWN_PATH = Path(__file__).with_name("known.json")
MAX_INPUT = 64 * 1024          # mutasyon sonrasi girdi ust siniri
DEFAULT_TIMEOUT = 2.0          # girdi basina saniye
REPORT_SAMPLE = 512            # rapora yazilan azami girdi bayti


class FuzzTimeout(BaseException):
    """Girdi basina sure siniri asildi (parser yakalayamaz)."""


# --------------------------------------------------------------- istisna adi
def exc_id(exc: BaseException) -> str:
    """Istisna sinifi icin surumden bagimsiz kisa kimlik."""
    if isinstance(exc, re.error):
        # Python 3.13 're.error' -> re.PatternError olarak yeniden adlandirdi
        return "re.error"
    cls = type(exc)
    if cls.__module__ in ("builtins", "__main__"):
        return cls.__name__
    return f"{cls.__module__}.{cls.__name__}"


def _canonical_name(exc: BaseException) -> str:
    if isinstance(exc, re.error):
        return "re.error"
    cls = type(exc)
    return f"{cls.__module__}.{cls.__qualname__}"


# -------------------------------------------------------------------- sinir
def _alarm_available() -> bool:
    return (hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")
            and threading.current_thread() is threading.main_thread())


def _raise_timeout(signum, frame):  # pragma: no cover - sinyal yolu
    raise FuzzTimeout("girdi basina sure siniri asildi")


@contextlib.contextmanager
def time_limit(seconds: float | None):
    """Girdi basina sure siniri. Asimda FuzzTimeout yukseltir."""
    if not seconds or seconds <= 0:
        yield
        return
    if not _alarm_available():
        # Yedek yol: kesme yok, yalnizca sonradan olcum
        started = time.monotonic()
        yield
        elapsed = time.monotonic() - started
        if elapsed > seconds:
            raise FuzzTimeout(f"girdi {elapsed:.2f}s surdu (sinir {seconds:.2f}s)")
        return
    previous = signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# ------------------------------------------------------------------- hedef
@dataclass(frozen=True)
class Target:
    """Tek bir parser hedefi.

    `call(data, workdir)` fonksiyonu veriyi ayristirir; donen deger
    onemsizdir. `expected` tuple'i normal (yani bulgu olmayan) istisnalari
    listeler. `tokens` yapisal mutasyonlar icin ek parcalardir.
    `rewrites` yapisal (anlamsal) mutasyonlardir: (bayt regex, aday
    degerler) ciftleri; eslesen bir alanin degeri rastgele biriyle
    degistirilir. Boylece XML/JSON alanlari bozulmadan uc degerler
    denenir (or. `t`, `duration`, `preference`, `size`).
    """
    name: str
    seeds: tuple[bytes, ...]
    call: Callable[[bytes, Path], object]
    expected: tuple[type, ...]
    tokens: tuple[bytes, ...] = ()
    rewrites: tuple[tuple[bytes, tuple[bytes, ...]], ...] = ()
    note: str = ""


# ----------------------------------------------------------------- mutator
_INTERESTING_BYTES = bytes(
    b"\x00\x01\x09\x0a\x0d\x1f\x20\x22\x26\x27\x2c\x2d\x2e\x2f\x3a\x3b"
    b"\x3c\x3d\x3e\x3f\x5b\x5c\x5d\x7b\x7c\x7d\x7f\xf0\xff")


def _rand_bytes(rng: random.Random, n: int) -> bytes:
    return bytes(rng.randrange(256) for _ in range(n))


class Mutator:
    """Bayt ve yapisal mutasyonlar (bit flip, ekle/sil, dilim, kopyala)."""

    def __init__(self, rng: random.Random, tokens: Iterable[bytes],
                 rewrites: Iterable[tuple[bytes, Iterable[bytes]]] = ()):
        self.rng = rng
        self.tokens = tuple(tokens)
        self.rewrites = tuple((re.compile(p), tuple(v)) for p, v in rewrites)

    # -- dis arayuz
    def seed_mutation(self, seeds: tuple[bytes, ...]) -> bytes:
        """Tohumdan 1-3 mutasyonla yeni girdi.

        Bazen yalnizca yapisal (rewrite) mutasyon uygulanir: boylece
        belge butun kalir ve parser'in derin yollari da denenir. Aksi
        halde rastgele bayt bozulmasi XML/JSON'u ilk adimda dusurur.
        """
        buf = bytearray(self.rng.choice(seeds))
        if self.rewrites and self.rng.random() < 0.4:
            for _ in range(self.rng.randint(1, 2)):
                self._rewrite(buf)
            return _cap(buf)
        for _ in range(self.rng.randint(1, 3)):
            self._one(buf, seeds)
        return _cap(buf)

    def mutate(self, data: bytes, seeds: tuple[bytes, ...] = ()) -> bytes:
        """Onceki girdiden 1-5 mutasyonla yeni girdi."""
        buf = bytearray(data)
        if self.rewrites and self.rng.random() < 0.25:
            for _ in range(self.rng.randint(1, 3)):
                self._rewrite(buf)
            return _cap(buf)
        for _ in range(self.rng.randint(1, 5)):
            self._one(buf, seeds)
        return _cap(buf)

    # -- tek mutasyon
    def _one(self, buf: bytearray, seeds: tuple[bytes, ...]) -> None:
        op = self.rng.choices(
            ("flip", "set", "insert", "delete", "dup", "splice",
             "repeat", "truncate", "token", "rewrite"),
            weights=(18, 10, 14, 12, 7, 9, 5, 3, 9, 13))[0]
        n = len(buf)
        if op == "flip" and n:
            pos = self.rng.randrange(n)
            buf[pos] ^= 1 << self.rng.randrange(8)
        elif op == "set" and n:
            pos = self.rng.randrange(n)
            buf[pos] = (self.rng.randrange(256) if self.rng.random() < 0.5
                        else _INTERESTING_BYTES[self.rng.randrange(
                            len(_INTERESTING_BYTES))])
        elif op == "insert":
            pos = self.rng.randrange(n + 1)
            buf[pos:pos] = self._chunk(seeds)
        elif op == "delete" and n:
            pos = self.rng.randrange(n)
            buf[pos:pos + self.rng.randint(1, max(1, n // 4))] = b""
        elif op == "dup" and n:
            pos = self.rng.randrange(n)
            ln = self.rng.randint(1, max(1, n // 8))
            buf[pos:pos] = bytes(buf[pos:pos + ln])
        elif op == "splice" and seeds and n:
            other = self.rng.choice(seeds)
            if other:
                a = self.rng.randrange(len(other))
                b = self.rng.randrange(a, min(len(other), a + 4096) + 1)
                pos = self.rng.randrange(n)
                buf[pos:pos] = other[a:b]
        elif op == "repeat" and n:
            pos = self.rng.randrange(n)
            ln = min(max(1, n // 16), 4096)
            times = self.rng.randint(2, 40)
            buf[pos:pos] = bytes(buf[pos:pos + ln]) * times
        elif op == "truncate" and n:
            del buf[self.rng.randrange(n):]
        elif op == "token" and self.tokens:
            pos = self.rng.randrange(n + 1)
            buf[pos:pos] = self.rng.choice(self.tokens)
        elif op == "rewrite":
            self._rewrite(buf)

    def _rewrite(self, buf: bytearray) -> None:
        """Eslesen alanlardan birinin degerini degistirir (yapisal)."""
        order = list(range(len(self.rewrites)))
        self.rng.shuffle(order)
        for idx in order:
            pattern, values = self.rewrites[idx]
            if not values:
                continue
            matches = list(pattern.finditer(bytes(buf)))
            if not matches:
                continue
            match = self.rng.choice(matches)
            buf[match.start():match.end()] = self.rng.choice(values)
            return

    def _chunk(self, seeds: tuple[bytes, ...]) -> bytes:
        r = self.rng.random()
        if r < 0.35 and self.tokens:
            return self.rng.choice(self.tokens)
        if r < 0.60 and seeds:
            other = self.rng.choice(seeds)
            if other:
                a = self.rng.randrange(len(other))
                return other[a:a + self.rng.randint(1, 64)]
        return _rand_bytes(self.rng, self.rng.randint(1, 32))


def _cap(buf: bytearray) -> bytes:
    return bytes(buf[:MAX_INPUT])


# ------------------------------------------------------------------ bulgu
@dataclass
class Finding:
    target: str
    kind: str                      # exc_id ya da "Timeout"/"Memory"
    count: int
    sample: bytes
    detail: str
    frames: tuple[str, ...] = ()
    first_iter: int = 0

    @property
    def key(self) -> str:
        return f"{self.target}:{self.kind}"

    def hex_sample(self) -> str:
        return self.sample[:REPORT_SAMPLE].hex()

    def report(self) -> str:
        lines = [f"{self.key}  ({self.count} isabet, ilk isabet girdi "
                 f"#{self.first_iter}, ornek {len(self.sample)}B)",
                 f"  ayrinti: {self.detail}"]
        if self.frames:
            lines.append("  iz (en ic cerceve sonda):")
            lines.extend(f"    {f}" for f in self.frames)
        hexed = self.hex_sample()
        if hexed:
            shown = min(len(self.sample), REPORT_SAMPLE)
            lines.append(f"  girdi(hex, ilk {shown}B): {hexed}")
        else:
            lines.append("  girdi(hex): <bos>")
        return "\n".join(lines)


@dataclass
class TargetResult:
    target: str
    inputs: int
    elapsed: float
    findings: dict[str, Finding] = field(default_factory=dict)

    def new(self, known: dict[str, str]) -> list[Finding]:
        return [f for f in self.findings.values() if f.key not in known]

    def known_hits(self, known: dict[str, str]) -> list[Finding]:
        return [f for f in self.findings.values() if f.key in known]


# --------------------------------------------------------------- minimize
def _same_failure(target: Target, data: bytes, workdir: Path,
                  expected: tuple[type, ...], kind: str,
                  timeout: float | None) -> bool:
    try:
        with time_limit(timeout):
            target.call(data, workdir)
    except expected:
        return False
    except FuzzTimeout:
        return False
    except Exception as exc:  # noqa: BLE001 - her istisna siniflandirilir
        return exc_id(exc) == kind
    return False


def minimize(target: Target, data: bytes, workdir: Path,
             expected: tuple[type, ...], kind: str,
             timeout: float | None = None, budget: int = 400) -> bytes:
    """Hata sinifini koruyan kucuk girdi (ddmin-lite)."""
    if kind in ("Timeout", "Memory") or not data:
        return data
    best = data
    steps = 0
    n = max(1, len(best) // 2)
    while n >= 1 and steps < budget:
        i = 0
        shrunk = False
        while i < len(best) and steps < budget:
            cand = best[:i] + best[i + n:]
            steps += 1
            if cand and _same_failure(target, cand, workdir, expected, kind,
                                      timeout):
                best = cand
                shrunk = True
            else:
                i += n
        if not shrunk:
            n //= 2
    return best


# ------------------------------------------------------------------- kosu
def _short_path(filename: str) -> str:
    """Depo icindeki yolu goreli, stdlib yolunu son iki bilesenle yazar."""
    repo = Path(__file__).resolve().parents[2]
    try:
        return str(Path(filename).resolve().relative_to(repo))
    except ValueError:
        return "/".join(Path(filename).parts[-2:])


def _describe(exc: BaseException) -> tuple[str, tuple[str, ...]]:
    top = traceback.extract_tb(exc.__traceback__)
    frames = []
    for fr in top[-6:]:
        frames.append(f"{_short_path(fr.filename)}:{fr.lineno} in {fr.name}")
    where = f" @ {frames[-1]}" if frames else ""
    detail = f"{_canonical_name(exc)}: {exc}{where}"
    return detail, tuple(frames)


def run_target(target: Target, *, seed: int = 1,
               seconds: float | None = None,
               iterations: int | None = None,
               timeout: float | None = DEFAULT_TIMEOUT,
               memory_limit: int | None = None,
               workdir: Path | None = None,
               minimize_findings: bool = True) -> TargetResult:
    """Hedefi `seconds` ya da `iterations` kadar kosar; bulgulari doner."""
    rng = random.Random(seed)
    mutator = Mutator(rng, target.tokens, target.rewrites)
    deadline = (time.monotonic() + seconds) if seconds else None
    tempdir = None
    if workdir is None:
        tempdir = Path(tempfile.mkdtemp(prefix=f"ferdim-fuzz-{target.name}-"))
        workdir = tempdir
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    tracing = False
    result = TargetResult(target.name, 0, 0.0)
    started = time.monotonic()
    data = b""
    try:
        for _ in range(iterations if iterations is not None else 1 << 30):
            if deadline is not None and time.monotonic() >= deadline:
                break
            result.inputs += 1
            # Bilinen bir asilma sinifi bulunduktan sonra tekrarlar daha
            # kisa sinirla kosar: ayni asilma butceyi yemesin.
            attempt_timeout = timeout
            if timeout and "Timeout" in result.findings:
                attempt_timeout = min(timeout, 0.5)
            if not data or rng.random() < 0.3:
                data = mutator.seed_mutation(target.seeds)
            else:
                data = mutator.mutate(data, target.seeds)
            # bellek siniri: yalnizca istege bagli (tracemalloc yavaslatir)
            mem = None
            if memory_limit:
                import tracemalloc
                if not tracemalloc.is_tracing():
                    tracemalloc.start()
                    tracing = True
                tracemalloc.reset_peak()
                mem = tracemalloc.get_traced_memory()[0]
            kind = detail = None
            frames: tuple[str, ...] = ()
            try:
                with time_limit(attempt_timeout):
                    target.call(data, workdir)
            except target.expected:
                continue
            except FuzzTimeout as exc:
                kind, detail = "Timeout", f"FuzzTimeout: {exc}"
            except Exception as exc:  # noqa: BLE001
                kind = exc_id(exc)
                detail, frames = _describe(exc)
            else:
                if mem is not None:
                    peak = tracemalloc.get_traced_memory()[1]
                    if peak - mem > memory_limit:
                        kind = "Memory"
                        detail = (f"bellek artisi ~{(peak - mem) >> 20} MiB "
                                  f"(sinir {memory_limit >> 20} MiB)")
            if kind is None:
                continue
            if kind in result.findings:
                result.findings[kind].count += 1
            else:
                sample = data
                if minimize_findings:
                    sample = minimize(target, data, workdir, target.expected,
                                      kind, attempt_timeout)
                result.findings[kind] = Finding(target.name, kind, 1, sample,
                                                detail, frames,
                                                result.inputs)
                data = b""          # bulgudan sonra temiz tohumla devam
    finally:
        result.elapsed = time.monotonic() - started
        if tracing:
            import tracemalloc
            tracemalloc.stop()
        if tempdir is not None:
            shutil.rmtree(tempdir, ignore_errors=True)
    return result


# ------------------------------------------------------------------ known
def load_known(path: Path | str | None = KNOWN_PATH) -> dict[str, str]:
    """`known.json`: {"known": [{"id": "target:sinif", "note": ...}]}."""
    if not path or not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = data.get("known", []) if isinstance(data, dict) else data
    known: dict[str, str] = {}
    for entry in entries:
        ident = entry.get("id") if isinstance(entry, dict) else str(entry)
        if not isinstance(ident, str) or ":" not in ident:
            raise ValueError(f"gecersiz known kaydi: {entry!r}")
        known[ident] = str(entry.get("note", "")) if isinstance(entry, dict) else ""
    return known


def known_suggestion(finding: Finding) -> dict:
    """known.json'a eklenecek kayit (mesaj metni yerine sinif kimligi)."""
    entry = {"id": finding.key,
             "note": finding.detail.split(" @ ")[0][:200],
             "first_iter": finding.first_iter}
    if len(finding.sample) <= 2048:     # kucuk ve yeniden uretilebilir ornek
        entry["sample_hex"] = finding.sample.hex()
    return entry
