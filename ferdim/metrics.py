"""Prometheus metin bicimi (0.0.4): /metrics ucu ve 'ferdim metrics'.

Sayaclar kuyruk tick'inde orneklenir: her is icin son gorulen bayt ve
sayac degerleri tutulur, aradaki fark toplama eklenir. Boylece is
kuyruktan silinse de toplam bayt/tekrar sayaci geri sarmaz (Prometheus
sayaclari monotonik olmali). Kartesite sinirlidir: en cok MAX_HOSTS ana
makine adiyla gorunur, geri kalan 'other' etiketi altinda toplanir;
katlama ornekleme aninda yapilir, sozlukler zamanla buyumez.
Ana makine anahtari profiles.host_key'dir: kimlik bilgisi ve yol yok.
"""
from __future__ import annotations

import threading

from . import __version__
from .profiles import host_key

MAX_HOSTS = 200
OTHER = "other"
TERMINAL = ("done", "error", "canceled")
STATES = ("queued", "connecting", "downloading", "paused", "done",
          "error", "canceled")


def _label(value) -> str:
    """Etiket degeri: ters bolu, cift tirnak ve satir sonu kacirilir."""
    return (str(value).replace("\\", "\\\\").replace("\n", "\\n")
            .replace('"', '\\"'))


def _num(value) -> str:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    return str(int(value)) if value.is_integer() else f"{value:.3f}"


class Collector:
    """Kuyruk ornekleyicisi: sayaclari biriktirir, metin uretir."""

    def __init__(self):
        self._lock = threading.Lock()
        self._done = {}          # is -> son orneklenen bayt
        self._counters = {}      # is -> son orneklenen {tur: {host: n}}
        self._states = {}        # is -> son gorulen durum
        self._bytes = {}         # host -> bayt (toplam)
        self._retries = {}       # host -> tekrar denemesi
        self._throttles = {}     # host -> 429/503
        self._jobs_total = {}    # terminal durum -> sayi
        self._queue_jobs = {}    # durum -> guncel is sayisi
        self._links = 0
        self._speed = 0.0
        self._coexist = 0

    # ------------------------------------------------------------ ornekleme
    def observe(self, jobs, coexist_active: bool = False, states=None) -> None:
        """Kuyruk tick'i: is nesnelerini okur, deltalari toplama ekler.

        `states` verilirse (kuyruk yolu) yalniz `jobs` orneklenir; durum
        sayaclari (Prometheus `ferdim_queue_jobs`) hazir sozlukten alinir ve
        kuyruktan cikan isler `forget()` ile dusurulur. Boylece 10 000
        duraklatilmis is her tick'te kilit/sozluk kopyasi odemez. `states`
        verilmezse (dogrudan kullanim) sayim `jobs`tan turetilir.

        Tek bir isin okunamasi (kilit/ozellik hatasi) tick'in tamamini
        dusurmez: is atlanir, digerleri sayilir."""
        with self._lock:
            if states is None:
                live: set[str] | None = set()
                counts: dict[str, int] = {}
            else:
                live = None
                counts = dict(states)
            links, speed = 0, 0.0
            for job in list(jobs):
                try:
                    jid = str(getattr(job, "id", "") or "")
                    if not jid:
                        continue
                    if live is not None:
                        live.add(jid)
                    state = str(getattr(job, "state", "") or "")
                    if live is not None:
                        counts[state] = counts.get(state, 0) + 1
                    if state in TERMINAL and self._states.get(jid) not in TERMINAL:
                        self._jobs_total[state] = \
                            self._jobs_total.get(state, 0) + 1
                    self._states[jid] = state
                    links += max(0, int(getattr(job, "_active", 0) or 0))
                    speed += max(0.0, float(getattr(job, "speed", 0.0) or 0.0))
                    host = str(getattr(job, "_host", "") or
                               host_key(getattr(job, "url", "")) or "?")
                    done = max(0, int(getattr(job, "done", 0) or 0))
                    prev = self._done.get(jid, 0)
                    if done < prev:
                        # yeniden baslatma / merkle onarimi: taban duser,
                        # negatif delta uretilmez; sonraki baytlar sayilir
                        prev = done
                    self._done[jid] = done
                    if done > prev:
                        self._add(self._bytes, host, done - prev)
                    self._sample_counters(jid, self._snapshot_counters(job))
                except Exception:
                    continue
            if live is not None:
                for jid in set(self._done) - live:      # kuyruktan cikti
                    self._forget_locked(jid)
            self._bound()
            self._queue_jobs = counts
            self._links = links
            self._speed = speed
            self._coexist = 1 if coexist_active else 0

    def forget(self, job_id: str) -> None:
        """Kuyruktan cikan isin ornekleme tabanini siler (O(1)).

        Toplamlar (bayt/tekrar) SILINMEZ: Prometheus sayaclari monotonik
        kalmali; is kuyruktan ciksa da toplam geri sarmaz."""
        with self._lock:
            self._forget_locked(str(job_id))

    def _forget_locked(self, jid: str) -> None:
        """Is basina tabanlari dusurur (kilit cagiranda)."""
        self._done.pop(jid, None)
        self._counters.pop(jid, None)
        self._states.pop(jid, None)

    @staticmethod
    def _snapshot_counters(job):
        """job.stats_counters'i is kilidi altinda kopyalar: isciler ayni
        sozlugu degistirirken gezinmek RuntimeError firlatabilir."""
        raw = getattr(job, "stats_counters", None)
        if not isinstance(raw, dict):
            return None
        lock = getattr(job, "_lock", None)
        try:
            if lock is not None:
                with lock:
                    return {k: dict(v) for k, v in raw.items()
                            if isinstance(v, dict)}
            return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            return None

    @staticmethod
    def _add(target: dict, host: str, n: int) -> None:
        target[host] = target.get(host, 0) + int(n)

    def _bound(self) -> None:
        """Kardesilik siniri ornekleme aninda uygulanir (kilit cagiranda).

        Uc ana makine sozlugu birlikte MAX_HOSTS ile sinirlidir: en cok
        baytli olanlar tutulur, kalanlar 'other' satirina katlanir. Boylece
        sozlukler zamanla degil, kuyrugun o anki ana makine sayisiyla
        buyur. Siralama bayt + ad ile belirlenimcidir."""
        hosts = set(self._bytes) | set(self._retries) | set(self._throttles)
        hosts.discard(OTHER)
        if len(hosts) <= MAX_HOSTS:
            return
        ranked = sorted(hosts, key=lambda h: (-self._bytes.get(h, 0), h))
        for host in ranked[MAX_HOSTS:]:
            for target in (self._bytes, self._retries, self._throttles):
                n = target.pop(host, 0)
                if n:
                    self._add(target, OTHER, n)

    def _sample_counters(self, jid: str, counters) -> None:
        """Is sayaclarindaki deltayi toplama ekler (kilit cagiranda)."""
        if not isinstance(counters, dict):
            return
        seen = self._counters.setdefault(jid, {})
        for kind, target in (("retries", self._retries),
                             ("throttles", self._throttles)):
            cur = counters.get(kind)
            if not isinstance(cur, dict):
                continue
            old = seen.get(kind) or {}
            for host, n in dict(cur).items():
                try:
                    delta = int(n) - int(old.get(host, 0))
                except (TypeError, ValueError):
                    continue
                if delta > 0:
                    self._add(target, str(host or "?"), delta)
            seen[kind] = dict(cur)

    # --------------------------------------------------------------- metin
    def render(self) -> str:
        with self._lock:
            by_bytes = dict(self._bytes)
            retries = dict(self._retries)
            throttles = dict(self._throttles)
            jobs_total = dict(self._jobs_total)
            queue_jobs = dict(self._queue_jobs)
            links, speed, coexist = self._links, self._speed, self._coexist
        def rows(values: dict):
            # Katlama ornekleme aninda yapildi (Collector._bound); burada
            # yalniz belirlenimci sira: once bayt, sonra ad.
            return sorted(((h, int(n)) for h, n in values.items()),
                          key=lambda r: (-r[1], r[0]))

        out = ["# HELP ferdim_bytes_total Bytes downloaded, by host.",
               "# TYPE ferdim_bytes_total counter"]
        for host, n in rows(by_bytes):
            out.append(f'ferdim_bytes_total{{host="{_label(host)}"}} {_num(n)}')
        out += ["# HELP ferdim_jobs_total Jobs that reached a terminal state.",
                "# TYPE ferdim_jobs_total counter"]
        for state in TERMINAL:
            if state in jobs_total:
                out.append(f'ferdim_jobs_total{{state="{state}"}} '
                           f'{_num(jobs_total[state])}')
        out += ["# HELP ferdim_active_links Live connections across all jobs.",
                "# TYPE ferdim_active_links gauge",
                f"ferdim_active_links {_num(links)}",
                "# HELP ferdim_retries_total Failed attempts retried, by host.",
                "# TYPE ferdim_retries_total counter"]
        for host, n in rows(retries):
            out.append(f'ferdim_retries_total{{host="{_label(host)}"}} '
                       f'{_num(n)}')
        out += ["# HELP ferdim_throttles_total HTTP 429/503 throttle "
                "responses, by host.",
                "# TYPE ferdim_throttles_total counter"]
        for host, n in rows(throttles):
            out.append(f'ferdim_throttles_total{{host="{_label(host)}"}} '
                       f'{_num(n)}')
        out += ["# HELP ferdim_speed_bytes Aggregate throughput in bytes "
                "per second.",
                "# TYPE ferdim_speed_bytes gauge",
                f'ferdim_speed_bytes{{scope="aggregate"}} {_num(speed)}',
                "# HELP ferdim_queue_jobs Current jobs by state.",
                "# TYPE ferdim_queue_jobs gauge"]
        for state in STATES:
            if state in queue_jobs:
                out.append(f'ferdim_queue_jobs{{state="{state}"}} '
                           f'{_num(queue_jobs[state])}')
        out += ["# HELP ferdim_coexist_active Coexistence pressure active "
                "(1) or not (0).",
                "# TYPE ferdim_coexist_active gauge",
                f"ferdim_coexist_active {coexist}",
                "# HELP ferdim_build_info Build information.",
                "# TYPE ferdim_build_info gauge",
                f'ferdim_build_info{{version="{_label(__version__)}"}} 1']
        return "\n".join(out) + "\n"


def render(qm) -> str:
    """Ucun okudugu metin: kuyrugun ornekleyicisi (yoksa bos govde)."""
    col = getattr(qm, "metrics", None)
    return col.render() if isinstance(col, Collector) else Collector().render()
