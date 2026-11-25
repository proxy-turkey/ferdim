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
        dakikalarca beklemez.

        Burst tavani n'yi de kapsar: 256 KB'lik bir okuma, 128 KB/sn'lik
        kovada bile (yavas yavas biriktirilip) karsilanabilir. Aksi halde
        n > burst olan istek hicbir zaman saglanamaz ve is sonsuza kadar
        beklerdi."""
        if self._rate <= 0 or n <= 0:
            return True
        step = 0.1 if stop is not None else 0.25
        while True:
            if stop is not None and stop.is_set():
                return False
            with self._cond:
                now = time.monotonic()
                burst = max(self._rate, 1 << 17, n)  # tek istek hep sigsin
                self._tokens = min(burst, self._tokens + (now - self._ts) * self._rate)
                self._ts = now
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                wait = (n - self._tokens) / self._rate
            time.sleep(min(wait, step))

    def refund(self, n: int) -> None:
        """take() ile alinan n bayti kovaya geri verir (bilesik kova iptali)."""
        if self._rate <= 0 or n <= 0:
            return
        with self._cond:
            burst = max(self._rate, 1 << 17, n)
            self._tokens = min(burst, self._tokens + n)


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
        # Yalniz KUYRUK GENELI (ilk) bilesen: is basina `--limit` bu
        # cagriyla ezilmez. Kuyruk zaten paylasilan TokenBucket'i dogrudan
        # ayarlar; bu yalniz eski cagiranlar/testler icin bir kolayliktir.
        if self._buckets:
            self._buckets[0].set_rate(rate)

    def take(self, n: int, stop: "threading.Event | None" = None) -> bool:
        """Tum kovalardan yer ister. Once EN YAVAS (buyuk olasilikla
        bloklayan) kova alinir; boylece durdurma ikinci kovada gelirse
        harcanmis token sizmaz. Yine de sizinti olursa alinanlar iade
        edilir."""
        if n <= 0:
            return True
        if stop is not None and stop.is_set():
            return False
        ordered = sorted((b for b in self._buckets if b.rate > 0),
                         key=lambda b: b.rate)
        taken = []
        for b in ordered:
            if not b.take(n, stop):
                for t in taken:
                    t.refund(n)
                return False
            taken.append(b)
        return True
