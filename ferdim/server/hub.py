"""Paylasilan yayin merkezi: soket izleyicileri ve SSE istemcileri.

Temel kural: is parcaciklari ve istek is parcaciklari istemciye ASLA yazmaz.
`notify()` yalnizca bir bayrak kaldirir; tek itici parcacik en fazla
`interval` araliginda bir istatistik hesaplar. Istemci yokken hic
hesaplamaz (bosta CPU sifir). Her istemcinin TEK YUVALI kuyrugu vardir:
yeni kare eskisini ezer (birlestirme), yavas istemci kare kaybeder ama
iticiyi ve is parcaciklarini bloklamaz. Yazma butcesi asilirsa baglanti
kapatilir.

Tel bicimleri:
- soket izleyicisi: `{"event":"stats", ...}\n` (1.x sozlesmesi, degismez)
- SSE: `event: stats` + tek satir JSON veri; ~15 sn'de bir `: ping` yorumu
"""
from __future__ import annotations

import json
import logging
import socket
import struct
import threading
import time

LOG = logging.getLogger("ferdim")

PUSH_INTERVAL = 0.25      # degisiklik bildirimi icin en kucuk aralik (sn)
HEARTBEAT = 1.0           # istemci varken periyodik kare araligi (sn)
SEND_TIMEOUT = 3.0        # tek send() icin cekirdek zaman asimi (SO_SNDTIMEO)
FRAME_TIMEOUT = 12.0      # bir karenin toplam yazma butcesi (sn)
CHUNK = 64 * 1024         # parca boyu: gonderim butcesi parca parca denetlenir
SSE_KEEPALIVE = 15.0      # SSE yorum satiri araligi (sn)


def sock_frame(stats: dict) -> bytes:
    """Soket izleyicisi karesi: tek satir JSON (mevcut tel sozlesmesi)."""
    return (json.dumps({"event": "stats", **stats},
                       ensure_ascii=False) + "\n").encode()


def sse_frame(stats: dict) -> bytes:
    """SSE karesi: `event: stats` + JSON veri (`json.dumps` satir kirmez)."""
    data = json.dumps(stats, ensure_ascii=False, separators=(",", ":"))
    return ("event: stats\ndata: " + data + "\n\n").encode()


def mark(stats: dict) -> str:
    """Karenin 'degisti mi' ozeti: is listesi ve yapisal alanlar disinda.

    rev + turbo/limit/hiz/aktif gibi kucuk alanlari kapsar; 10 000 islik
    listenin JSON'unu her saniye uretmeden degisikligi yakalar. Ayni ozet
    iki ardisik pompayi esitse istemciye kare GONDERILMEZ. `full` disarida:
    yapisal bir isarettir (tam/fark), icerik degisikligi degildir."""
    return json.dumps({k: v for k, v in stats.items()
                       if k not in ("jobs", "removed", "full")},
                      ensure_ascii=False, sort_keys=True, default=str)


class Client:
    """Tek istemci: tek yuvali kuyruk + kendi yazici parcacigi.

    `delta` SSE istemcileri icindir (fark kareleri); soket izleyicisi her
    zaman tam kare alir. `rev` istemcinin gordugu son rev'dir, None ise
    sonraki kare tam gonderilir.

    `closed` bir threading.Event'tir: istek/bekleme parcaciklari akisin
    bittigini `closed.wait()` ile ogrenir.
    """

    def __init__(self, sock, name: str = "", keepalive: float = 0.0,
                 send_timeout: float = SEND_TIMEOUT,
                 frame_timeout: float = FRAME_TIMEOUT):
        self.sock = sock
        self.name = name
        self.delta = False
        self.rev = None
        self.mark = None          # son gonderilen karenin ust alan ozeti
        self.keepalive = float(keepalive)
        self._send_timeout = float(send_timeout)
        self._frame_timeout = float(frame_timeout)
        self._lock = threading.Lock()
        self._frame = b""
        self._event = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._sent = time.monotonic()
        # cekirdek duzeyi gonderim asimi: yavas istemci send()'i sonsuza
        # kadar kilitleyemez (Python settimeout'u paylasilan soketin okuma
        # tarafini da etkilerdi; SO_SNDTIMEO yalniz yazmayi sinirlar)
        try:
            sec = int(self._send_timeout)
            usec = int((self._send_timeout - sec) * 1e6)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                            struct.pack("ll", sec, usec))
        except (OSError, struct.error, AttributeError):
            pass                  # platform desteklemiyorsa kare butcesi kalir

    # -------------------------------------------------------------- yasam
    @property
    def closed(self) -> threading.Event:
        return self._closed

    def start(self) -> "Client":
        if self._thread is None and not self._closed.is_set():
            self._thread = threading.Thread(
                target=self._run, name="ferdim-yayin", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        """Baglantiyi kapatir: okuyan/yazan parcaciklar uyanir.

        Soket `shutdown` ile kapatilir (dosya tanitici sunucunun isidir);
        boylece yarim kalan bir yazma yuzunden fd yeniden kullanilmaz."""
        with self._lock:
            if self._closed.is_set():
                return
            self._frame = b""
            self._closed.set()
            self._event.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self._closed.wait(timeout)

    # -------------------------------------------------------------- kuyruk
    def offer(self, frame: bytes) -> bool:
        """Yeni kare birakir; bekleyen eski kare DUSER (birlestirme)."""
        with self._lock:
            if self._closed.is_set():
                return False
            self._frame = frame
            self._event.set()
        return True

    def take(self) -> bytes:
        """Bekleyen en yeni kareyi alir (yoksa bos bayt)."""
        with self._lock:
            frame, self._frame = self._frame, b""
            self._event.clear()
        return frame

    # ------------------------------------------------------------- yazici
    def _run(self) -> None:
        while True:
            self._event.wait(self.keepalive or None)
            if self._closed.is_set():
                return
            frame = self.take()
            if not frame:
                if not self.keepalive:
                    continue
                if time.monotonic() - self._sent < self.keepalive:
                    continue
                frame = b": ping\n\n"      # SSE kalp atisi (yorum satiri)
            if not self._send(frame):
                self.close()
                return

    def _send(self, data: bytes) -> bool:
        """Kareyi parca parca yazar; butce asilirsa False (baglanti kapanir)."""
        deadline = time.monotonic() + self._frame_timeout
        view = memoryview(data)
        while view:
            if time.monotonic() > deadline:
                return False
            try:
                sent = self.sock.send(view[:CHUNK])
            except (OSError, ValueError):
                return False          # zaman asimi / kapanmis soket
            if sent <= 0:
                return False
            view = view[sent:]
        self._sent = time.monotonic()
        return True


class Hub:
    """Istatistik karelerini butun istemcilere dagitan tek nokta.

    `stats(params)` cagrilabilir olmalidir (api.handle(ctx, "stats", params));
    `since` verildiginde fark karesi dondurebilir. rev yoksa (eski sunucu)
    butun kareler tam gonderilir.
    """

    def __init__(self, stats, interval: float = PUSH_INTERVAL,
                 heartbeat: float = HEARTBEAT):
        self._stats = stats
        self._interval = float(interval)
        self._heartbeat = float(heartbeat)
        self._lock = threading.Lock()
        self._clients: list[Client] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener = None
        self._last = 0.0

    # -------------------------------------------------------------- yasam
    def start(self, qm=None) -> "Hub":
        """Titreyen itici parcacigi baslatir; dinleyici bir kez baglanir."""
        if qm is not None and self._listener is None:
            try:
                qm.add_listener(self.notify)
                self._listener = qm
            except Exception:
                self._listener = None
        if self._thread is None and not self._stop.is_set():
            self._thread = threading.Thread(
                target=self._run, name="ferdim-itici", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        """Akislari bitirir, parcacigi durdurur (daemon kapanisi)."""
        self._stop.set()
        self._wake.set()
        if self._listener is not None:
            try:
                self._listener.remove_listener(self.notify)
            except Exception:
                pass
            self._listener = None
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            c.close()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._stop.is_set()

    # ----------------------------------------------------------- istemciler
    def add_client(self, sock, delta: bool = False, name: str = "",
                   keepalive: float = 0.0) -> Client:
        client = Client(sock, name=name, keepalive=keepalive)
        client.delta = bool(delta)
        with self._lock:
            if self._stop.is_set():
                client.close()
                return client           # cagiran `closed` ile hemen doner
            self._clients.append(client)
        client.start()
        self._wake.set()                # ilk kare beklemesin
        return client

    def add_sse(self, sock, name: str = "sse") -> Client:
        return self.add_client(sock, delta=True, name=name,
                               keepalive=SSE_KEEPALIVE)

    def remove_client(self, client: Client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        client.close()

    def count(self) -> int:
        with self._lock:
            return len(self._clients)

    def _snapshot(self) -> list:
        with self._lock:
            return list(self._clients)

    # ------------------------------------------------------------ bildirim
    def notify(self, _job=None) -> None:
        """Degisiklik isareti: YALNIZ bayrak (istatistik/IO yok).

        Is parcaciklari bu cagriya takilmaz; yavas bir istemci kuyrugu
        doldursa bile burada bekleme olmaz."""
        self._wake.set()

    # -------------------------------------------------------------- itici
    def _run(self) -> None:
        while not self._stop.is_set():
            signalled = self._wake.wait(self._heartbeat)
            if self._stop.is_set():
                break
            self._wake.clear()
            if signalled:
                gap = self._last + self._interval - time.monotonic()
                if gap > 0:
                    # hiz siniri: erken uyanmalar tek karede birlestirilir
                    self._stop.wait(gap)
                    if self._stop.is_set():
                        break
                    self._wake.clear()
            self._push()
            self._last = time.monotonic()

    def _push(self) -> None:
        clients = self._snapshot()
        if not clients:
            return            # istemci yok: istatistik HESAPLANMAZ (bosta CPU)
        full = None
        cached: dict = {}     # rev -> kare (ayni rev tek kez hesaplanir)
        for client in clients:
            try:
                if not client.delta:
                    # soket izleyicisi tel sozlesmesi: TAM kare (`jobs`
                    # butun liste). Yalniz DEGISIKLIK varsa gonderilir:
                    # rev/ust alanlar ayniysa kare uretilmez (10 000 iste
                    # saniyede megabaytlar JSON uretilmez).
                    if full is None:
                        full = self._stats({})
                    tag = mark(full)
                    if tag == client.mark:
                        continue
                    client.mark = tag
                    client.offer(sock_frame(full))
                    continue
                # SSE: istemci basina `since` -> yalniz degisen isler
                if client.rev is None:
                    if full is None:
                        full = self._stats({})
                    frame = full
                elif client.rev in cached:
                    frame = cached[client.rev]
                else:
                    frame = self._stats({"since": client.rev})
                    cached[client.rev] = frame
                rev = frame.get("rev")
                if not isinstance(rev, int):
                    # rev yok (eski sunucu): fark kurulamaz, her kare tam
                    # gonderilir; rev takibi bir sonraki karede yeniden denenir
                    client.rev = None
                else:
                    # kare yalniz DEGISIKLIK varsa gonderilir: rev ayni ve
                    # ust alanlar (turbo, hiz, limit, nm...) ayni ise trafik
                    # uretilmez
                    tag = mark(frame)
                    same = (rev == client.rev and tag == client.mark
                            and not frame.get("jobs")
                            and not frame.get("removed"))
                    client.rev = rev
                    client.mark = tag
                    if same:
                        continue          # degisiklik yok: kare gonderilmez
                client.offer(sse_frame(frame))
            except Exception:
                continue      # tek istemcinin hatasi digerlerini durdurmaz


def for_ctx(ctx: dict) -> Hub:
    """ctx basina tek merkez: soket ve HTTP sunuculari AYNI hub'i paylasir.

    Ilk olusturmada itici parcacik baslar ve kuyruk dinleyicisi baglanir;
    sonraki cagrilar ayni nesneyi dondurur (yeniden baglama yok)."""
    hub = ctx.get("hub")
    if hub is None:
        from .. import api
        hub = ctx["hub"] = Hub(lambda params: api.handle(ctx, "stats", params))
    return hub.start(ctx.get("qm"))
