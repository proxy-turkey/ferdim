"""LAN esbazli parca onbellegi (ROADMAP 2.x, istege bagli).

Ayni agdaki makineler, Merkle bildiriminde listelenen ve SHA-256 ile
dogrulanmis parcalari internetten yeniden cekmek yerine birbirinden alir.
Varsayilan KAPALI: `peer_cache` acik degilse hicbir soket dinlenmez, beacon
yollanmaz ve indirme yolu degismez.

Bilesenler:
  * `PeerIndex`   - `.ferdim-merkle` bildirimleri (indirme klasorleri) ve
                    Merkle ile dogrulanmis bitmis dosyalarin kaydi (durum
                    dizinindeki `peer-index.json`): parca ozeti ->
                    (dosya, ofset, uzunluk). Yalnizca kayitli ofsetler
                    sunulur; istemci yolu ASLA gormez.
  * `PeerDiscovery` - UDP multicast beacon ({v, node_id, port, roots}) ve
                    ayni gruptan gelen beacon'lardan esbaz listesi.
  * `PeerServer`  - HTTP; YALNIZCA `GET /chunk/<sha256>`. Birincil LAN
                    adresine baglanir, ozel olmayan kaynak IP'leri reddeder,
                    hiz isteklerini sinirlar; sunmadan once parcanin
                    SHA-256'sini dogrular. Dizin listesi yok.
  * `PeerClient`  - koku duyuran esbazlardan parca ister; her parcayi
                    yazmadan once SHA-256 ile dogrular (esbazlar guvenilmez).

Gizlilik: beacon'lar makinenin hangi Merkle koklerine sahip oldugunu LAN'a
duyurur. Bu yuzden ozellik istege baglidir; `peer_cache` kapatilinca hicbir
sey duyurulmaz. Beacon'lar yalnizca kok listesi tasir, dosya adi/yolu tasimaz.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import os
import secrets
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import cdc, util

MULTICAST_GROUP = "239.255.70.77"
DEFAULT_PORT = 8379
CACHE_NAME = "peer-index.json"
MAX_ROOTS = 64                  # beacon basina duyurulan kok
MAX_INDEX_FILES = 2000
MAX_INDEX_CHUNKS = 250_000
BEACON_INTERVAL = 5.0           # sn
PEER_STALE = 20.0               # sn: beacon'i kesilen esbaz unutulur
CHUNK_TIMEOUT = 5.0             # sn: tek parca istegi
FETCH_BUDGET = 8.0              # sn: baslangicta esbaz taramasina ayrilan sure
MAX_PEERS = 8
RATE_BYTES_PER_SEC = 64 << 20   # sunucu cikis tavani
RATE_BURST = 256 << 20
RATE_REQ_PER_MIN = 600

log = logging.getLogger("ferdim.peer")


# ------------------------------------------------------------------ yardim
def is_lan_ip(ip: str) -> bool:
    """Kaynak adresi ozel/yerel mi (RFC1918, ULA, link-local, loopback)?

    Genel (Internet) adresler esbaz sunucusuna erisemez: acik bir parca
    sunucusu cevrimici ortama sizmasin.
    """
    try:
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return False
    if addr.is_unspecified or addr.is_multicast:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local)


def _hex64(value) -> str:
    s = str(value or "").strip().lower()
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        return ""
    return s


def subtract_ranges(pending, filled):
    """`filled` [(bas, son)] araliklarini `pending` listesinden cikarir.

    Kismen dolu araliklar bolunur; cikarma sonucu bos parca uretilmez.
    Safligi testlerde deterministik dogrulanir (ag yok).
    """
    holes = sorted((int(s), int(e)) for s, e in filled)
    out = []
    for s, e in pending:
        cur = int(s)
        end = int(e)
        for fs, fe in holes:
            if fe < cur or fs > end:
                continue
            if fs > cur:
                out.append((cur, min(end, fs - 1)))
            cur = max(cur, fe + 1)
            if cur > end:
                break
        if cur <= end:
            out.append((cur, end))
    return out


def _entry_from_manifest(man, path: Path) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file() or st.st_size != man.size:
        return None
    return {"root": man.root, "path": str(path), "size": man.size,
            "chunks": [list(c) for c in man.chunks]}


# ------------------------------------------------------------------- indeks
class PeerIndex:
    """Dogrulanmis parca dizini: parca ozeti -> (dosya, ofset, uzunluk)."""

    def __init__(self, dirs=(), cache_path=None, *,
                 max_files: int = MAX_INDEX_FILES,
                 max_chunks: int = MAX_INDEX_CHUNKS):
        self._lock = threading.RLock()
        self._dirs = [Path(d) for d in dirs]
        self._cache = Path(cache_path) if cache_path is not None else None
        self.max_files = max(1, int(max_files))
        self.max_chunks = max(1, int(max_chunks))
        self._entries: list[dict] = []
        self._map: dict[str, tuple[str, int, int]] = {}
        self._roots: list[str] = []
        self._built = 0.0

    # ---------------------------------------------------------------- yapim
    def set_dirs(self, dirs) -> None:
        with self._lock:
            new = [Path(d).expanduser() for d in (dirs or [])
                   if str(d).strip()]
            if new != self._dirs:
                self._dirs = new
                self._built = 0.0

    def refresh(self, force: bool = False, interval: float = 30.0) -> None:
        """Dizini ve onbellegi tazeler (en fazla `interval` sn'de bir)."""
        with self._lock:
            now = time.monotonic()
            if not force and self._built and now - self._built < interval:
                return
            self._built = now
            entries = self._load_cache_locked()
            seen = {e["path"] for e in entries}
            for d in self._dirs:
                if len(entries) >= self.max_files:
                    break
                try:
                    paths = sorted(d.rglob(f"*{cdc.SUFFIX}"))
                except OSError:
                    continue
                for man_path in paths:
                    if len(entries) >= self.max_files:
                        break
                    data_path = man_path.with_suffix("")
                    if str(data_path) in seen:
                        continue
                    try:
                        if man_path.stat().st_size > cdc.MAX_MANIFEST:
                            continue
                        man = cdc.read_manifest(man_path)
                    except (OSError, ValueError):
                        continue
                    ent = _entry_from_manifest(man, data_path)
                    if ent is not None:
                        entries.append(ent)
                        seen.add(ent["path"])
            self._entries = entries
            self._reindex_locked()

    def add(self, man, path) -> bool:
        """Merkle ile dogrulanmis bir dosyayi kalici onbellege ekler."""
        try:
            ent = _entry_from_manifest(man, Path(path))
        except (OSError, ValueError, TypeError):
            return False
        if ent is None:
            return False
        try:
            with self._lock:
                self._entries = [e for e in self._entries
                                 if e["path"] != ent["path"]]
                self._entries.insert(0, ent)
                if len(self._entries) > self.max_files:
                    self._entries = self._entries[:self.max_files]
                self._reindex_locked()
                self._save_cache_locked()
            return True
        except OSError as exc:
            log.warning("esbaz indeksi yazilamadi: %s", exc)
            return False

    def _reindex_locked(self) -> None:
        mapping: dict[str, tuple[str, int, int]] = {}
        roots = []
        total = 0
        kept = []
        for ent in self._entries:
            path = Path(ent["path"])
            try:
                if not path.is_file() or path.stat().st_size != ent["size"]:
                    continue
            except OSError:
                continue
            kept.append(ent)
            if ent["root"] not in roots:
                roots.append(ent["root"])
            for off, ln, hx in ent["chunks"]:
                if total >= self.max_chunks:
                    break
                mapping.setdefault(hx, (ent["path"], int(off), int(ln)))
                total += 1
            if total >= self.max_chunks:
                break
        self._entries = kept
        self._map = mapping
        self._roots = roots

    # ------------------------------------------------------------- sorgulama
    def lookup(self, hx: str) -> tuple[str, int, int] | None:
        hx = _hex64(hx)
        if not hx:
            return None
        with self._lock:
            ent = self._map.get(hx)
            if ent is None:
                return None
            path, off, ln = ent
            try:
                if not Path(path).is_file() or os.path.getsize(path) < off + ln:
                    return None
            except OSError:
                return None
            return ent

    def roots(self, limit: int = MAX_ROOTS) -> list[str]:
        with self._lock:
            return list(self._roots[:max(0, int(limit))])

    def has_root(self, root: str) -> bool:
        with self._lock:
            return root in self._roots

    # -------------------------------------------------------------- onbellek
    def _load_cache_locked(self) -> list[dict]:
        if self._cache is None:
            return []
        raw = util.read_json(self._cache)
        items = raw.get("files") if isinstance(raw, dict) else None
        out = []
        if not isinstance(items, list):
            return out
        for it in items[:self.max_files]:
            if not isinstance(it, dict):
                continue
            root = _hex64(it.get("root"))
            path = it.get("path")
            size = it.get("size")
            chunks = it.get("chunks")
            if not root or not isinstance(path, str) or not path \
                    or not isinstance(size, int) or not isinstance(chunks, list):
                continue
            valid = []
            pos = 0
            for c in chunks[:self.max_chunks]:
                if not (isinstance(c, list) and len(c) == 3):
                    valid = []
                    break
                off, ln, hx = c
                if not isinstance(off, int) or not isinstance(ln, int) \
                        or off != pos or ln <= 0 or ln > cdc.MAX_CHUNK:
                    valid = []
                    break
                hx = _hex64(hx)
                if not hx:
                    valid = []
                    break
                valid.append([off, ln, hx])
                pos += ln
            if not valid or pos != size:
                continue
            out.append({"root": root, "path": path, "size": size,
                        "chunks": valid})
        return out

    def _save_cache_locked(self) -> None:
        if self._cache is None:
            return
        self._cache.parent.mkdir(parents=True, exist_ok=True)
        util.atomic_write_json(self._cache,
                               {"v": 1, "files": self._entries})


# --------------------------------------------------------------- kesif (beacon)
def parse_beacon(raw: bytes, own_id: str = "") -> dict | None:
    """Ham UDP beacon'ini dogrular; bozuk/yanlis surum None."""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None
    node_id = str(data.get("node_id") or "")[:64]
    if not node_id or node_id == own_id:
        return None
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    roots = []
    for r in data.get("roots") or []:
        hx = _hex64(r)
        if hx and hx not in roots:
            roots.append(hx)
        if len(roots) >= MAX_ROOTS:
            break
    return {"node_id": node_id, "port": port, "roots": roots}


class PeerDiscovery:
    """UDP multicast beacon'larini yollar ve toplar (en iyi caba)."""

    def __init__(self, node_id: str, port: int = DEFAULT_PORT,
                 roots_fn=None, group: str = MULTICAST_GROUP,
                 enabled: bool = True):
        self.node_id = node_id
        self.port = int(port)
        self.group = group
        self._roots_fn = roots_fn or (lambda: [])
        self.enabled = enabled
        self._lock = threading.Lock()
        self._peers: dict[tuple, dict] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._send = None

    # -------------------------------------------------------------- yasam
    def start(self) -> None:
        if not self.enabled or self._threads:
            return
        self._stop.clear()
        for target in (self._listen_loop, self._announce_loop):
            t = threading.Thread(target=target, daemon=True,
                                 name=f"ferdim-peer-{target.__name__}")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        sock, self._send = self._send, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(1.0)
        self._threads = []
        with self._lock:
            self._peers.clear()

    def set_port(self, port: int) -> None:
        self.port = int(port)

    # ------------------------------------------------------------- esbazlar
    def peers(self) -> list[dict]:
        """Taze esbazlar: [{host, port, roots, node_id}]."""
        now = time.monotonic()
        with self._lock:
            for key in [k for k, p in self._peers.items()
                        if now - p["t"] > PEER_STALE]:
                del self._peers[key]
            return [dict(host=k[0], port=p["port"], roots=list(p["roots"]),
                         node_id=p["node_id"])
                    for k, p in self._peers.items()]

    def peers_for(self, root: str) -> list[tuple[str, int]]:
        root = _hex64(root)
        out = []
        for p in self.peers():
            if root and root in p["roots"]:
                out.append((p["host"], p["port"]))
        return out

    def _remember(self, host: str, beacon: dict) -> None:
        if not is_lan_ip(host):
            return
        with self._lock:
            self._peers[(host, beacon["port"])] = {
                "port": beacon["port"], "node_id": beacon["node_id"],
                "roots": beacon["roots"], "t": time.monotonic()}

    # -------------------------------------------------------------- dongu
    def _listen_loop(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                 socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", self.port))
            mreq = struct.pack("4s4s", socket.inet_aton(self.group),
                               socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(1.0)
        except OSError as exc:
            log.warning("esbaz beacon dinleyicisi baslatilamadi: %s", exc)
            return
        try:
            while not self._stop.is_set():
                try:
                    raw, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                beacon = parse_beacon(raw, self.node_id)
                if beacon is not None:
                    self._remember(addr[0], beacon)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _announce_loop(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                 socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            self._send = sock
        except OSError as exc:
            log.warning("esbaz beacon yollayicisi baslatilamadi: %s", exc)
            return
        try:
            while not self._stop.wait(BEACON_INTERVAL):
                try:
                    roots = [_hex64(r) for r in self._roots_fn()[:MAX_ROOTS]]
                    payload = json.dumps({
                        "v": 1, "node_id": self.node_id, "port": self.port,
                        "roots": [r for r in roots if r],
                    }, separators=(",", ":")).encode()
                    sock.sendto(payload, (self.group, self.port))
                except (OSError, TypeError, ValueError):
                    continue
        finally:
            try:
                sock.close()
            except OSError:
                pass


# --------------------------------------------------------------- HTTP sunucu
class _RateLimiter:
    """Kaba cikis siniri: bayt/sn + kaynak IP basina istek/dk."""

    def __init__(self, rate: float = RATE_BYTES_PER_SEC,
                 burst: float = RATE_BURST,
                 req_per_min: int = RATE_REQ_PER_MIN, now=None):
        self.rate = float(rate)
        self.burst = float(burst)
        self.req_per_min = int(req_per_min)
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._tokens = float(burst)
        self._t = self._now()
        self._req: dict[str, tuple[float, int]] = {}

    def allow(self, ip: str, nbytes: int) -> bool:
        now = self._now()
        with self._lock:
            dt = max(0.0, now - self._t)
            self._t = now
            self._tokens = min(self.burst, self._tokens + dt * self.rate)
            start, count = self._req.get(ip, (now, 0))
            if now - start >= 60.0:
                start, count = now, 0
            count += 1
            self._req[ip] = (start, count)
            if count > self.req_per_min or self._tokens < nbytes:
                return False
            self._tokens -= nbytes
            return True


class PeerHandler(BaseHTTPRequestHandler):
    """Yalnizca `/chunk/<sha256>`; yol, listeleme veya dosya adi yok."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _err(self, code: int) -> None:
        body = b""
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        srv = self.server
        if not srv.peer_source_ok(self.client_address[0]):
            self._err(403)
            return
        path = urlsplit(self.path).path
        if not path.startswith("/chunk/"):
            self._err(404)
            return
        hx = path[len("/chunk/"):]
        if path.count("/") != 2:
            self._err(404)
            return
        hx = _hex64(hx)
        if not hx:
            self._err(400)
            return
        ent = srv.peer_index.lookup(hx)
        if ent is None:
            self._err(404)
            return
        path_s, off, ln = ent
        if not srv.peer_limiter.allow(self.client_address[0], ln):
            self._err(429)
            return
        try:
            with open(path_s, "rb") as f:
                f.seek(off)
                data = f.read(ln)
        except OSError:
            self._err(404)
            return
        if len(data) != ln or hashlib.sha256(data).hexdigest() != hx:
            self._err(404)          # bozuk/eski dosya sunulmaz
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(ln))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


class PeerServer:
    """HTTP parca sunucusu: birincil LAN adresine baglanir."""

    def __init__(self, index: PeerIndex, host: str | None = None,
                 port: int = DEFAULT_PORT, limiter=None):
        self.index = index
        self.host = host or primary_address() or "127.0.0.1"
        self._limiter = limiter or _RateLimiter()
        self._httpd = ThreadingHTTPServer(
            (self.host, int(port)), PeerHandler)
        self._httpd.daemon_threads = True
        self._httpd.peer_index = index
        self._httpd.peer_limiter = self._limiter
        self._httpd.peer_source_ok = self.source_ok
        self._thread = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def source_ok(self, ip: str) -> bool:
        return is_lan_ip(ip)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True,
                                        name="ferdim-peer-http")
        self._thread.start()

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None


def primary_address() -> str:
    """Birincil LAN IPv4 adresi (paket yollamadan); yoksa bos dize."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))       # TEST-NET: paket gitmez
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""
    return ip if is_lan_ip(ip) else ""


# -------------------------------------------------------------------- istemci
class PeerClient:
    """Koku duyuran esbazlardan parca ister; her parca SHA-256 ile dogrulanir."""

    def __init__(self, discovery: PeerDiscovery | None = None, *,
                 peers=None, timeout: float = CHUNK_TIMEOUT,
                 budget: float = FETCH_BUDGET, now=None):
        self.discovery = discovery
        self.timeout = float(timeout)
        self.budget = float(budget)
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._injected: list[tuple[str, int]] = []
        self.set_peers(peers or [])

    def set_peers(self, peers) -> None:
        out = []
        for p in peers or []:
            try:
                if isinstance(p, str):
                    host, _, port = p.rpartition(":")
                    if not host:
                        host, port = p, DEFAULT_PORT
                    out.append((host.strip("[]"), int(port)))
                else:
                    host, port = p
                    out.append((str(host), int(port)))
            except (TypeError, ValueError):
                continue
        with self._lock:
            self._injected = out

    def candidates(self, root: str) -> list[tuple[str, int]]:
        out = []
        if self.discovery is not None:
            out.extend(self.discovery.peers_for(root))
        with self._lock:
            out.extend(self._injected)
        seen, uniq = set(), []
        for ep in out:
            if ep not in seen:
                seen.add(ep)
                uniq.append(ep)
        return uniq[:MAX_PEERS]

    def fetch_chunk(self, host: str, port: int, hx: str, ln: int):
        """Tek parcayi ister ve dogrular; basarisizsa None (istisna yok)."""
        conn = None
        try:
            conn = http.client.HTTPConnection(host, port,
                                              timeout=self.timeout)
            conn.request("GET", f"/chunk/{hx}", headers={
                "User-Agent": "ferdim-peer", "Accept": "application/octet-stream",
                "Connection": "close"})
            resp = conn.getresponse()
            if resp.status != 200:
                return None
            cl = resp.getheader("Content-Length")
            if cl is not None and int(cl) != ln:
                return None
            data = resp.read(ln)
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if len(data) != ln:
            return None
        if hashlib.sha256(data).hexdigest() != hx:
            return None                 # esbaz guvenilmez: parca atilir
        return data

    def fetch_chunks(self, man, pending, fd, stop=None):
        """Eksik parcalari esbazlardan doldurur; [(offset, uzunluk)] doner.

        Yalnizca TAMAMEN `pending` araliklarinin icinde kalan parcalar
        denenir; yazma once SHA-256 dogrulamasindan gecer. Sure butcesi
        asilirsa kalan parcalar normal kaynak araliklarina birakilir.
        """
        if man is None or not pending:
            return []
        peers = self.candidates(man.root)
        if not peers:
            return []
        deadline = self._now() + self.budget
        filled = []
        for off, ln, hx in man.chunks:
            if stop is not None and stop():
                break
            if self._now() >= deadline:
                break
            last = off + ln - 1
            if not any(s <= off and last <= e for s, e in pending):
                continue
            data = None
            for host, port in peers:
                if self._now() >= deadline:
                    break
                data = self.fetch_chunk(host, port, hx, ln)
                if data is not None:
                    break
            if data is None:
                continue
            try:
                os.pwrite(fd, data, off)
            except OSError:
                break
            filled.append((off, ln))
        return filled


# ----------------------------------------------------------------- dugum
class PeerNode:
    """Bir AppEnv icin esbaz dugumu: indeks + kesif + sunucu + istemci."""

    def __init__(self, env, *, peers=None, multicast: bool = True,
                 host: str | None = None):
        self.env = env
        self.node_id = secrets.token_hex(8)
        self.index = PeerIndex(cache_path=Path(env.state_dir) / CACHE_NAME)
        self.discovery = PeerDiscovery(self.node_id,
                                       roots_fn=lambda: self.index.roots(),
                                       enabled=multicast)
        self.client = PeerClient(self.discovery, peers=peers)
        self.server: PeerServer | None = None
        self._host = host
        self._lock = threading.Lock()

    def set_peers(self, peers) -> None:
        self.client.set_peers(peers)

    def start(self, cfg: dict) -> bool:
        """`peer_cache` acikken sunucuyu ve beacon'lari baslatir."""
        if not cfg.get("peer_cache"):
            return False
        try:
            port = int(cfg.get("peer_port") or DEFAULT_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        dirs = cfg.get("peer_dirs") or [str(self.env.download_dir)]
        with self._lock:
            self.index.set_dirs(dirs)
            try:
                self.index.refresh(force=True)
            except Exception as exc:       # indeks hatasi servisi durdurmaz
                log.warning("esbaz indeksi kurulamadi: %s", exc)
            if self.server is None:
                try:
                    self.server = PeerServer(self.index, host=self._host,
                                             port=port)
                except OSError as exc:
                    log.warning("esbaz sunucusu baslatilamadi: %s", exc)
                    return False
                self.server.start()
            self.discovery.set_port(self.server.port)
            self.discovery.enabled = True
            self.discovery.start()
        return True

    def stop(self) -> None:
        with self._lock:
            self.discovery.stop()
            if self.server is not None:
                self.server.stop()
                self.server = None

    def add_verified(self, man, path) -> bool:
        return self.index.add(man, path)

    def fetch_chunks(self, man, pending, fd, stop=None):
        try:
            self.index.refresh()
        except Exception:
            pass
        return self.client.fetch_chunks(man, pending, fd, stop=stop)


_OPEN_LOCK = threading.Lock()


def open_peer(env) -> PeerNode:
    """AppEnv basina tek dugum: isler ve daemon ayni indeksi paylasir."""
    node = getattr(env, "peer_node", None)
    if node is not None:
        return node
    with _OPEN_LOCK:
        node = getattr(env, "peer_node", None)
        if node is None:
            node = PeerNode(env)
            env.peer_node = node
    return node
