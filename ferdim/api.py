"""Komut tasiyicilari: socket ve HTTP sunucularinin ortak API katmani."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .engine.job import JobSpec
from . import util
from .history import STATES as HISTORY_STATES
from .i18n import detect_lang, dict_for
from . import metalink as _ml
from .profiles import open_profiles
from .watch import MAX_FILE, parse_url_list

MAX_MIRRORS = 24              # metalink belgeleri sikca 10-20 ayna listeler
MAX_METALINK_FILES = 64
_MAX_INT = {"segments": (1, 32, 8), "max_concurrent": (1, 32, 8),
            "retries": (0, 20, 5), "timeout": (5, 300, 30),
            "stall_timeout": (5, 120, 20), "web_port": (1024, 65535, 8378),
            "history_days": (0, 3650, 365)}


def _default_segments(cfg: dict) -> int:
    if cfg.get("turbo_mode"):
        return int(cfg.get("turbo_segments", 32))
    return int(cfg.get("segments", 8))


def _spec_from(params: dict, cfg: dict) -> JobSpec:
    headers = {}
    for h in params.get("headers") or []:
        if isinstance(h, str) and ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()
    # saglamalar: sha256/md5/sha1/sha512 alanlari + checksums sozlugu +
    # 'algo:ozet' ya da uzunlugundan anlasilan tek giris (checksum)
    checks = util.normalize_checksums(params.get("checksums") or {})
    algo, digest = util.parse_checksum_spec(params.get("checksum") or "")
    if algo:
        checks[algo] = digest
    for a in ("sha256", "md5", "sha1", "sha512"):
        checks.update(util.normalize_checksums({a: params.get(a) or ""}))
    try:
        segments = int(params.get("segments") or _default_segments(cfg))
    except (TypeError, ValueError):
        segments = _default_segments(cfg)
    return JobSpec(
        url=params.get("url", ""),
        filename=util.sanitize_filename(params["filename"])
        if params.get("filename") else "",
        mirrors=[u for u in (params.get("mirrors") or [])
                 if isinstance(u, str)
                 and u.startswith(("http://", "https://"))][:MAX_MIRRORS],
        sha256=checks.pop("sha256", ""),
        md5=checks.pop("md5", ""),
        checksums=checks,
        directory=params.get("dir") or params.get("directory") or "",
        segments=max(1, min(32, segments)),
        headers=headers,
        cookies=params.get("cookies") or "",
        user=params.get("user") or "",
        password=params.get("password") or "",
        proxy=params.get("proxy") or "",
        insecure=bool(params.get("insecure")),
        http3=(str(params.get("http3") or "")
               if params.get("http3") in ("off", "auto", "force") else ""),
        referer=params.get("referer") or params.get("page") or "",
        user_agent=params.get("ua") or params.get("user_agent") or "",
        limit=util.parse_size(params.get("limit")) if params.get("limit") else 0,
        merkle=str(params.get("merkle") or "").strip()[:2048],
    )


def _sanitize_config(cfg: dict, env=None) -> None:
    """Yapilandirmayi guvenli araliga ceker: bozuk bir deger (el ile
    duzenlenmis config.json, bozuk API cagrisi) daemon'u devirmemeli.
    env verilirse indirme dizini de denenir; acilamiyorsa varsayilan."""
    for key, (lo, hi, default) in _MAX_INT.items():
        try:
            cfg[key] = max(lo, min(hi, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            cfg[key] = default
    cfg["stall_timeout"] = min(cfg["stall_timeout"], cfg["timeout"])
    try:
        cfg["speed_limit"] = max(0, int(cfg.get("speed_limit", 0) or 0))
    except (TypeError, ValueError):
        cfg["speed_limit"] = 0
    try:
        cfg["watch_interval"] = max(1.0, min(60.0, float(cfg.get("watch_interval", 3))))
    except (TypeError, ValueError):
        cfg["watch_interval"] = 3
    if cfg.get("watch_done") not in ("delete", "rename", "keep"):
        cfg["watch_done"] = "delete"
    if cfg.get("http3") not in ("off", "auto", "force"):
        cfg["http3"] = "off"
    # "learn_hosts": elle duzenlenen config.json'da metin de olabilir
    lh = cfg.get("learn_hosts", True)
    cfg["learn_hosts"] = lh if isinstance(lh, bool) else \
        str(lh).strip().lower() not in ("0", "false", "no", "off")
    # masaustu bildirimleri: elle duzenlenmis "false" metni de kapali sayilir
    for _k, _default in (("notify", True), ("notify_errors_only", False)):
        _v = cfg.get(_k, _default)
        if isinstance(_v, str):
            _v = _v.strip().lower() not in ("", "0", "false", "no", "off")
        cfg[_k] = bool(_v)
    # "coexist": ayni sekilde metin kabul edilir; esik 0.02-0.9 arasinda
    cv = cfg.get("coexist", False)
    cfg["coexist"] = cv if isinstance(cv, bool) else \
        str(cv).strip().lower() not in ("0", "false", "no", "off", "")
    try:
        cfg["coexist_threshold"] = max(
            0.02, min(0.9, float(cfg.get("coexist_threshold", 0.15))))
    except (TypeError, ValueError):
        cfg["coexist_threshold"] = 0.15
    # gecikme sinyali esigi (guncel TTFB / taban): 1.2-20 arasi
    try:
        cfg["coexist_latency"] = max(
            1.2, min(20.0, float(cfg.get("coexist_latency", 2.5))))
    except (TypeError, ValueError):
        cfg["coexist_latency"] = 2.5
    dirs = cfg.get("watch_dirs")
    cfg["watch_dirs"] = [d for d in dirs if isinstance(d, str) and d.strip()] \
        if isinstance(dirs, list) else []
    sch = cfg.get("schedule")
    if not isinstance(sch, dict):
        sch = cfg["schedule"] = {}
    if not isinstance(sch.get("categories", {}), dict):
        sch["categories"] = {}
    hm = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
    for name in list(sch.get("categories") or {}):
        w = sch["categories"].get(name)
        if not isinstance(w, dict) or not (hm.match(str(w.get("start", "")))
                                           and hm.match(str(w.get("stop", "")))):
            del sch["categories"][name]     # bozuk pencere = kisit yok
    d = cfg.get("download_dir")
    cfg["download_dir"] = d if isinstance(d, str) and d.strip() else None
    if env is not None:
        _recheck_download_dir({"env": env, "cfg": cfg})


def _recheck_download_dir(ctx) -> str:
    """Indirme dizini ayari degisti: onbellegi bosa cikarir. Dizin
    olusturulamiyorsa (yetki yok, dosya uzerinde yol) varsayilana doner ve
    'download_dir' uyari kodunu dondurur; yoksa bos."""
    env, cfg = ctx["env"], ctx["cfg"]
    env.reload_download_dir(cfg)      # dizin cfg'den okunur (dosyadan degil)
    try:
        env.download_dir
    except OSError:
        cfg["download_dir"] = None
        env.reload_download_dir(cfg)
        return "download_dir"
    return ""


def _bad_checksum(params: dict) -> bool:
    """Kullanicinin elle verdigi saglama degerlerinden biri gecersiz mi?
    (metalink belgesinden gelenler degil: onlar yalniz elenir.) Seslice
    yok sayilsaydi kullanici dogrulamanin yapildigini sanirdi."""
    chk = params.get("checksum")
    if isinstance(chk, str) and chk.strip() and \
            not util.parse_checksum_spec(chk)[0]:
        return True
    for algo in ("sha256", "md5", "sha1", "sha512"):
        v = params.get(algo)
        if isinstance(v, str) and v.strip() and \
                not util.normalize_checksums({algo: v}):
            return True
    return False


def _metalink_root_guard(items: list, base: dict, cfg: dict) -> str:
    """metalink icindeki Merkle kokunun indirilen bildirimle ayni oldugunu
    dogrular (bos dize: sorun yok).

    Kullanicinin verdigi belge acik bir kaynaktir: bildirim `--merkle` gibi
    KESIN sayilir, uyusmazsa is hata olur. Belgedeki kok ayrica bildirimin
    kendi kokuyla ayni olmalidir; tutmazsa dosya kuyruga hic alinmaz
    (yanlis eslesme "dogrulandi" demek olurdu). Bildirim bu anda
    okunamiyorsa is yine kuyruga girer: motor ayni sorunu merkle_manifest
    koduyla bildirir."""
    need = [it for it in items[:MAX_METALINK_FILES]
            if it.get("merkle") and it.get("merkle_root")]
    if not need:
        return ""
    from . import cdc
    from .engine import httpcli as _hc
    try:
        opt = _spec_from(base, cfg).options(cfg)
    except (TypeError, ValueError, KeyError):
        return ""                    # secenekler kurulamadi: motor karar verir
    for it in need:
        try:
            text = _hc.fetch_small(
                it["merkle"], opt,
                timeout=min(max(int(opt.timeout), 2), 8),
                limit=cdc.MAX_MANIFEST)
            man = cdc.Manifest.parse(text)
        except OSError as e:            # ag hatasi / HTTP 4xx-5xx
            close = getattr(e, "close", None)
            if close is not None:
                close()                # urllib HTTPError'i soketi birakmaz
            continue                   # motor ayni sorunu bildirir
        except ValueError:
            continue                   # cozulemeyen bildirim: motor bildirir
        if man.root != it["merkle_root"]:
            return (f"{it['filename']}: merkle koku bildirimle uyusmuyor")
    return ""


def _add_metalink_items(ctx, items: list, base: dict) -> dict:
    """Ayristirilmis metalink dosyalarini kuyruga ekler. 'base' icindeki
    dizin, kimlik, hiz limiti gibi ayarlar her dosyaya tasinir."""
    keep = {k: v for k, v in (base or {}).items()
            if k not in ("url", "filename", "mirrors", "sha256", "md5",
                         "sha1", "sha512", "checksums", "checksum", "merkle")}
    err = _metalink_root_guard(items, keep, ctx["cfg"])
    if err:
        return {"ok": False, "error": f"metalink: {err}"}
    ids, first = [], None
    for it in items[:MAX_METALINK_FILES]:
        checks = it.get("hashes") or {"sha256": it.get("sha256", ""),
                                      "md5": it.get("md5", "")}
        add = {**keep, "url": it["urls"][0],
               "mirrors": it["urls"][1:MAX_MIRRORS],
               "filename": it["filename"], "checksums": checks}
        if it.get("merkle"):
            # belgeye ait bildirim: dosya icin acikca verilmis sayilir
            add["merkle"] = it["merkle"]
        r = handle(ctx, "add", add)
        if r.get("ok"):
            ids.append(r["id"])
            first = first or r
    if not first:
        return {"ok": False, "error": "metalink bos"}
    return {**first, "ids": ids, "metalink": len(items)}


def _add_remote_metalink(ctx, params: dict) -> dict:
    from .engine import httpcli as _hc
    cfg = ctx["cfg"]
    try:
        opt = _spec_from(params, cfg).options(cfg)
        # metalink belgeleri bazi sunucularda yanlis Content-Type ile gelir
        text = _hc.fetch_small(params["url"], opt, allow_html=True,
                               timeout=min(max(int(opt.timeout), 2), 30),
                               limit=MAX_FILE)
    except Exception as e:
        return {"ok": False, "error": f"metalink: {e}"}
    try:
        items = _ml.parse(text)
    except ValueError as e:
        return {"ok": False, "error": f"metalink: {e}"}
    return _add_metalink_items(ctx, items, params)


def _add_local_file(ctx, path) -> dict:
    """Yerel dosyayi kuyruga ekler: .metalink/.meta4 belgesi ya da her
    satirinda URL olan metin. Dosya icerigi disari verilmez; yalniz
    kuyruga eklenen islerin kimlikleri doner."""
    try:
        p = Path(str(path)).expanduser()
        if not p.is_file():
            return {"ok": False, "error": "dosya yok"}
        if p.stat().st_size > MAX_FILE:
            return {"ok": False, "error": "dosya cok buyuk"}
        text = p.read_text("utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": f"dosya okunamadi: {e}"}
    if p.suffix.lower() in (".metalink", ".meta4") \
            or "<metalink" in text[:4096].lower():
        try:
            items = _ml.parse(text)
        except ValueError as e:
            return {"ok": False, "error": f"metalink: {e}"}
        r = _add_metalink_items(ctx, items, {})
        if r.get("ok"):
            r["count"] = len(r["ids"])
        return r
    urls = parse_url_list(text)
    if not urls:
        return {"ok": False, "error": "gecerli URL yok"}
    ids = []
    for u in urls:
        r = handle(ctx, "add", {"url": u})
        if r.get("ok"):
            ids.append(r["id"])
    if not ids:
        return {"ok": False, "error": "eklenemedi"}
    return {"ok": True, "ids": ids, "count": len(ids)}


def _archive_cmd(qm, cmd: str, params: dict) -> dict:
    """Arsiv komutlari: listeleme, secmeli cikarma, onarim ve gorev durumu.
    Cikarma arka planda calisir; istemci 'archive-status' ile izler."""
    from . import archive
    if cmd == "archive-status":
        return archive.task_status(str(params.get("task") or ""))
    job = qm.get(str(params.get("id") or ""))
    if not job or job.state != "done":
        return {"ok": False, "error": "is yok"}
    path = job.final_path
    if cmd == "archive-list":
        try:
            info = archive.describe(path, nested=bool(params.get("nested")))
        except archive.ArchiveError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "filename": job.final_filename,
                "format": info["format"], "entries": info["entries"],
                "note": info["note"]}
    raw_dest = str(params.get("dest") or "").strip()
    if raw_dest and not os.path.isabs(os.path.expanduser(raw_dest)):
        # daemon'un calisma dizini istemcininki degil: goreli yol belirsiz
        return {"ok": False, "error": "hedef dizin mutlak olmali"}
    dest = archive.default_dest(path, raw_dest or None)
    force = bool(params.get("force"))
    if cmd == "archive-repair":
        if not archive.has_manifest(dest):
            return {"ok": False, "code": "no_manifest",
                    "error": "cikarma kaydi yok"}
        try:
            snap = archive.start_extract(path, [], dest, repair=True,
                                         force=force)
        except archive.ArchiveError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "task": snap["id"], "dest": snap["dest"],
                "state": snap["state"]}
    members = params.get("members") or []
    if not isinstance(members, list):
        return {"ok": False, "error": "gecersiz uye listesi"}
    try:
        snap = archive.start_extract(path, [str(m) for m in members], dest,
                                     force=force)
    except archive.ArchiveError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "task": snap["id"], "dest": snap["dest"],
            "state": snap["state"]}


def handle(ctx, cmd: str, params: dict) -> dict:
    """ctx: {qm, env, cfg, set_cfg} sozlugu; yanit sozlugu dondurur."""
    qm, env, cfg = ctx["qm"], ctx["env"], ctx["cfg"]

    if cmd == "ping":
        return {"ok": True, "turbo": bool(cfg.get("turbo_mode")),
                "segments": _default_segments(cfg)}
    if cmd == "version":
        from . import __version__
        return {"ok": True, "version": __version__}
    if cmd == "i18n":
        lang = params.get("lang") or cfg.get("language")
        return {"ok": True, "lang": detect_lang(lang), "strings": dict_for(
            detect_lang(lang))}
    if cmd == "turbo":
        want = params.get("on")
        if want is None:
            return {"ok": True,
                    "turbo": bool(cfg.get("turbo_mode"))}
        state = qm.set_turbo(bool(want))
        return {"ok": True, "turbo": state}
    if cmd == "coexist":
        want = params.get("on")
        if want is None:
            return {"ok": True, **qm.coexist_status()}
        cfg["coexist"] = bool(want)
        ctx["set_cfg"](cfg)
        qm._coexist_tick()      # tavan hemen kurulur/birakilir
        return {"ok": True, **qm.coexist_status()}
    if cmd == "stats" or cmd == "list" or cmd == "jobs":
        return {"ok": True, **qm.stats()}
    if cmd in ("archive-list", "archive-extract", "archive-repair",
               "archive-status"):
        return _archive_cmd(qm, cmd, params)
    if cmd == "zip-preview":
        job = qm.get(params.get("id", ""))
        if not job or job.state != "done":
            return {"ok": False, "error": "is yok"}
        entries, note = util.zip_preview(job.final_path)
        if entries is None:
            return {"ok": False, "error": note}
        return {"ok": True, "entries": entries, "note": note,
                "filename": job.final_filename}
    if cmd == "add":
        url = params.get("url", "")
        if not isinstance(url, str) or not url.startswith(
                ("http://", "https://", "ftp://", "ftps://")):
            return {"ok": False, "error": "gecersiz URL"}
        if _bad_checksum(params):
            return {"ok": False, "error": "gecersiz saglama degeri"}
        # metalink / .meta4: icindeki dosyalara ac (aynalar + saglamalar)
        if _ml.looks_like_metalink(url):
            return _add_remote_metalink(ctx, params)
        spec = _spec_from(params, cfg)
        job = qm.add(spec, paused=bool(params.get("paused")))
        qm._tick()
        return {"ok": True, "id": job.id, "filename": job.snapshot()["filename"]}
    if cmd == "add-file":
        return _add_local_file(ctx, params.get("path", ""))
    if cmd == "add-batch":
        ids = []
        for item in params.get("items") or []:
            if not isinstance(item, dict):
                continue
            p = dict(params)
            p.update(item)
            r = handle(ctx, "add", p)
            if r.get("ok"):
                ids.append(r["id"])
        return {"ok": True, "ids": ids}
    if cmd in ("pause", "resume", "cancel", "restart"):
        ok = qm.control(params.get("id", ""), cmd,
                        delete_files=bool(params.get("delete_files")))
        return {"ok": ok} if ok else {"ok": False, "error": "is bulunamadi"}
    if cmd == "remove":
        return {"ok": qm.remove(params.get("id", ""),
                                delete_files=bool(params.get("delete_files")))}
    if cmd == "clear-finished":
        qm.clear_finished()
        return {"ok": True}
    if cmd == "history":
        state = str(params.get("state") or "")
        found = qm.history.search(
            str(params.get("q") or params.get("text") or ""),
            state=state if state in HISTORY_STATES else "",
            limit=params.get("limit", 50), offset=params.get("offset", 0))
        if found is None:
            return {"ok": False, "error": "gecmis okunamadi"}
        return {"ok": True, **found}
    if cmd == "history-stats":
        stats = qm.history.stats()
        if stats is None:
            return {"ok": False, "error": "gecmis okunamadi"}
        return {"ok": True, **stats}
    if cmd == "history-redownload":
        rec = qm.history.get(params.get("id", ""))
        if not rec:
            return {"ok": False, "error": "gecmis kaydi yok"}
        again = dict(rec.get("opts") or {})
        again["url"] = rec.get("url", "")      # sifre zaten opts icinde
        result = handle(ctx, "add", again)
        if result.get("ok"):
            result["history"] = rec.get("id", "")
        return result
    if cmd == "history-clear":
        older = params.get("older_than")
        try:
            days = int(older) if older not in (None, "") else 0
        except (TypeError, ValueError):
            days = 0
        removed = qm.history.clear(older_than_days=days if days > 0 else None)
        return {"ok": True, "removed": removed}
    if cmd == "hosts":
        # ogrenilen ana makine profilleri: listele / unut / tumunu sil
        store = open_profiles(env)
        if params.get("clear"):
            return {"ok": True, "removed": store.clear()}
        forget = str(params.get("forget") or "")
        if forget:
            return ({"ok": True, "forgotten": forget}
                    if store.forget(forget)
                    else {"ok": False, "error": "profil yok"})
        rows = store.hosts()
        return {"ok": True, "hosts": rows, "count": len(rows)}
    if cmd == "limit":
        val = params.get("value", 0)
        if isinstance(val, str):
            val = util.parse_size(val)
        val = int(val or 0)
        # turbo "limitsiz" demek: kullanici limit sectiginde turbo kapanir,
        # yoksa dugme TURBO ACIK gorunurken limit uygulanirdi
        if val > 0 and cfg.get("turbo_mode"):
            qm.set_turbo(False)
        qm.set_speed_limit(val)
        return {"ok": True, "speed_limit": qm.cfg["speed_limit"]}
    if cmd == "config-get":
        return {"ok": True, "config": cfg}
    if cmd == "config-set":
        new = params.get("config")
        new = new if isinstance(new, dict) else {}
        for k, v in new.items():
            if k in cfg:
                cfg[k] = v
        _sanitize_config(cfg)
        # turbo acikken girilen limit / eszamanlilik, turbo kapaninca da
        # (turbo_prev geri yuklemesiyle) kaybolmasin
        prev = cfg.get("turbo_prev")
        if cfg.get("turbo_mode") and isinstance(prev, dict):
            for k in ("speed_limit", "max_concurrent"):
                if k in new:
                    prev[k] = cfg[k]
        warning = _recheck_download_dir(ctx) if "download_dir" in new else ""
        ctx["set_cfg"](cfg)
        if "speed_limit" in new:
            qm.set_speed_limit(int(cfg.get("speed_limit") or 0))
        out = {"ok": True, "config": cfg}
        if warning:
            out["warning"] = warning
        return out
    if cmd == "nm-ping":
        import time as _t
        qm.last_nm_ping = _t.time()
        return {"ok": True}
    return {"ok": False, "error": f"bilinmeyen komut: {cmd}"}
