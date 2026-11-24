"""ferdim komut satiri arayuzu."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__, util
from .client import FerdimClient, ensure_daemon
from .i18n import detect_lang, error_text, t as _
from .paths import AppEnv


def _lang(cfg_getter):
    try:
        return detect_lang(cfg_getter())
    except Exception:
        return "tr"


def _client(env, spawn=True) -> FerdimClient:
    if not ensure_daemon(env, spawn=spawn):
        print(_("cli_daemon_fail"), file=sys.stderr)
        print("Log: " + str(env.log_path), file=sys.stderr)
        raise SystemExit(1)
    return FerdimClient(str(env.socket_path))


def cmd_turbo(a, env):
    with _client(env) as c:
        if a.action == "status":
            r = c.request("turbo")
            print("TURBO:", _("turbo_on") if r.get("turbo") else _("turbo_off"))
            return 0
        r = c.request("turbo", on=(a.action == "on"))
    print("TURBO:", _("turbo_on") if r.get("turbo") else _("turbo_off"))
    if r.get("turbo"):
        print(_("turbo_hint"))
    return 0


def _add_file(c, path: str) -> int:
    """Yerel baglanti listesini / metalink belgesini kuyruga ekler;
    eklenen is sayisini dondurur."""
    r = c.request("add-file", path=path)
    if not r.get("ok"):
        print(f"{_('cli_error')}: {path}: {r.get('error', '')}", file=sys.stderr)
        return 0
    n = int(r.get("count") or len(r.get("ids") or []))
    print(f"{path}: " + _("cli_added_n", n=n))
    return n


def cmd_add(a, env):
    if a.url.startswith("@"):          # ferdim add @liste.txt
        with _client(env) as c:
            return 0 if _add_file(c, a.url[1:]) else 1
    checks = {}
    if a.checksum:
        algo, digest = util.parse_checksum_spec(a.checksum)
        if not algo:
            print(_("cli_error") + ": " + _("cli_bad_checksum"), file=sys.stderr)
            return 2
        checks[algo] = digest
    with _client(env) as c:
        r = c.request("add", url=a.url, filename=a.output, dir=a.dir,
                      segments=a.segments, headers=a.header, cookies=a.cookie,
                      user=a.user, password=a.password, proxy=a.proxy,
                      insecure=a.insecure, referer=a.referer,
                      limit=a.limit, mirrors=a.mirror,
                      sha256=a.sha256, md5=a.md5,
                      sha1=a.sha1, sha512=a.sha512, checksums=checks)
        if not r.get("ok"):
            print(_("cli_error") + ":", r.get("error"), file=sys.stderr)
            return 1
        ids = r.get("ids") or []
        if len(ids) > 1:                # metalink: birden fazla dosya
            print(_("cli_added_n", n=len(ids)))
            return 0
        print(f"{_('cli_added')}: {r['id']}  ->  {r['filename']}")
        if a.wait:
            return _wait(c, r["id"])
    return 0


def cmd_add_file(a, env):
    with _client(env) as c:
        total = sum(_add_file(c, f) for f in a.files)
    return 0 if total else 1


def _wait(c, job_id):
    while True:
        time.sleep(0.4)
        st = c.request("stats")
        job = next((j for j in st["jobs"] if j["id"] == job_id), None)
        if not job:
            print("\nis kayboldu")
            return 1
        pct = (job["done"] * 100 // job["size"]) if job["size"] > 0 else 0
        line = (f"\r{pct:3d}%  {util.human_size(job['done'])}"
                f"/{util.human_size(job['size'])}  "
                f"{util.human_size(job['speed'])}/sn  "
                f"{_('st_' + job['state'])}      ")
        sys.stdout.write(line + ("\n" if job["state"] != "downloading" else ""))
        sys.stdout.flush()
        if job["state"] in ("done", "error", "canceled", "paused"):
            if job["state"] == "error":
                print(_("st_error") + ":",
                      error_text(job.get("error_code", ""), job.get("error", "")))
            print(_("cli_file") + ":", job["fullpath"] if job["state"] == "done" else "-")
            return 0 if job["state"] == "done" else 1


def cmd_list(a, env):
    with _client(env) as c:
        st = c.request("stats")
    if a.json:
        print(json.dumps(st, indent=2, ensure_ascii=False))
        return 0
    if not st["jobs"]:
        print(_("cli_queue_empty"))
        return 0
    for j in st["jobs"]:
        print(f"{j['id']:<10} {_('st_' + j['state']):<14} "
              f"{util.human_size(j['done']):>9}/{util.human_size(j['size']):<9} "
              f"{util.human_size(j['speed']):>10}/sn  {j['filename']}")
    return 0


def cmd_control(action):
    def run(a, env):
        with _client(env) as c:
            r = c.request(action, id=a.id)
        if not r.get("ok"):
            print("HATA:", r.get("error", "islem basarisiz"), file=sys.stderr)
            return 1
        return 0
    return run


def cmd_limit(a, env):
    with _client(env) as c:
        r = c.request("limit", value=a.value)
    print("hiz limiti:", util.human_size(r.get("speed_limit", 0)) + "/sn"
          if r.get("speed_limit") else "sinirsiz")
    return 0


def cmd_config(a, env):
    with _client(env) as c:
        if a.set:
            data = json.loads(a.set)
            r = c.request("config-set", config=data)
        else:
            r = c.request("config-get")
    print(json.dumps(r.get("config", r), indent=2, ensure_ascii=False))
    return 0


def cmd_daemon(a, env):
    from .daemon import main as daemon_main
    return daemon_main()


def cmd_web(a, env):
    import subprocess
    ensure_daemon(env)
    port = env.port_path.read_text().strip()
    url = f"http://127.0.0.1:{port}"
    print(url)
    subprocess.Popen(["xdg-open", url])
    return 0


def cmd_remove(a, env):
    with _client(env) as c:
        r = c.request("remove", id=a.id, delete_files=a.delete_files)
    return 0 if r.get("ok") else 1


def cmd_history(a, env):
    with _client(env) as c:
        if a.stats:
            r = c.request("history-stats")
            if not r.get("ok"):
                print(_("cli_error") + ":", r.get("error", ""), file=sys.stderr)
                return 1
            print(_("hist_stats", n=r.get("count", 0),
                    bytes=util.human_size(r.get("bytes", 0))))
            for h in r.get("hosts", []):
                print("  " + _("hist_host_line", host=h.get("host", ""),
                               n=h.get("count", 0),
                               links=f"{h.get('avg_links') or 0:.1f}",
                               speed=util.human_size(h.get("avg_speed") or 0)))
            return 0
        if a.clear:
            r = c.request("history-clear", older_than=a.older_than)
            if not r.get("ok"):
                print(_("cli_error") + ":", r.get("error", ""), file=sys.stderr)
                return 1
            print(_("hist_cleared", n=r.get("removed", 0)))
            return 0
        if a.redownload:
            r = c.request("history-redownload", id=a.redownload)
            if not r.get("ok"):
                print(_("cli_error") + ":", r.get("error", ""), file=sys.stderr)
                return 1
            print(f"{_('cli_added')}: {r['id']}  ->  {r['filename']}")
            return 0
        r = c.request("history", q=a.query or "", state=a.state or "",
                      limit=a.limit)
    if not r.get("ok"):
        print(_("cli_error") + ":", r.get("error", ""), file=sys.stderr)
        return 1
    items = r.get("items") or []
    if not items:
        print(_("hist_empty"))
        return 0
    for it in items:
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(it.get("finished_at") or 0))
        print(f"{it['id']:<10} {_('st_' + it['state']):<10} "
              f"{util.human_size(it.get('bytes')):>9}  {when}  "
              f"{it.get('filename', '')}")
    return 0


def cmd_hosts(a, env):
    """Ogrenilen ana makine profilleri: listele / unut / tumunu sil."""
    with _client(env) as c:
        if a.clear:
            r = c.request("hosts", clear=True)
            if not r.get("ok"):
                print(_("cli_error") + ":", r.get("error", ""), file=sys.stderr)
                return 1
            print(_("hosts_cleared", n=r.get("removed", 0)))
            return 0
        if a.forget:
            r = c.request("hosts", forget=a.forget)
            if not r.get("ok"):
                print(_("hosts_unknown", host=a.forget), file=sys.stderr)
                return 1
            print(_("hosts_forgotten", host=a.forget))
            return 0
        r = c.request("hosts")
    if not r.get("ok"):
        print(_("cli_error") + ":", r.get("error", ""), file=sys.stderr)
        return 1
    rows = r.get("hosts") or []
    if not rows:
        print(_("hosts_empty"))
        return 0
    for h in rows:
        band = _("rtt_" + h["rtt_band"]) if h.get("rtt_band") else "?"
        seen = time.strftime("%Y-%m-%d",
                             time.localtime(h.get("last_seen") or 0))
        print(_("hosts_row", host=h.get("host", ""),
                links=h.get("links", 0), rtt=band,
                speed=util.human_size(h.get("per_link_bps") or 0),
                samples=h.get("samples", 0), seen=seen))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ferdim",
        description="FerDim - dinamik segmentli indirme yoneticisi")
    p.add_argument("--version", action="version",
                   version=f"FerDim {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("add", help="URL indir")
    sp.add_argument("url")
    sp.add_argument("-o", "--output", help="dosya adi")
    sp.add_argument("-d", "--dir", help="hedef dizin")
    sp.add_argument("-n", "--segments", type=int, help="baglanti sayisi")
    sp.add_argument("-H", "--header", action="append", default=[],
                    help='\"Ad: Deger\" (tekrarlanabilir)')
    sp.add_argument("-c", "--cookie", help="Cookie basligi")
    sp.add_argument("-r", "--referer", help="Referer")
    sp.add_argument("-u", "--user", help="HTTP kullanicisi")
    sp.add_argument("-p", "--password", help="HTTP sifresi")
    sp.add_argument("--proxy", help="proxy URL (http, https, socks5, socks5h)")
    sp.add_argument("--limit", help="is basina hiz limiti (orn 2MB)")
    sp.add_argument("-k", "--insecure", action="store_true",
                    help="TLS sertifikasi dogrulama")
    sp.add_argument("--wait", action="store_true", help="bitmesini bekle")
    sp.add_argument("--sha256", help="bitiste SHA-256 dogrulamasi")
    sp.add_argument("--md5", help="bitiste MD5 dogrulamasi")
    sp.add_argument("--sha1", help="bitiste SHA-1 dogrulamasi")
    sp.add_argument("--sha512", help="bitiste SHA-512 dogrulamasi")
    sp.add_argument("--checksum",
                    help="algo:ozet (orn sha256:abc...) ya da uzunlugundan "
                         "anlasilan ozet")
    sp.add_argument("--mirror", action="append", default=[],
                    help="aynak URL (tekrarlanabilir; .metalink otomatik)")
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("add-file",
                        help="dosyadaki URL'leri ya da metalink belgesini "
                             "kuyruga ekle")
    sp.add_argument("files", nargs="+")
    sp.set_defaults(fn=cmd_add_file)

    sp = sub.add_parser("list", aliases=["ls"], help="kuyrugu listele")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_list)

    for name, help_ in (("pause", "durdur"), ("resume", "devam"),
                        ("cancel", "iptal"), ("restart", "bastan indir")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("id", help="is kimligi veya all")
        sp.set_defaults(fn=cmd_control(name))

    sp = sub.add_parser("remove", help="kuyruktan sil")
    sp.add_argument("id")
    sp.add_argument("--delete-files", action="store_true")
    sp.set_defaults(fn=cmd_remove)

    sp = sub.add_parser("limit", help="global hiz limiti (0 = sinirsiz)")
    sp.add_argument("value", help="orn 2MB / 0")
    sp.set_defaults(fn=cmd_limit)

    sp = sub.add_parser("turbo", help="azami guc: hatti doyur (on/off/status)")
    sp.add_argument("action", choices=["on", "off", "status"], nargs="?",
                    default="status")
    sp.set_defaults(fn=cmd_turbo)

    sp = sub.add_parser("config", help="yapilandirmayi goster/degistir")
    sp.add_argument("--set", help='JSON, orn \'{"segments":16}\'')
    sp.set_defaults(fn=cmd_config)

    sp = sub.add_parser("history",
                        help="indirme gecmisi: ara, yeniden indir, istatistik")
    sp.add_argument("query", nargs="?", default="", help="metin aramasi")
    sp.add_argument("--state", choices=["done", "error", "canceled"],
                    help="durum suzgeci")
    sp.add_argument("--limit", type=int, default=50, help="en cok kayit")
    sp.add_argument("--stats", action="store_true",
                    help="toplam bayt, kayit ve sunucu basina ortalamalar")
    sp.add_argument("--redownload", metavar="ID",
                    help="kaydi ayni URL/ayarlarla yeniden kuyruga al")
    sp.add_argument("--clear", action="store_true", help="gecmisi sil")
    sp.add_argument("--older-than", type=int, metavar="DAYS",
                    help="--clear ile: yalniz bu gunden eski kayitlar")
    sp.set_defaults(fn=cmd_history)

    sp = sub.add_parser("hosts",
                        help="ogrenilen ana makine profilleri (baglanti tavani)")
    sp.add_argument("--forget", metavar="HOST", help="tek profili unut")
    sp.add_argument("--clear", action="store_true", help="tum profilleri sil")
    sp.set_defaults(fn=cmd_hosts)

    sp = sub.add_parser("daemon", help="arka plan servisini on planda calistir")
    sp.set_defaults(fn=cmd_daemon)

    sp = sub.add_parser("web", help="web arayuzunu ac")
    sp.set_defaults(fn=cmd_web)
    return p


def main() -> int:
    env = AppEnv()
    args = build_parser().parse_args()
    try:
        return args.fn(args, env)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
