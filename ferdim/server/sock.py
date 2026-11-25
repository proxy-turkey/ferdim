"""Unix socket JSON-satir RPC sunucusu + watch bildirimleri.

Yayin (watch) is parcaciklarinin isi DEGILDIR: `qm` dinleyicisi yalnizca
ortak merkeze (`hub.Hub`) bayrak kaldirir; tek itici parcacik istatistigi
en fazla 0.25 sn'de bir hesaplar ve her izleyicinin tek yuvali kuyruguna
birakir. Yavas bir istemci ne is parcaciklarini ne de diger izleyicileri
bloklar; butcesini asan baglanti kapatilir."""
from __future__ import annotations

import json
import os
import socketserver

from . import hub as _hub

MAX_RPC_LINE = 1024 * 1024   # tek satir istek: 1 MiB (bellek sinirsiz buyumesin)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server: SockServer = self.server
        watcher = None
        try:
            while True:
                # satir sinirlidir: istemci sonlandirici gondermezse okuma
                # 1 MiB + 1 baytta doner, baglanti too_large ile kapanir
                raw = self.rfile.readline(MAX_RPC_LINE + 1)
                if not raw:
                    break
                if len(raw) > MAX_RPC_LINE:
                    try:
                        self.wfile.write(b'{"ok":false,"code":"too_large"}\n')
                        self.wfile.flush()
                    except Exception:
                        pass
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    self.wfile.write(b'{"ok":false,"error":"gecersiz JSON"}\n')
                    continue
                if not isinstance(msg, dict):
                    self.wfile.write(b'{"ok":false,"error":"gecersiz JSON"}\n')
                    continue
                cmd = msg.pop("cmd", "")
                # istek kimligi 'req_id' alanindadir: 'id' bir IS kimligidir
                # (pause/resume/remove ...). Eskiden 'id' istek kimligi
                # sanilip parametreden atiliyordu; is komutlari bos kimlikle
                # gidiyordu ve hicbir zaman islemiyordu.
                req_id = msg.pop("req_id", None)
                if cmd == "watch":
                    # yazma itici parcacigin isi: bu is parcacigi yalnizca
                    # okumaya devam eder, yavas istemci onu bloklamaz
                    if watcher is None:      # ikinci 'watch' yeni kayit acmaz
                        watcher = server.register_watcher(self.connection)
                    continue
                from .. import api
                try:
                    resp = api.handle(server.ctx, cmd, msg)
                except Exception as e:
                    resp = {"ok": False, "error": str(e)}
                if req_id is not None:
                    resp["req_id"] = req_id
                try:
                    self.wfile.write(
                        (json.dumps(resp, ensure_ascii=False) + "\n").encode())
                    self.wfile.flush()
                except Exception:
                    break
        except Exception:
            pass
        finally:
            if watcher is not None:
                server.unregister_watcher(watcher)


class SockServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, ctx: dict):
        self.ctx = ctx
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        # soket dosyasi umask'tan bagimsiz 0700 kipte dogar: baglama ile
        # chmod arasinda dunyaya acik bir pencere kalmasin
        old = os.umask(0o077)
        try:
            super().__init__(path, _Handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        # soket ve HTTP sunucusu AYNI merkezi paylasir (ctx["hub"]): iki ayri
        # itici parcacik istatistigi iki kez hesaplamaz. Soket baglandiktan
        # SONRA kurulur: baglama basarisizsa bos parcacik baslatilmaz.
        self.hub: _hub.Hub = _hub.for_ctx(ctx)

    def register_watcher(self, sock):
        """Izleyiciyi merkeze baglar; donen nesne kapatma tutamacidir."""
        return self.hub.add_client(sock, name="sock-watch")

    def unregister_watcher(self, client):
        if client is not None:
            self.hub.remove_client(client)

    def shutdown(self):
        try:
            super().shutdown()
        finally:
            # izleyici kuyruklari ve SSE akislari kapansin: daemon kapanisi
            # bekleyen bir yazma yuzunden askida kalmasin
            self.hub.stop()
