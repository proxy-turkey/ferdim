"""Testler icin Range destekli yerel HTTP sunucusu."""
from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.parse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.urandom(4 * 1024 * 1024 + 7777)  # 4 MB + tuhaf kuyruk
LIMIT_DATA = DATA                          # limitli yol: 4 MB (bolluk icin)
LIMIT_SEM = threading.BoundedSemaphore(3)  # sunucu en fazla 3 eszamanli
SEG = [os.urandom(200_000 + i * 333) for i in range(8)]  # HLS segmentleri
ETAG_V1 = '"v1"'
ETAG_V2 = '"v2"'


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/octet-stream",
              extra: list[tuple[str, str]] = ()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        p = urllib.parse.urlsplit(self.path).path

        if p == "/file.bin":
            rng = None if head else self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, b = rng[6:].split("-", 1)
                a = int(a)
                b = int(b) if b else len(DATA) - 1
                body = b"" if head else DATA[a:b + 1]
                self._send(206, body, extra=[
                    ("Content-Range", f"bytes {a}-{b}/{len(DATA)}"),
                    ("Accept-Ranges", "bytes"), ("ETag", ETAG_V1),
                    ("Content-Disposition",
                     "attachment; filename=\"dosya.bin\"; "
                     "filename*=UTF-8''dosya%20ad%C4%B1.bin")])
            else:
                body = b"" if head else DATA
                self._send(200, body, extra=[
                    ("Accept-Ranges", "bytes"), ("ETag", ETAG_V1),
                    ("Content-Disposition",
                     "attachment; filename=\"dosya.bin\"; "
                     "filename*=UTF-8''dosya%20ad%C4%B1.bin")])
        elif p == "/file2.bin":  # degisen kaynak (ETag V2)
            rng = None if head else self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, b = rng[6:].split("-", 1)
                body = b"" if head else DATA[int(a):int(b) + 1]
                self._send(206, body, extra=[
                    ("Content-Range", f"bytes {a}-{int(b)}/{len(DATA)}"),
                    ("ETag", ETAG_V2)])
            else:
                self._send(200, b"" if head else DATA, extra=[("ETag", ETAG_V2)])
        elif p == "/norange.bin":  # Range yok, tek akis
            body = b"" if head else DATA[:1024 * 1024]
            self._send(200, body, ctype="application/x-iso")
        elif p == "/slow.bin":
            if head:
                self._send(200, b"", extra=[("Accept-Ranges", "bytes"),
                                            ("ETag", ETAG_V1)])
                return
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, b = rng[6:].split("-", 1)
                self.send_response(206)
                n = int(b) - int(a) + 1
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(DATA)}")
                self.send_header("Content-Length", str(n))
                self.send_header("ETag", ETAG_V1)
                self.end_headers()
                pos = int(a)
                while pos <= int(b):
                    time.sleep(0.004)
                    end = min(pos + 8192, int(b) + 1)
                    self.wfile.write(DATA[pos:end])
                    pos = end
            else:
                self._send(200, b"")
        elif p == "/limit3.bin":
            # eszamanli baglanti siniri olan sunucu taklidi: 4. baglanti 503
            if not LIMIT_SEM.acquire(blocking=False):
                self.send_response(503)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    a, b = rng[6:].split("-", 1)
                    a = int(a)
                    b = min(int(b), len(LIMIT_DATA) - 1)
                    body = LIMIT_DATA[a:b + 1]
                    self.send_response(206)
                    self.send_header("Content-Range",
                                     f"bytes {a}-{b}/{len(LIMIT_DATA)}")
                else:
                    body = LIMIT_DATA
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", ETAG_V1)
                self.end_headers()
                for i in range(0, len(body), 32768):
                    time.sleep(0.002)  # baglantilari ust uste bindir
                    self.wfile.write(body[i:i + 32768])
            finally:
                LIMIT_SEM.release()
        elif p == "/perconn.bin":
            # ISCI BASI hiz kisitli sunucu (yaygin durum): her baglanti ~0.4MB/s
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, b = rng[6:].split("-", 1)
                a = int(a)
                b = min(int(b), len(DATA) - 1)
                body = DATA[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range",
                                 f"bytes {a}-{b}/{len(DATA)}")
            else:
                body = DATA
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", ETAG_V1)
            self.end_headers()
            for i in range(0, len(body), 16384):
                time.sleep(0.04)
                self.wfile.write(body[i:i + 16384])
        elif p == "/redir":
            self.send_response(302)
            self.send_header("Location", "/file.bin")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif p == "/master.m3u8":
            body = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\n"
                    "media.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=900000\n"
                    "media.m3u8\n").encode()
            self._send(200, body, ctype="application/vnd.apple.mpegurl")
        elif p == "/media.m3u8":
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2"]
            for i in range(len(SEG)):
                lines += [f"#EXTINF:2.0,", f"seg{i:03d}.ts"]
            lines.append("#EXT-X-ENDLIST")
            self._send(200, "\n".join(lines).encode(),
                       ctype="application/vnd.apple.mpegurl")
        elif p.startswith("/seg"):
            i = int(p[4:7])
            self._send(200, SEG[i], ctype="video/mp2t")
        elif p == "/auth":
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                self._send(200, b"OK", ctype="text/plain")
            else:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="t"')
                self.send_header("Content-Length", "0")
                self.end_headers()
        else:
            self._send(404, b"yok")


def start_server() -> tuple[ThreadingHTTPServer, str]:
    ThreadingHTTPServer.request_queue_size = 128
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


DATA_SHA = hashlib.sha256(DATA).hexdigest()
