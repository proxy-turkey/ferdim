"""Cekirdegin TCP_INFO verisi: RTT ve yeniden iletim oranlari.

Linux `struct tcp_info` (uapi/linux/tcp.h) sabit ofsetlidir; `getsockopt`
eski cekirdeklerde daha kisa bir tampon dondurebilir, bu yuzden her alan
"tamponda varsa" okunur. Linux disinda (TCP_INFO yok) `read()` None doner.

Ofsetler (bayt) - tumu kucuk-endian:
  0 state, 1 ca_state, 2 retransmits, 3 probes, 4 backoff, 5 options,
  6 snd/rcv wscale, 7 delivery/fastopen bayraklari,
  8 rto_us, 12 ato_us, 16 snd_mss, 20 rcv_mss,
  24 unacked, 28 sacked, 32 lost, 36 retrans (bekleyen), 40 fackets,
  44 last_data_sent, 48 last_ack_sent, 52 last_data_recv, 56 last_ack_recv,
  60 pmtu, 64 rcv_ssthresh, 68 rtt_us, 72 rttvar_us,
  76 snd_ssthresh, 80 cwnd, 84 advmss, 88 reordering,
  92 rcv_rtt_us, 96 rcv_space, 100 total_retrans,
  104 pacing_bps, 108 max_pacing_bps, 112 bytes_acked (u64),
  120 bytes_received (u64), 128 segs_out, 132 segs_in, 136 notsent,
  140 min_rtt_us, 144 data_segs_in, 148 data_segs_out,
  152 delivery_rate_bps (u64), 160 busy_us (u64), 168 rwnd_limited_us (u64),
  176 sndbuf_limited_us (u64), 184 delivered, 188 delivered_ce,
  192 bytes_sent (u64), 200 bytes_retrans (u64), 208 dsack_dups,
  212 reord_seen, 216 rcv_ooopack, 220 snd_wnd, 224 rcv_wnd.

`CongestionSignal` ornekleri zamanla birlikte alir (saat ENJEKTE edilir):
governor testleri uyumadan, sahte orneklerle calisir. Sinyal yalnizca
governor'a EK bir tavan sinyali verir; hiz mantigi ve azami guc varsayilani
degismez.
"""
from __future__ import annotations

import socket
import struct
import sys
import threading

SIZE = 232                 # modern cekirdegin struct tcp_info boyu

# (ad, ofset, struct bicimi) - artan ofset sirasinda
_FIELDS = (
    ("state", 0, "<B"),
    ("ca_state", 1, "<B"),
    ("retransmits", 2, "<B"),
    ("probes", 3, "<B"),
    ("backoff", 4, "<B"),
    ("options", 5, "<B"),
    ("rto_us", 8, "<I"),
    ("ato_us", 12, "<I"),
    ("snd_mss", 16, "<I"),
    ("rcv_mss", 20, "<I"),
    ("unacked", 24, "<I"),
    ("sacked", 28, "<I"),
    ("lost", 32, "<I"),
    ("retrans", 36, "<I"),
    ("fackets", 40, "<I"),
    ("last_data_sent", 44, "<I"),
    ("last_ack_sent", 48, "<I"),
    ("last_data_recv", 52, "<I"),
    ("last_ack_recv", 56, "<I"),
    ("pmtu", 60, "<I"),
    ("rcv_ssthresh", 64, "<I"),
    ("rtt_us", 68, "<I"),
    ("rttvar_us", 72, "<I"),
    ("snd_ssthresh", 76, "<I"),
    ("cwnd", 80, "<I"),
    ("advmss", 84, "<I"),
    ("reordering", 88, "<I"),
    ("rcv_rtt_us", 92, "<I"),
    ("rcv_space", 96, "<I"),
    ("total_retrans", 100, "<I"),
    ("pacing_bps", 104, "<I"),
    ("max_pacing_bps", 108, "<I"),
    ("bytes_acked", 112, "<Q"),
    ("bytes_received", 120, "<Q"),
    ("segs_out", 128, "<I"),
    ("segs_in", 132, "<I"),
    ("notsent_bytes", 136, "<I"),
    ("min_rtt_us", 140, "<I"),
    ("data_segs_in", 144, "<I"),
    ("data_segs_out", 148, "<I"),
    ("delivery_rate_bps", 152, "<Q"),
    ("busy_us", 160, "<Q"),
    ("rwnd_limited_us", 168, "<Q"),
    ("sndbuf_limited_us", 176, "<Q"),
    ("delivered", 184, "<I"),
    ("delivered_ce", 188, "<I"),
    ("bytes_sent", 192, "<Q"),
    ("bytes_retrans", 200, "<Q"),
    ("dsack_dups", 208, "<I"),
    ("reord_seen", 212, "<I"),
    ("rcv_ooopack", 216, "<I"),
    ("snd_wnd", 220, "<I"),
    ("rcv_wnd", 224, "<I"),
)


def parse(raw: bytes) -> dict | None:
    """Ham `struct tcp_info` tamponunu cozer; kisa tampon sorun degil.

    Ne RTT ne de toplam yeniden iletim alani varsa None (okunacak veri yok).
    """
    if not raw:
        return None
    out: dict = {}
    for name, off, fmt in _FIELDS:
        if off + struct.calcsize(fmt) > len(raw):
            break
        out[name] = struct.unpack_from(fmt, raw, off)[0]
    if "rtt_us" not in out and "total_retrans" not in out:
        return None
    return out


def read(sock) -> dict | None:
    """Soketin TCP_INFO'sunu okur (Linux); desteklenmiyorsa None."""
    if sock is None or not sys.platform.startswith("linux"):
        return None
    opt = getattr(socket, "TCP_INFO", None)
    if opt is None:
        return None
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, opt, SIZE)
    except Exception:
        return None
    return parse(raw)


class CongestionSignal:
    """TCP_INFO orneklerinden tikaniklik sinyali (saf; saat enjekte).

    Link basina:
      * yeniden iletim orani: `bytes_retrans`/`bytes_sent` deltasi (yeni
        cekirdek) ya da `total_retrans`/`segs_out` deltasi (eski cekirdek);
        ikisi de yoksa yeniden iletim/sn,
      * RTT tabani (gozlenen en iyi, binde 2/sn yumusak sisme) ve EMA.

    Tikaniklik = yeniden iletim orani esigi asiyor VE RTT tabana gore
    belirgin sismis. Tek bir bozuk link yeterlidir (max).
    """

    WARM = 2                # RTT taban/EMA icin en az ornek
    ALPHA = 0.35            # guncel ornek agirligi
    DECAY = 1.002           # tabanin ornek basina yumusak sismesi
    STALE = 20.0            # sn: bu kadar ornek gelmeyen link unutulur
    RATIO_MIN = 0.02        # yeniden iletilen bayt/segment orani esigi
    RATE_MIN = 5.0          # oran hesaplanamayan cekirdeklerde retrans/sn
    RTT_FACTOR = 1.5        # guncel RTT / taban esigi

    def __init__(self):
        self._lock = threading.Lock()
        self._links: dict = {}

    # ------------------------------------------------------------- ornekleme
    def add(self, key, sample: dict, now: float) -> None:
        """Bir TCP_INFO ornegini isler. Gecersiz/eksik alanlar yok sayilir."""
        if not isinstance(sample, dict):
            return
        try:
            now = float(now)
        except (TypeError, ValueError):
            return
        rtt = sample.get("rtt_us")
        try:
            rtt_s = float(rtt) / 1e6 if rtt else 0.0
        except (TypeError, ValueError):
            rtt_s = 0.0
        with self._lock:
            self._prune_locked(now)
            e = self._links.get(key)
            if e is None:
                e = {"t": now, "n": 0, "ratio": None, "rate": 0.0,
                     "rtt_base": rtt_s, "rtt_ema": rtt_s,
                     "total_retrans": sample.get("total_retrans"),
                     "segs_out": sample.get("segs_out"),
                     "bytes_sent": sample.get("bytes_sent"),
                     "bytes_retrans": sample.get("bytes_retrans")}
                self._links[key] = e
                if rtt_s > 0:
                    e["n"] = 1
                return
            dt = now - e["t"]
            if dt > 0:
                self._update_rate_locked(e, sample, dt)
                e["t"] = now
            for field in ("total_retrans", "segs_out", "bytes_sent",
                          "bytes_retrans"):
                val = sample.get(field)
                if isinstance(val, int) and not isinstance(val, bool):
                    e[field] = val
            if rtt_s > 0:
                e["n"] += 1
                e["rtt_ema"] += self.ALPHA * (rtt_s - e["rtt_ema"])
                if e["rtt_base"] <= 0:
                    e["rtt_base"] = rtt_s
                elif rtt_s < e["rtt_base"]:
                    e["rtt_base"] = rtt_s
                else:
                    e["rtt_base"] = min(rtt_s, e["rtt_base"] * self.DECAY)

    def _update_rate_locked(self, e: dict, sample: dict, dt: float) -> None:
        """Iki ornek arasindaki yeniden iletim oranini/hizini gunceller."""
        d_retrans = _delta(sample.get("total_retrans"), e.get("total_retrans"))
        d_segs = _delta(sample.get("segs_out"), e.get("segs_out"))
        d_sent = _delta(sample.get("bytes_sent"), e.get("bytes_sent"))
        d_rbytes = _delta(sample.get("bytes_retrans"),
                          e.get("bytes_retrans"))
        inst = None
        if d_sent is not None and d_rbytes is not None and d_sent > 0 \
                and 0 <= d_rbytes <= d_sent:
            inst = d_rbytes / d_sent
        elif d_segs is not None and d_retrans is not None and d_segs > 0 \
                and 0 <= d_retrans <= d_segs:
            inst = d_retrans / d_segs
        if inst is not None:
            e["ratio"] = inst if e["ratio"] is None else \
                (1.0 - self.ALPHA) * e["ratio"] + self.ALPHA * inst
        if d_retrans is not None and d_retrans >= 0:
            rate = d_retrans / dt
            e["rate"] = rate if e["rate"] <= 0 else \
                (1.0 - self.ALPHA) * e["rate"] + self.ALPHA * rate

    def _prune_locked(self, now: float) -> None:
        for key in [k for k, e in self._links.items()
                    if now - e["t"] > self.STALE]:
            del self._links[key]

    # --------------------------------------------------------------- sinyaller
    def retrans_ratio(self) -> float:
        """Taze linklerde en yuksek yeniden iletim orani (yoksa 0)."""
        with self._lock:
            vals = [e["ratio"] for e in self._links.values()
                    if e["ratio"] is not None]
            return max(vals) if vals else 0.0

    def retrans_rate(self) -> float:
        """Taze linklerde en yuksek yeniden iletim/sn (yoksa 0)."""
        with self._lock:
            vals = [e["rate"] for e in self._links.values()]
            return max(vals) if vals else 0.0

    def rtt_inflation(self) -> float:
        """Taze linklerde en kotu RTT/taban orani (bilinmiyorsa 1.0)."""
        with self._lock:
            worst = 1.0
            for e in self._links.values():
                if e["n"] < self.WARM or e["rtt_base"] <= 0:
                    continue
                worst = max(worst, e["rtt_ema"] / e["rtt_base"])
            return worst

    def congested(self) -> bool:
        """Yeniden iletim artti VE RTT tabana gore sismis mi?"""
        ratio = self.retrans_ratio()
        ratio_hot = ratio >= self.RATIO_MIN or \
            self.retrans_rate() >= self.RATE_MIN
        return ratio_hot and self.rtt_inflation() >= self.RTT_FACTOR

    def reset(self) -> None:
        with self._lock:
            self._links.clear()


def _delta(cur, prev):
    """Iki sayac arasindaki pozitif olmayan/sarmalanmis farki yok sayar."""
    if not isinstance(cur, int) or isinstance(cur, bool) \
            or not isinstance(prev, int) or isinstance(prev, bool):
        return None
    return cur - prev
