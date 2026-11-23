"""Arayuz testleri icin yerel dosya sunucusu.

ROOT icinde ornek dosyalar (sabit tohumlu, yan dosyali) hazirlanir; /slow/
yolu ~6 MB'lik bir dosyayi yavas ve Range destegiyle sunar. Kullanim:
python3 srv.py ROOT PORT   (PORT=0: bos port, gercek port stdout'a yazilir)"""
import hashlib
import os
import random
import sys
import time
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 0


def make_fixtures(root: str) -> None:
    """Deterministik ornek dosyalar: dogru .sha256 yan dosyali ZIP, bayat
    .md5 yan dosyali PDF, baska kategorilerden dosyalar."""
    os.makedirs(root, exist_ok=True)
    if os.path.exists(os.path.join(root, "arsiv.zip")):
        return
    rnd = random.Random(7)
    blob = lambda n: bytes(rnd.getrandbits(8) for _ in range(n))
    with zipfile.ZipFile(os.path.join(root, "arsiv.zip"), "w",
                         zipfile.ZIP_DEFLATED) as z:
        for i in range(12):
            z.writestr(f"klasor/dosya-{i:02d}.txt", "veri " * 5000)
    data = open(os.path.join(root, "arsiv.zip"), "rb").read()
    with open(os.path.join(root, "arsiv.zip.sha256"), "w") as f:
        f.write(f"{hashlib.sha256(data).hexdigest()}  arsiv.zip\n")
    with open(os.path.join(root, "rapor.pdf"), "wb") as f:
        f.write(blob(300_000))
    with open(os.path.join(root, "rapor.pdf.md5"), "w") as f:      # bayat
        f.write("0" * 32 + "  rapor.pdf\n")
    for name, size in (("ornek-video.mp4", 2_000_000), ("yazilim.deb", 500_000),
                       ("sarki.mp3", 400_000)):
        with open(os.path.join(root, name), "wb") as f:
            f.write(blob(size))


SLOW_BODY = b"\0" * (6 << 20)


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if not self.path.startswith("/slow/"):
            super().do_GET()
            return
        a, b = 0, len(SLOW_BODY) - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, b = rng[6:].split("-", 1)
            a = int(a)
            b = min(int(b or b), len(SLOW_BODY) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(SLOW_BODY)}")
            self.send_header("Accept-Ranges", "bytes")
        else:
            self.send_response(200)
        body = SLOW_BODY[a:b + 1]
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            step = 64 << 10
            for i in range(0, len(body), step):     # ~1.5 MB/s toplam
                self.wfile.write(body[i:i + step])
                self.wfile.flush()
                time.sleep(0.04)
        except Exception:
            pass


make_fixtures(ROOT)
srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
srv.daemon_threads = True
# indirme testlerinde istemci baglantiyi kisabilir (yeniden baslatma, iptal):
# sunucu tarafindaki beklenen sifirlanmalar iz birakmasin
srv.handle_error = lambda *_a: None
print(srv.server_address[1], flush=True)      # gercek port (PORT=0 icin)
srv.serve_forever()
