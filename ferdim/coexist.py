"""Birlikte yasam modu (2.0): hat mesgulken baglanti birakir.

Varsayilan KAPALI: azami guc davranisi degismez; bu yalnizca nazik
paylasimi akillandiran istege bagli bir secenektir.

Hat ~1 sn'de bir orneklenir (/proc/net/dev toplami), FerDim'in kendi
hizi dusulur; kalan yabanci (etkilesimli) trafik gozlenen hat
kapasitesine oranlanir. Oran esigi belirgin sure asarsa 'active' olur,
daha uzun bir sessizlikten sonra birakilir (histerezis: baglanti sayisi
titremez).

Ikinci sinyal GECIKME: kendi baglantilarimizin yanit suresi (TTFB) ana
makine basina izlenir; guncel EMA tabanin (gozlenen en iyi deger)
`latency_factor` kati kadar ustundeyse hat doymadan da basinc sayilir.
Yabanci trafik ve gecikme AYNI histerezis pencerelerini paylasir.

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
DEFAULT_LATENCY = 2.5     # TTFB sismesi esigi: guncel EMA / taban


def _include_set(include) -> frozenset:
    if isinstance(include, str):
        include = (include,)
    return frozenset(n for n in (include or ()) if n)


def is_line_iface(name: str, include=()) -> bool:
    """Arayuz hattin kendisini mi temsil ediyor? (sanal olanlar hayir)

    `include` verilen adlar (varsayilan rotayi tasiyan wg*/tun* ya da
    kullanici listesi) atlama kuralini EZER: VPN uzerinden giden hattin
    gercek trafigi de izlenir."""
    if name in _include_set(include):
        return True
    return name not in SKIP_EXACT and not name.startswith(SKIP_PREFIX)


def parse_net_dev(text: str, include=()) -> int:
    """/proc/net/dev metninden hat arayuzlerinin toplam rx+tx bayti.

    Baslik satirlari ve sanal arayuzler atlanir; bozuk satir sayilmaz.
    `include` DOLUYSA yalniz o adlar sayilir (VPN/tunnel dahil); bos ise
    eski davranis (sanal olanlari atla) gecerlidir."""
    total = 0
    only = _include_set(include)
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if only and name not in only:
            continue
        if not only and not is_line_iface(name):
            continue
        cols = rest.split()
        if len(cols) < 9:
            continue
        try:
            total += int(cols[0]) + int(cols[8])   # rx bayt, tx bayt
        except ValueError:
            continue
    return total


def parse_route(text: str) -> str | None:
    """/proc/net/route: varsayilan rotayi (00000000) tasiyan arayuz.

    En dusuk metrik kazanir; varsayilan rota yoksa None."""
    best = None
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8:
            continue
        iface, dest, _gw, _flags, _ref, _use, metric, _mask = cols[:8]
        if dest.lower() != "00000000":
            continue
        try:
            m = int(metric, 16)
        except ValueError:
            continue
        if best is None or m < best[0]:
            best = (m, iface)
    return best[1] if best else None


def parse_route6(text: str) -> str | None:
    """/proc/net/ipv6_route: ::/0 varsayilan rotasini tasiyan arayuz.

    Satir bicimi: dest(32 hex) prefixlen dest_src src_plen gw metric ...
    iface. En dusuk metrik kazanir; varsayilan rota yoksa None."""
    best = None
    for line in text.splitlines():
        cols = line.split()
        if len(cols) < 10:
            continue
        dest, plen, metric = cols[0], cols[1], cols[5]
        try:
            if int(dest, 16) != 0 or int(plen, 16) != 0:
                continue
            m = int(metric, 16)
        except ValueError:
            continue
        if best is None or m < best[0]:
            best = (m, cols[-1])
    return best[1] if best else None


def default_route_interface(route_path: str = "/proc/net/route",
                            route6_path: str = "/proc/net/ipv6_route"
                            ) -> str | None:
    """Varsayilan rotayi tasiyan arayuz (IPv4 once, yoksa IPv6).

    Okunamayan dosya yok sayilir; ikisi de yoksa None (eski davranis)."""
    for path, parser in ((route_path, parse_route),
                         (route6_path, parse_route6)):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                iface = parser(f.read())
        except OSError:
            iface = None
        if iface:
            return iface
    return None


def resolve_interfaces(cfg=None, route_path: str = "/proc/net/route",
                       route6_path: str = "/proc/net/ipv6_route") -> tuple:
    """Izlenecek arayuzler: cfg['coexist_interfaces'] varsa o, yoksa
    varsayilan rotanin arayuzu, o da yoksa bos demet (eski davranis)."""
    raw = (cfg or {}).get("coexist_interfaces") if cfg else None
    if isinstance(raw, str):
        names = [n for n in raw.replace(",", " ").split() if n]
    elif isinstance(raw, (list, tuple)):
        names = [str(n).strip() for n in raw if str(n).strip()]
    else:
        names = []
    if names:
        return tuple(names)
    iface = default_route_interface(route_path, route6_path)
    return (iface,) if iface else ()


def read_net_dev(path: str = "/proc/net/dev", include=()) -> int:
    """Canli sayac; okunamazsa -1 (izleyici fail-open davranir)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return parse_net_dev(f.read(), include)
    except OSError:
        return -1


def read_net_dev_default(path: str = "/proc/net/dev",
                         route_path: str = "/proc/net/route",
                         route6_path: str = "/proc/net/ipv6_route") -> int:
    """Varsayilan rotanin arayuzunu (VPN/tunnel dahil) izleyen okuyucu.

    Varsayilan rota bulunamazsa eski davranisa (sanal arayuzleri atla)
    duser."""
    iface = default_route_interface(route_path, route6_path)
    return read_net_dev(path, (iface,) if iface else ())


class LatencySignal:
    """Kendi baglantilarimizin yanit suresi (TTFB): ana makine basina
    taban + guncel EMA.

    Taban "EMA-min"dir: gozlenen en iyi TTFB, her ornekte binde 2 sisme
    payi ile (tek bir asiri hizli ornek tabani kilitleyip kalici yanlis
    alarm uretmesin). Oran 1.0 = normal; 2.0 = yanitlar iki kat yavas.
    Ornek yoksa 1.0 (bilinmiyorsa basinc yok).

    Ana makine kayitlari ZAMAN DAMGALIDIR: kaynagi olmayan (olumlu ayna,
    terk edilmis baglanti) bir kayit `STALE` sn sonra dusurulur; yoksa tek
    bir yavas ayna `inflation()` degerini sonsuza kadar yuksek tutup
    baglanti sayisini kalici olarak yarilayabiliyordu. Kaynak olunce
    `drop()` ile de hemen silinir.
    """

    ALPHA = 0.3            # guncel EMA agirligi (yeni ornegin payi)
    DECAY = 1.002          # tabanin ornek basina yumusak sismesi
    WARM = 3               # taban/guncel icin en az ornek
    LIMIT = 3600.0         # bundan uzun olcum (saat sicramasi) sayilmaz
    STALE = 20.0           # sn: bu kadar ornek gelmeyen ana makine unutulur

    def __init__(self, now=None):
        self._lock = threading.Lock()
        self._now = now or time.monotonic
        self._hosts: dict[str, dict] = {}

    def reset(self) -> None:
        """Taban sifirlanir (kaynak degisti / is yeniden basladi)."""
        with self._lock:
            self._hosts.clear()

    def drop(self, host: str) -> None:
        """Ana makine kaydi hemen silinir (kaynagi oldu/terk edildi)."""
        with self._lock:
            self._hosts.pop(host or "", None)

    def _prune_locked(self, now: float) -> None:
        for key in [k for k, st in self._hosts.items()
                    if now - float(st.get("t", 0.0)) > self.STALE]:
            del self._hosts[key]

    def add(self, host: str, ttfb: float) -> None:
        """Bir yanit suresi ornegi ekler (gecersiz deger yok sayilir)."""
        try:
            ttfb = float(ttfb)
        except (TypeError, ValueError):
            return
        if not 0.0 < ttfb < self.LIMIT:
            return
        key = host or ""
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            st = self._hosts.get(key)
            if st is None:
                self._hosts[key] = {"n": 1, "base": ttfb, "ema": ttfb,
                                    "t": now}
                return
            st["n"] += 1
            st["ema"] += self.ALPHA * (ttfb - st["ema"])
            st["base"] = ttfb if ttfb < st["base"] \
                else min(ttfb, st["base"] * self.DECAY)
            st["t"] = now

    def inflation(self) -> float:
        """En kotu ana makinenin guncel/taban orani; bilinmiyorsa 1.0."""
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            worst = 1.0
            for st in self._hosts.values():
                if st["n"] < self.WARM or st["base"] <= 0:
                    continue
                worst = max(worst, st["ema"] / st["base"])
            return worst


class LineMonitor:
    """Yabanci trafik basinci: 0..1 arasi seviye + histerezisli 'active'.

    Kapasite, gozlenen hizlarin yavas azalan tepesidir: bos hatta esik
    anlamsizlasmaz, hat tavani bir kez gorulunce hatirlanir.
    """

    def __init__(self, read=None, clock=None, threshold=DEFAULT_THRESHOLD,
                 interval=DEFAULT_INTERVAL, enter_secs=DEFAULT_ENTER,
                 leave_secs=DEFAULT_LEAVE,
                 latency_factor=DEFAULT_LATENCY, interfaces=None):
        if read is not None:
            self._read = read
        elif interfaces is None:
            # Varsayilan rotanin arayuzu (VPN/tunnel dahil) izlenir.
            self._read = read_net_dev_default
        else:
            # cfg['coexist_interfaces'] gibi acik bir liste atlamayi ezer.
            getter = interfaces if callable(interfaces) else (lambda: interfaces)
            self._read = lambda: read_net_dev(include=getter())
        self.clock = clock or time.monotonic
        self.threshold = float(threshold)
        self.interval = float(interval)
        self.enter_secs = float(enter_secs)
        self.leave_secs = float(leave_secs)
        self.latency_factor = float(latency_factor)
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
            self.latency = 1.0        # guncel/taban TTFB orani (1 = normal)
            self._last = None         # son okunan sayac
            self._last_t = None
            self._hot_since = None
            self._quiet_since = None

    def sample(self, own_speed: float = 0.0, latency: float | None = None) -> bool:
        """Bir ornek alir; guncel 'active' cevabini dondurur.

        latency: kendi baglantilarimizin TTFB sismesi (1.0 = normal);
        verilmezse son deger korunur."""
        with self._lock:
            return self._sample_locked(own_speed, latency)

    def _sample_locked(self, own_speed: float,
                       latency: float | None = None) -> bool:
        now = self.clock()
        if latency is not None:
            try:
                self.latency = max(1.0, float(latency))
            except (TypeError, ValueError):
                pass
        total = self._read()
        if total < 0:             # /proc yok/okunamaz: fail-open (azami guc)
            self._fail_open()
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
        foreign_pressure = (min(1.0, self.foreign / self.capacity)
                            if self.capacity > 0 else 0.0)
        self.pressure = max(foreign_pressure, self._latency_pressure())
        hot = (self.capacity > 0
               and self.foreign > self.threshold * self.capacity) \
            or self._latency_hot()
        self._update_active(now, hot)
        return self.active

    def _latency_hot(self) -> bool:
        """TTFB esigi asti mi (kendi baglantilarimiz yavasladi)?"""
        return self.latency > max(1.01, float(self.latency_factor))

    def _latency_pressure(self) -> float:
        """Gecikme basinci 0..1: esikte 1.0 (yabanci trafik gibi raporlanir)."""
        if self.latency <= 1.0:
            return 0.0
        span = max(0.01, max(1.01, float(self.latency_factor)) - 1.0)
        return min(1.0, (self.latency - 1.0) / span)

    def _fail_open(self) -> None:
        """Sayac okunamazsa basinc BILINMIYOR demektir: tavan tutulmaz.

        Eski davranis 'active' degerini sonsuza kadar donduruyordu; hat
        bilgisi kaybolunca tavan da dusmeli, yoksa is kalici olarak
        yarim baglantiyla kalirdi. Sayac geri gelince taban yeniden
        kurulur (eski basinc tasinmaz).
        """
        self.active = False
        self.pressure = 0.0
        self.foreign = 0.0
        self.latency = 1.0
        self._last = None
        self._last_t = None
        self._hot_since = None
        self._quiet_since = None

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
