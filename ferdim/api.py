"""Komut tasiyicilari: socket ve HTTP sunucularinin ortak API katmani."""
from __future__ import annotations

from .engine.job import JobSpec
from . import util
from .i18n import detect_lang, dict_for


def _default_segments(cfg: dict) -> int:
    if cfg.get("turbo_mode"):
        return int(cfg.get("turbo_segments", 32))
    return int(cfg.get("segments", 8))


def _spec_from(params: dict, cfg: dict) -> JobSpec:
    headers = {}
    for h in params.get("headers") or []:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()
    return JobSpec(
        url=params.get("url", ""),
        filename=util.sanitize_filename(params["filename"])
        if params.get("filename") else "",
        directory=params.get("dir") or params.get("directory") or "",
        segments=int(params.get("segments") or _default_segments(cfg)),
        headers=headers,
        cookies=params.get("cookies") or "",
        user=params.get("user") or "",
        password=params.get("password") or "",
        proxy=params.get("proxy") or "",
        insecure=bool(params.get("insecure")),
        referer=params.get("referer") or params.get("page") or "",
        user_agent=params.get("ua") or params.get("user_agent") or "",
        limit=util.parse_size(params.get("limit")) if params.get("limit") else 0,
    )


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
    if cmd == "stats" or cmd == "list" or cmd == "jobs":
        return {"ok": True, **qm.stats()}
    if cmd == "add":
        if not params.get("url", "").startswith(("http://", "https://")):
            return {"ok": False, "error": "gecersiz URL"}
        spec = _spec_from(params, cfg)
        job = qm.add(spec)
        qm._tick()
        return {"ok": True, "id": job.id, "filename": job.snapshot()["filename"]}
    if cmd == "add-batch":
        ids = []
        for item in params.get("items", []):
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
    if cmd == "limit":
        val = params.get("value", 0)
        if isinstance(val, str):
            val = util.parse_size(val)
        qm.set_speed_limit(int(val or 0))
        return {"ok": True, "speed_limit": qm.cfg["speed_limit"]}
    if cmd == "config-get":
        return {"ok": True, "config": cfg}
    if cmd == "config-set":
        new = params.get("config") or {}
        for k, v in new.items():
            if k in cfg:
                cfg[k] = v
        ctx["set_cfg"](cfg)
        if "speed_limit" in new:
            qm.set_speed_limit(int(cfg.get("speed_limit") or 0))
        if "segments" in new:
            cfg["segments"] = max(1, min(32, int(cfg["segments"])))
        return {"ok": True, "config": cfg}
    if cmd == "nm-ping":
        import time as _t
        qm.last_nm_ping = _t.time()
        return {"ok": True}
    return {"ok": False, "error": f"bilinmeyen komut: {cmd}"}
