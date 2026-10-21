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

    def take(self, n: int) -> None:
        if self._rate <= 0 or n <= 0:
            return
        while True:
            with self._cond:
                now = time.monotonic()
                burst = max(self._rate, 1 << 17)  # en az 128KB burst
                self._tokens = min(burst, self._tokens + (now - self._ts) * self._rate)
                self._ts = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                wait = (n - self._tokens) / self._rate
            time.sleep(min(wait, 0.25))
