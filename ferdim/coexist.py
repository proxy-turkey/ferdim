"""Birlikte yasam modu (2.0): hat mesgulken baglanti birakir.

Varsayilan KAPALI: azami guc davranisi degismez; bu yalnizca nazik
paylasimi akillandiran istege bagli bir secenektir.

Hat ~1 sn'de bir orneklenir (/proc/net/dev toplami), FerDim'in kendi
hizi dusulur; kalan yabanci (etkilesimli) trafik gozlenen hat
kapasitesine oranlanir. Oran esigi belirgin sure asarsa 'active' olur,
daha uzun bir sessizlikten sonra birakilir (histerezis: baglanti sayisi
titremez).

Saat ve okuyucu enjekte edilebilir; testler uyumaz (FakeTime tarzi).
"""
from __future__ import annotations

import threading
import time

# Yerel/sanal arayuzler hattin gercek trafigini gostermez: loopback,
# konteyner kopruleri, tuneller, VPN'ler haric tutulur.
SKIP_EXACT = ("lo",)
SKIP_PREFIX = ("docker", "veth", "virbr", "vnet", "br-", "tun", "tap",
               "wg", "zt", "dummy", "ifb")

DEFAULT_THRESHOLD = 0.15
DEFAULT_INTERVAL = 1.0
DEFAULT_ENTER = 3.0
DEFAULT_LEAVE = 10.0


def is_line_iface(name: str) -> bool:
    """Arayuz hattin kendisini mi temsil ediyor? (sanal olanlar hayir)"""
    return name not in SKIP_EXACT and not name.startswith(SKIP_PREFIX)


def parse_net_dev(text: str) -> int:
    """/proc/net/dev metninden hat arayuzlerinin toplam rx+tx bayti.

    Baslik satirlari ve sanal arayuzler atlanir; bozuk satir sayilmaz.
    """
    total = 0
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        if not is_line_iface(name.strip()):
            continue
        cols = rest.split()
        if len(cols) < 9:
            continue
        try:
            total += int(cols[0]) + int(cols[8])   # rx bayt, tx bayt
        except ValueError:
            continue
    return total


def read_net_dev() -> int:
    """Canli sayac; okunamazsa -1 (izleyici fail-open davranir)."""
    try:
        with open("/proc/net/dev", "r", encoding="utf-8",
                  errors="replace") as f:
            return parse_net_dev(f.read())
    except OSError:
        return -1


class LineMonitor:
    """Yabanci trafik basinci: 0..1 arasi seviye + histerezisli 'active'.

    Kapasite, gozlenen hizlarin yavas azalan tepesidir: bos hatta esik
    anlamsizlasmaz, hat tavani bir kez gorulunce hatirlanir.
    """

    def __init__(self, read=None, clock=None, threshold=DEFAULT_THRESHOLD,
                 interval=DEFAULT_INTERVAL, enter_secs=DEFAULT_ENTER,
                 leave_secs=DEFAULT_LEAVE):
        self._read = read or read_net_dev
        self.clock = clock or time.monotonic
        self.threshold = float(threshold)
        self.interval = float(interval)
        self.enter_secs = float(enter_secs)
        self.leave_secs = float(leave_secs)
        # kuyruk tick'i ve API ayni izleyiciyi cagirabilir
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Durumu sifirlar (mod kapandi/acildi): gecmis basinc tasinmaz."""
        with self._lock:
            self.pressure = 0.0
            self.active = False
            self.capacity = 0.0       # bayt/sn: gozlenen hat kapasitesi
            self.foreign = 0.0        # bayt/sn: bizim disimizdaki trafik
            self._last = None         # son okunan sayac
            self._last_t = None
            self._hot_since = None
            self._quiet_since = None

    def sample(self, own_speed: float = 0.0) -> bool:
        """Bir ornek alir; guncel 'active' cevabini dondurur."""
        with self._lock:
            return self._sample_locked(own_speed)

    def _sample_locked(self, own_speed: float) -> bool:
        now = self.clock()
        total = self._read()
        if total < 0:             # /proc yok: fail-open (azami guc)
            return self.active
        if self._last is None:
            self._last, self._last_t = total, now
            return self.active
        dt = now - self._last_t
        if dt < self.interval:    # ornek araligi dolmadi
            return self.active
        rate = max(0.0, (total - self._last) / dt)
        self._last, self._last_t = total, now
        self.foreign = max(0.0, rate - max(0.0, float(own_speed)))
        # kapasite: %2/sn azalan tepe; taban hat hizinin kendisi
        self.capacity = max(rate, self.capacity * (0.98 ** dt))
        self.pressure = (min(1.0, self.foreign / self.capacity)
                         if self.capacity > 0 else 0.0)
        hot = self.capacity > 0 \
            and self.foreign > self.threshold * self.capacity
        self._update_active(now, hot)
        return self.active

    def _update_active(self, now: float, hot: bool) -> None:
        if hot:
            self._quiet_since = None
            if self._hot_since is None:
                self._hot_since = now
            elif not self.active and now - self._hot_since >= self.enter_secs:
                self.active = True
        else:
            self._hot_since = None
            if not self.active:
                self._quiet_since = None
                return
            if self._quiet_since is None:
                self._quiet_since = now
            elif now - self._quiet_since >= self.leave_secs:
                self.active = False
                self._quiet_since = None
