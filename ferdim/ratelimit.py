"""Token kovasi ile hiz sinirlama (global + is basina)."""
from __future__ import annotations

import threading
import time


class TokenBucket:
    """rate<=0 iken hic bekletmez (sinirsiz)."""

    def __init__(self, rate: float = 0):
        self._rate = float(rate or 0)
        self._tokens = self._rate
        self._ts = time.monotonic()
        self._cond = threading.Condition()

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float) -> None:
        with self._cond:
            self._rate = float(rate or 0)
            self._tokens = min(self._tokens, max(self._rate, 1 << 20))
            self._cond.notify_all()

    def take(self, n: int, stop: "threading.Event | None" = None) -> bool:
        """n baytlik yer ayirir. Hiz limiti altinda yer yoksa bekler.
        stop verilirse bekleme sirasinda durdurma sinyalini gormezden
        gelmez: sinyal gelince bayt harcamadan False doner (cagiran,
        elindeki parcayi yazmadan araligi kuyruga iade eder). Boylece hiz
        limitli bir is durdurulmak istendiginde isciler kovada
        dakikalarca beklemez."""
        if self._rate <= 0 or n <= 0:
            return True
        step = 0.1 if stop is not None else 0.25
        while True:
            if stop is not None and stop.is_set():
                return False
            with self._cond:
                now = time.monotonic()
                burst = max(self._rate, 1 << 17)  # en az 128KB burst
                self._tokens = min(burst, self._tokens + (now - self._ts) * self._rate)
                self._ts = now
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                wait = (n - self._tokens) / self._rate
            time.sleep(min(wait, step))


class CombinedBucket:
    """Birden cok kovayi BIRLIKTE uygular (kuyruk geneli + is basina).

    `take(n)` her kovadan yer ister: en yavas kova akisi belirler. Is
    basina `--limit` kuyruk geneli limitle celismez, iki sinir da gecerlidir.
    `rate` bilesenlerin en buyugudur (0 = hicbir sinir yok); durdurma
    semantigi (`take(n, stop)` bayt harcamadan False) korunur.
    """

    def __init__(self, *buckets):
        self._buckets = [b for b in buckets if b is not None]

    @property
    def rate(self) -> float:
        return max((b.rate for b in self._buckets), default=0.0)

    def set_rate(self, rate: float) -> None:
        # Tek kova gibi davran: mevcut cagiranlar (testler/araclar) tum
        # bilesenleri ayarlayabilsin.
        for b in self._buckets:
            b.set_rate(rate)

    def take(self, n: int, stop: "threading.Event | None" = None) -> bool:
        if n <= 0:
            return True
        for b in self._buckets:
            if b.rate > 0 and not b.take(n, stop):
                return False
        return True
