"""ferdim komut satiri arayuzu."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__, util
from .client import FerdimClient, ensure_daemon
from .i18n import detect_lang, t as _
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


def cmd_add(a, env):
    with _client(env) as c:
        r = c.request("add", url=a.url, filename=a.output, dir=a.dir,
                      segments=a.segments, headers=a.header, cookies=a.cookie,
                      user=a.user, password=a.password, proxy=a.proxy,
                      insecure=a.insecure, referer=a.referer,
                      limit=a.limit)
        if not r.get("ok"):
            print(_("cli_error") + ":", r.get("error"), file=sys.stderr)
            return 1
        print(f"{_('cli_added')}: {r['id']}  ->  {r['filename']}")
        if a.wait:
            return _wait(c, r["id"])
    return 0


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
                print(_("st_error") + ":", job["error"])
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
    sp.add_argument("--proxy", help="proxy URL")
    sp.add_argument("--limit", help="is basina hiz limiti (orn 2MB)")
    sp.add_argument("-k", "--insecure", action="store_true",
                    help="TLS sertifikasi dogrulama")
    sp.add_argument("--wait", action="store_true", help="bitmesini bekle")
    sp.set_defaults(fn=cmd_add)

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
