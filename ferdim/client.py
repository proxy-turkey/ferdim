"""Unix socket uzerinden JSON-satir RPC istemcisi + daemon baslatici."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time


class FerdimClient:
    def __init__(self, socket_path: str, timeout: float = 10.0):
        self.socket_path = socket_path
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def connect(self) -> "FerdimClient":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.socket_path)
        self._sock = s
        return self

    def request(self, cmd: str, **params) -> dict:
        if self._sock is None:
            self.connect()
        payload = json.dumps({"cmd": cmd, **params}, ensure_ascii=False)
        self._sock.sendall((payload + "\n").encode())
        f = self._sock.makefile("rb")
        line = f.readline()
        if not line:
            raise ConnectionError("daemon baglantiyi kapatti")
        return json.loads(line)

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()


def ping(socket_path: str) -> bool:
    try:
        with FerdimClient(socket_path, timeout=2.0) as c:
            r = c.request("ping")
            return bool(r.get("ok"))
    except Exception:
        return False


def ensure_daemon(env, spawn=True, python_bin=sys.executable) -> bool:
    """Socket canli degilse daemon'u arka planda baslatir."""
    if ping(str(env.socket_path)):
        return True
    if not spawn:
        return False
    pkg_parent = str(env.state_dir)  # sadece log icin
    log = open(env.log_path, "ab", buffering=0)
    subprocess.Popen(
        [python_bin, "-m", "ferdim.daemon"],
        cwd="/",
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True,
    )
    for _ in range(50):  # 5 sn
        time.sleep(0.1)
        if ping(str(env.socket_path)):
            return True
    return False
