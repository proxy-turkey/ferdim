"""FerDim daemon: socket RPC + HTTP API + kuyruk yonetimi."""
from __future__ import annotations

import logging
import os
import secrets
import signal
import threading

from . import __version__
from .engine.queue import QueueManager
from .paths import AppEnv
from .server import SockServer, make_server


def main() -> int:
    env = AppEnv()
    cfg = env.load_config()

    logging.basicConfig(
        filename=env.log_path, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("ferdim")

    # ayni anda tek daemon
    try:
        pid = int(env.pid_path.read_text())
        os.kill(pid, 0)
        import sys
        print("FerDim daemon zaten calisiyor (pid %d)" % pid, file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass
    env.pid_path.write_text(str(os.getpid()))

    if not env.token_path.exists():
        env.token_path.write_text(secrets.token_hex(16))
        os.chmod(env.token_path, 0o600)

    qm = QueueManager(env, cfg)
    ctx = {
        "qm": qm,
        "env": env,
        "cfg": cfg,
        "set_cfg": lambda c: env.save_config(c),
    }

    sock_srv = SockServer(str(env.socket_path), ctx)
    threading.Thread(target=sock_srv.serve_forever, daemon=True).start()

    web_srv = None
    if cfg.get("web_enabled", True):
        try:
            web_srv = make_server(env, ctx, int(cfg.get("web_port", 8378)))
            env.port_path.write_text(str(web_srv.server_address[1]))
            threading.Thread(target=web_srv.serve_forever, daemon=True).start()
        except Exception as e:
            log.warning("web arayuzu baslatilamadi: %s", e)

    # opsiyonel pano izleyici
    if cfg.get("clipboard_watch"):
        try:
            from .clipboard import ClipboardWatcher
            ClipboardWatcher(qm, cfg).start()
        except Exception as e:
            log.warning("pano izleyici baslatilamadi: %s", e)

    stop = threading.Event()

    def _term(*_a):
        stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    log.info("FerDim %s basladi (socket=%s)", __version__, env.socket_path)
    print(f"FerDim {__version__} calisiyor  (socket: {env.socket_path})",
          flush=True)
    if web_srv:
        print(f"Web arayuzu: http://127.0.0.1:{web_srv.server_address[1]}",
              flush=True)

    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        print("Kapatiliyor...")
        qm.shutdown()
        sock_srv.shutdown()
        if web_srv:
            web_srv.shutdown()
        env.socket_path.unlink(missing_ok=True)
        env.pid_path.unlink(missing_ok=True)
        log.info("FerDim durduruldu")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
