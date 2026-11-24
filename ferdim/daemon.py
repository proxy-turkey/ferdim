"""FerDim daemon: socket RPC + HTTP API + kuyruk yonetimi."""
from __future__ import annotations

import logging
import os
import secrets
import signal
import threading

from . import __version__
from . import api
from .engine.queue import QueueManager
from .paths import AppEnv
from .server import SockServer, make_server


def ensure_token(env) -> str:
    """Token'i her acilista 0600'a ceker (eski dosya dahil) ve dondurur.
    Yalniz ilk olusturmada chmod yapmak, bir kez genis izin almis dosyayi
    sonsuza kadar genis birakiyordu."""
    if env.token_path.exists():
        try:
            os.chmod(env.token_path, 0o600)
        except OSError:
            pass
    else:
        api.write_token(env.token_path, secrets.token_hex(16))
    return env.token_path.read_text("utf-8").strip()


def secure_log(env) -> None:
    """Log dosyasini 0600 ile hazirlar: logging.basicConfig dosyayi umask
    izniyle acar, bu yuzden once dar izinle olusturulur."""
    fd = os.open(env.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    try:
        os.chmod(env.log_path, 0o600)
    except OSError:
        pass


def main() -> int:
    env = AppEnv()
    raw = env.load_config()
    cfg = dict(raw)
    # el ile duzenlenmis config.json'daki bozuk degerler (ya da acilamayan
    # indirme dizini) daemon'u devirmesin: guvenli araliga cekilir
    api._sanitize_config(cfg, env)
    if cfg != raw:
        env.save_config(cfg)
    env.reload_download_dir(cfg)    # indirme dizini canli cfg'den cozulur

    # durum dizini yalniz kullaniciya acik olsun (token, is dosyalari, log)
    try:
        env.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(env.state_dir, 0o700)
    except OSError:
        pass
    secure_log(env)
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

    token = ensure_token(env)

    qm = QueueManager(env, cfg)
    ctx = {
        "qm": qm,
        "env": env,
        "cfg": cfg,
        "set_cfg": lambda c: env.save_config(c),
        # HTTP sunucusu baglaninca gercek port yazilir; `web-session` bileti
        # bu portla kurulur (0 = web arayuzu kapali).
        "web_port": 0,
    }
    # token tutucusu: HTTP katmani her istekte buradan okur, `token-rotate`
    # buradaki degeri degistirir
    api.web_access(ctx).set_token(token)

    sock_srv = SockServer(str(env.socket_path), ctx)
    threading.Thread(target=sock_srv.serve_forever, daemon=True).start()

    web_srv = None
    if cfg.get("web_enabled", True):
        try:
            web_srv = make_server(env, ctx, int(cfg.get("web_port", 8378)))
            ctx["web_port"] = web_srv.server_address[1]
            env.port_path.write_text(str(ctx["web_port"]))
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

    # izlenen klasorler: baglanti listesi / metalink dosyasi birakilinca
    # otomatik kuyruga eklenir (cfg'de klasor yokken neredeyse masrafsiz)
    try:
        from .watch import FolderWatcher
        FolderWatcher(qm, cfg, env,
                      lambda p: api.handle(ctx, "add-file", {"path": p})).start()
    except Exception as e:
        log.warning("klasor izleyici baslatilamadi: %s", e)

    # LAN esbaz onbellegi (istege bagli): peer_cache kapaliyken no-op.
    # Acikken parca sunucusu ve beacon'lar burada baslar; kapanista durur.
    peer_node = None
    try:
        from .peer import open_peer
        peer_node = open_peer(env)
        if peer_node.start(cfg):
            log.info("esbaz onbellegi acik (port %s)", peer_node.server.port)
    except Exception as e:
        log.warning("esbaz onbellegi baslatilamadi: %s", e)

    # masaustu bildirimleri: is bitince/hata verince (arka planda) bildir.
    # Bildirim kapaliysa isci sessizce bekler; config-set ile acilip
    # kapatilabilsin diye kosulsuz baslatilir.
    notifier = None
    try:
        from .notify import Notifier
        notifier = Notifier(cfg).start(qm)
    except Exception as e:
        log.warning("bildirim izleyici baslatilamadi: %s", e)

    stop = threading.Event()

    def _term(*_a):
        stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    log.info("FerDim %s basladi (socket=%s)", __version__, env.socket_path)
    print(f"FerDim {__version__} calisiyor  (socket: {env.socket_path})",
          flush=True)
    if web_srv:
        print(f"Web arayuzu: http://127.0.0.1:{ctx['web_port']}", flush=True)

    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        print("Kapatiliyor...")
        ctx["web_port"] = 0
        if notifier is not None:
            notifier.stop()
        qm.shutdown()
        if peer_node is not None:
            peer_node.stop()
        from .engine import h3      # paylasilan QUIC dongusu + UDP soketleri
        h3.close_all()
        sock_srv.shutdown()
        if web_srv:
            web_srv.shutdown()
        env.socket_path.unlink(missing_ok=True)
        env.pid_path.unlink(missing_ok=True)
        log.info("FerDim durduruldu")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
