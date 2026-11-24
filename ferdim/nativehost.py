"""Firefox native messaging koprusu.

Firefox <-> (4 byte uzunluk + JSON, stdin/stdout) <-> FerDim daemon (socket).
Tarayici ile daemon arasindaki kopru; daemon yoksa otomatik baslatir.
"""
from __future__ import annotations

import json
import struct
import sys
import threading
import time

from .client import FerdimClient, ensure_daemon, ping
from .paths import AppEnv

# Eklentiden daemon'a gecebilecek indirme alanlari (alan -> varsayilan).
# Tek 'download' ve toplu 'download-batch' AYNI kumeyi kullanir: merkle,
# proxy, insecure, checksum, headers gibi alanlar eklenti mesajindan gecmez.
# 'page' tek indirme sozlesmesindeki referer esidir (api cozer).
DOWNLOAD_DEFAULTS = {
    "url": "",
    "filename": "",
    "referer": "",
    "page": "",
    "cookies": "",
    "ua": "",
    "segments": None,
    "dir": "",
}


def download_params(msg: dict) -> dict:
    """Eklenti mesajindan yalnizca izinli indirme alanlarini al."""
    return {k: msg.get(k, d) for k, d in DOWNLOAD_DEFAULTS.items()}


def batch_items(msg: dict) -> list[dict]:
    """Toplu indirme ogelerini tek indirmeyle ayni alan kumesine indirger."""
    raw = msg.get("items")
    if not isinstance(raw, list):
        return []
    return [{k: it[k] for k in DOWNLOAD_DEFAULTS if k in it}
            for it in raw if isinstance(it, dict)]


_EOF = object()


def read_msg(stdin):
    """Bir cerceve okur. Akis bitti: _EOF; bozuk JSON: {} (bos komut)."""
    raw = stdin.buffer.read(4)
    if len(raw) < 4:
        return _EOF
    (n,) = struct.unpack("<I", raw)
    data = stdin.buffer.read(n)
    if len(data) < n:
        return _EOF
    try:
        return json.loads(data)
    except Exception:
        return {}


def write_msg(stdout, obj: dict) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode()
    stdout.buffer.write(struct.pack("<I", len(data)))
    stdout.buffer.write(data)
    stdout.buffer.flush()


def main() -> int:
    env = AppEnv()
    client: FerdimClient | None = None

    def get_client() -> FerdimClient | None:
        nonlocal client
        if client is not None:
            try:
                client.request("nm-ping")
                return client
            except Exception:
                client.close()
                client = None
        if ensure_daemon(env):
            try:
                client = FerdimClient(str(env.socket_path)).connect()
                client.request("nm-ping")
                return client
            except Exception:
                pass
        return None

    # eklenti baglanti durumunu daemon'a bildir (GUI gostergesi icin)
    def heartbeat():
        while True:
            time.sleep(45)
            get_client()

    threading.Thread(target=heartbeat, daemon=True).start()

    replied = False
    while True:
        msg = read_msg(sys.stdin)
        if msg is _EOF:
            break
        if not isinstance(msg, dict):
            # JSON gecerli ama nesne degil: komut yok, surec devrilmez
            write_msg(sys.stdout, {"ok": False, "error": "gecersiz istek",
                                   "inReplyTo": None})
            replied = True
            continue
        cmd = msg.pop("cmd", "")
        c = get_client()
        if c is None:
            write_msg(sys.stdout, {"ok": False,
                                   "error": "FerDim daemon calismiyor"})
            replied = True
            continue
        # eklenti -> daemon komut eslemesi
        if cmd == "download":
            r = c.request("add", **download_params(msg))
        elif cmd == "turbo":
            r = c.request("turbo", **({"on": bool(msg.get("on"))}
                                      if "on" in msg else {}))
        elif cmd == "ping":
            from . import __version__
            port = ""
            try:
                port = int(env.port_path.read_text().strip())
            except Exception:
                pass
            r = c.request("ping")  # turbo/segments alanlari da gelir
            r.update({"version": __version__, "port": port,
                      "downloaddir": str(env.download_dir)})
        elif cmd == "download-batch":
            r = c.request("add-batch", items=batch_items(msg))
        else:
            r = {"ok": False, "error": f"bilinmeyen komut {cmd!r}"}
        r["inReplyTo"] = cmd
        write_msg(sys.stdout, r)
        replied = True
    # stdin kapandi; Firefox host'u kapatiyor
    if client:
        client.close()
    return 0 if replied else 0


if __name__ == "__main__":
    raise SystemExit(main())
