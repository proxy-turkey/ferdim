"""ferdim komut satiri arayuzu."""
from __future__ import annotations

import argparse
import json
import os
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


def cmd_coexist(a, env):
    """Birlikte yasam modu: hat mesgulken baglanti birak (varsayilan kapali)."""
    with _client(env) as c:
        if a.action == "status":
            r = c.request("coexist")
        else:
            r = c.request("coexist", on=(a.action == "on"))
    on = bool(r.get("enabled"))
    print(f"{_('coexist')}: " + _("coexist_on" if on else "coexist_off"))
    if on:
        if r.get("active"):
            print(_("coexist_active"))
        print(_("coexist_hint"))
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


def _abs_merkle(value: str) -> str:
    """Yerel merkle bildirimi mutlak yola cevrilir: istegi daemon yurutur ve
    onun calisma dizini istemcininki degildir (goreli yol yanlis yeri
    gosterip isi merkle_manifest ile bitirirdi). URL'ler aynen kalir."""
    v = str(value or "").strip()
    if v and not v.startswith(("http://", "https://")):
        return os.path.abspath(os.path.expanduser(v))
    return v


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
                      insecure=a.insecure, http3=a.http3 or "",
                      referer=a.referer,
                      limit=a.limit, mirrors=a.mirror,
                      sha256=a.sha256, md5=a.md5,
                      sha1=a.sha1, sha512=a.sha512, checksums=checks,
                      merkle=_abs_merkle(a.merkle))
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
        # uyari kodu (or. yumusak 404) satirin sonunda gorunur: 'done' gorunen
        # bir is sessizce supheli kalmasin
        warn = str(j.get("warning") or "")
        tail = f"  ! {_('warn_' + warn)}" if warn else ""
        print(f"{j['id']:<10} {_('st_' + j['state']):<14} "
              f"{util.human_size(j['done']):>9}/{util.human_size(j['size']):<9} "
              f"{util.human_size(j['speed']):>10}/sn  {j['filename']}{tail}")
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


def _web_failure(r: dict) -> str:
    """web-session hatasini kullaniciya anlasilir metne cevir."""
    if r.get("code") == "web_disabled":
        return _("launch_web_disabled")
    return _("launch_web_failed")


def cmd_web(a, env):
    """Web arayuzunu tek kullanimlik URL ile acar (jeton adreste tasinmaz).

    URL her cagrida yeniden alinir: nonce tek kullanimlidir ve 120 sn
    sonra duser, bu yuzden onbelleklenmez."""
    import subprocess
    with _client(env) as c:
        r = c.request("web-session")
    url = str(r.get("url") or "")
    if not r.get("ok") or not url:
        print(f"{_('cli_error')}: {_web_failure(r)}", file=sys.stderr)
        return 1
    print(url)
    try:
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass                        # URL yazdirildi; tarayici yoksa da is gorur
    return 0


def cmd_token(a, env):
    """API jetonunu yeniler; eski tek kullanimlik web oturumlari duser."""
    with _client(env) as c:
        r = c.request("token-rotate")
    if not r.get("ok"):
        print(_("cli_error") + ":",
              r.get("error") or r.get("code") or _("launch_token_failed"),
              file=sys.stderr)
        return 1
    print(_("launch_token_rotated"))
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


def cmd_archive(a, env):
    """Arsiv komutlari: listele, secmeli cikar, yarim kalan cikarmayi onar."""
    with _client(env) as c:
        if a.action == "list":
            r = c.request("archive-list", id=a.id, nested=bool(a.nested))
            if not r.get("ok"):
                print(_("cli_error") + ":", r.get("error", ""), file=sys.stderr)
                return 1
            for e in r.get("entries") or []:
                mark = "*" if e.get("kind") != "file" else " "
                print(f"{mark} {util.human_size(e.get('size') or 0):>10}  "
                      f"{e.get('path', '')}")
            if r.get("note"):
                print(r["note"])
            return 0
        dest = os.path.abspath(os.path.expanduser(a.dest)) if a.dest else ""
        if a.action == "extract":
            r = c.request("archive-extract", id=a.id, members=a.members,
                          dest=dest, force=bool(a.force))
        else:
            r = c.request("archive-repair", id=a.id, dest=dest,
                          force=bool(a.force))
        if not r.get("ok"):
            if r.get("code") == "no_manifest":
                print(_("zip_nomf"), file=sys.stderr)
            else:
                print(_("cli_error") + ":", r.get("error", ""), file=sys.stderr)
            return 1
        while True:
            time.sleep(0.3)
            st = c.request("archive-status", task=r.get("task", ""))
            if not st.get("ok"):
                print(_("cli_error") + ":", st.get("error", ""), file=sys.stderr)
                return 1
            if st.get("state") == "running":
                if st.get("total"):
                    sys.stdout.write("\r" + _("zip_running", done=st.get("done", 0),
                                              total=st.get("total", 0)) + "   ")
                    sys.stdout.flush()
                continue
            sys.stdout.write("\r" + " " * 48 + "\r")
            res = st.get("result") or {}
            if st.get("state") != "done":
                errs = res.get("errors") or []
                msg = st.get("error") or (errs[0] if errs else "")
                print(_("zip_failed", e=msg), file=sys.stderr)
                return 1
            print(_("zip_repaired" if res.get("repair") else "zip_extracted",
                    n=res.get("extracted", 0), dest=res.get("dest", "")))
            errs = res.get("errors") or []
            if errs:
                print(_("zip_errors", n=len(errs), e=errs[0]))
            return 0


def cmd_merkle(a, env):
    """Parca bazli butunluk: bildirim uret / dogrula / degisen parcalari indir.

    make ve verify yereldir (daemon gerekmez); update tek akisli HTTP Range
    kullanir ve yalnizca yerel dosyada eslesmeyen parcalari indirir."""
    from . import cdc
    if a.action == "make":
        try:
            man = cdc.make_manifest(a.file)
            out = a.out or str(a.file) + cdc.SUFFIX
            cdc.write_manifest(man, out)
        except (OSError, ValueError) as e:
            print(_("cli_error") + ":", e, file=sys.stderr)
            return 1
        print(f"{_('merkle_manifest')}: {out}")
        print(f"{_('merkle_chunks')}: {len(man.chunks)}")
        print(f"{_('merkle_root')}: {man.root}")
        return 0
    if a.action == "verify":
        try:
            man = cdc.read_manifest(a.manifest or str(a.file) + cdc.SUFFIX)
            size_ok, bad = cdc.verify_file(a.file, man)
        except (OSError, ValueError) as e:
            print(_("cli_error") + ":", e, file=sys.stderr)
            return 1
        if not size_ok:
            print(_("merkle_size_bad"), file=sys.stderr)
            return 1
        if bad:
            print(_("merkle_bad", n=len(bad)), file=sys.stderr)
            for off, ln in bad:
                print(f"  {off}-{off + ln - 1}")
            return 1
        print(f"{_('merkle_root')}: {man.root}")
        print(_("merkle_ok"))
        return 0
    from .engine import httpcli
    opt = httpcli.DlOptions(user_agent=f"FerDim/{__version__}", timeout=30)
    try:
        man = cdc.read_manifest(a.manifest)
        res = cdc.update_file(a.file, a.url, man, a.out, opt)
    except (OSError, ValueError) as e:
        print(_("cli_error") + ":", e, file=sys.stderr)
        return 1
    print(res["path"])
    print(_("merkle_update", reused=util.human_size(res["reused"]),
            fetched=util.human_size(res["fetched"])))
    print(f"{_('merkle_chunks')}: {res['chunks']}")
    print(f"{_('merkle_root')}: {res['root']}")
    return 0


def cmd_publish(a, env):
    """Yayin dizini: dosya basina merkle/sha256/meta4 + tek index.meta4."""
    from . import publish
    try:
        res = publish.publish(a.dir, a.base_url, a.mirror, a.glob,
                              merkle=not a.no_merkle, force=bool(a.force))
    except (OSError, ValueError) as e:
        print(_("cli_error") + ":", e, file=sys.stderr)
        return 1
    for f in res["files"]:
        print(f"{_('cli_file')}: {f['name']}  ({util.human_size(f['size'])})")
        if f["merkle_path"]:
            print(f"  {f['merkle_path']}")
            print(f"  {_('merkle_chunks')}: {f['merkle_chunks']}  "
                  f"{_('merkle_root')}: {f['merkle_root']}")
        print(f"  {f['sha256_path']}")
        print(f"  {f['meta4_path']}")
    print(res["index"])
    return 0


def _plan_limit_text(n) -> str:
    """Plan limiti metni: '0'/yok = sinirsiz, aksi halde 1024 tabanli gosterim."""
    return util.human_size(n) + "/sn" if n else _("plan_unlimited")


def _plan_rule_text(rule: dict) -> str:
    days = rule.get("days") or _("plan_every_day")
    hours = (f"{rule.get('from', '')}-{rule.get('to', '')}"
             if rule.get("from") or rule.get("to") else _("plan_all_day"))
    limit = rule.get("limit")
    text = _("plan_unlimited") if not limit or str(limit) == "0" \
        else str(limit) + "/sn"
    return _("plan_rule", days=days, hours=hours, limit=text)


def cmd_speedplan(a, env):
    """Saat bazli hiz plani: goster / JSON ile kur / sil / denetimi geri al."""
    action = a.action or "show"
    with _client(env) as c:
        if action == "set":
            if not a.json_plan:
                print(_("cli_error") + ": " + _("plan_bad"), file=sys.stderr)
                return 2
            try:
                plan = json.loads(a.json_plan)
            except ValueError as e:
                print(_("cli_error") + ": " + _("plan_bad") + f" ({e})",
                      file=sys.stderr)
                return 2
            r = c.request("speedplan", action="set", plan=plan)
        elif action in ("clear", "resume"):
            r = c.request("speedplan", action=action)
        else:
            r = c.request("speedplan")
    if not r.get("ok"):
        print(_("cli_error") + ":", r.get("error", ""), file=sys.stderr)
        return 1
    plan = r.get("plan") or []
    active = r.get("active_rule") or {}
    print(_("plan_title") + ":")
    if not plan:
        print("  " + _("plan_state_none"))
    for i, rule in enumerate(plan):
        mark = "  * " if active.get("index") == i else "    "
        print(mark + _plan_rule_text(rule))
    if r.get("manual"):
        print("  " + _("plan_state_manual"))
    elif plan:
        if active:
            print("  " + _("plan_state_active",
                           limit=_plan_limit_text(active.get("limit"))))
        else:
            print("  " + _("plan_state_off"))
    if action == "clear":
        print(_("plan_cleared"))
    elif action == "resume":
        print(_("plan_resumed"))
    elif action == "set":
        print(_("plan_saved"))
    return 0


def cmd_metrics(a, env):
    """Prometheus metin bicimini yazdir (daemon ayni metni /metrics'te sunar)."""
    with _client(env) as c:
        r = c.request("metrics")
    if not r.get("ok"):
        print(_("cli_error") + ":", r.get("error", ""), file=sys.stderr)
        return 1
    sys.stdout.write(r.get("text") or "")
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
    sp.add_argument("--http3", choices=("off", "auto", "force"),
                    help="HTTP/3 (QUIC) tasima: aioquic gerekir")
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
    sp.add_argument("--merkle",
                    help="parca bazli dogrulama bildirimi (URL ya da yerel yol;"
                         " yalniz HTTP(S) isleri)")
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

    sp = sub.add_parser("coexist",
                        help="birlikte yasam: hat mesgulken baglanti birak "
                             "(on/off/status)")
    sp.add_argument("action", choices=["on", "off", "status"], nargs="?",
                    default="status")
    sp.set_defaults(fn=cmd_coexist)

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
    sp = sub.add_parser("archive",
                        help="arsiv icerigi: listele, cikar, onar")
    asp = sp.add_subparsers(dest="action", required=True)
    for act, hlp in (("list", "icerigi listele"),
                     ("extract", "secilenleri cikar"),
                     ("repair", "yarim kalan cikarmayi onar")):
        one = asp.add_parser(act, help=hlp)
        one.add_argument("id", help="is kimligi")
        if act == "list":
            one.add_argument("--nested", action="store_true",
                             help="ic arsivleri de listele")
        else:
            one.add_argument("--dest",
                             help="hedef dizin (mutlak; varsayilan: arsiv yaninda)")
            one.add_argument("--force", action="store_true",
                             help="kayitli olmayan var olan dosyalarin uzerine yaz")
            if act == "extract":
                one.add_argument("members", nargs="*",
                                 help="uye yollari (bos = tumu)")
        one.set_defaults(fn=cmd_archive)

    sp = sub.add_parser("merkle",
                        help="parca bazli butunluk: bildirim uret/dogrula/onar")
    msp = sp.add_subparsers(dest="action", required=True)
    one = msp.add_parser("make", help="FILE.ferdim-merkle bildirimi uret")
    one.add_argument("file")
    one.add_argument("-o", "--out", help="bildirim yolu")
    one.set_defaults(fn=cmd_merkle)
    one = msp.add_parser("verify", help="dosyayi bildirimle dogrula")
    one.add_argument("file")
    one.add_argument("manifest", nargs="?",
                     help="bildirim (varsayilan: FILE.ferdim-merkle)")
    one.set_defaults(fn=cmd_merkle)
    one = msp.add_parser("update",
                         help="eski dosyayi koru, yalnizca degisen parcalari indir")
    one.add_argument("file")
    one.add_argument("url")
    one.add_argument("--manifest", required=True, help="yeni bildirim")
    one.add_argument("-o", "--out", help="cikti (varsayilan: FILE)")
    one.set_defaults(fn=cmd_merkle)

    sp = sub.add_parser("daemon", help="arka plan servisini on planda calistir")
    sp.set_defaults(fn=cmd_daemon)

    sp = sub.add_parser("web", help="web arayuzunu ac")
    sp.set_defaults(fn=cmd_web)

    sp = sub.add_parser("token", help="API jetonu islemleri")
    tsp = sp.add_subparsers(dest="action", required=True)
    one = tsp.add_parser("rotate", help="web API jetonunu yenile")
    one.set_defaults(fn=cmd_token)

    sp = sub.add_parser("publish",
                        help="yayin dizini: merkle/sha256/meta4 uret")
    sp.add_argument("dir", help="yayin dizini (duz; alt dizinlere inilmez)")
    sp.add_argument("--base-url", required=True,
                    help="birincil indirme adresi (http/https)")
    sp.add_argument("--mirror", action="append", default=[],
                    help="ayna adresi (tekrarlanabilir)")
    sp.add_argument("--glob", metavar="PATTERN",
                    help="dosya adi deseni (orn '*.iso')")
    sp.add_argument("--no-merkle", action="store_true",
                    help="parca bildirimi (.ferdim-merkle) uretme")
    sp.add_argument("--force", action="store_true",
                    help="guncel olsa da bildirimleri yeniden uret")
    sp.set_defaults(fn=cmd_publish)

    # --- 1.8: saat bazli hiz plani + Prometheus metrikleri ---
    sp = sub.add_parser("speedplan",
                        help="saat bazli hiz plani (goster/set/clear/resume)")
    sp.add_argument("action", nargs="?", default="show",
                    choices=["show", "set", "clear", "resume"],
                    help="JSON kur, plani sil ya da denetimi plana geri ver")
    sp.add_argument("json_plan", nargs="?", metavar="JSON",
                    help='kural listesi, orn '
                         '\'[{"days":"mon-fri","from":"09:00",'
                         '"to":"18:00","limit":"2MB"}]\'')
    sp.set_defaults(fn=cmd_speedplan)

    sp = sub.add_parser("metrics",
                        help="Prometheus metin bicimini yazdir")
    sp.set_defaults(fn=cmd_metrics)
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
