# AGENTS.md — orientation for AI coding agents (and fast-onboarding humans)

FerDim is a dynamic, self-tuning download manager for Linux, built
Fedora-first, with a Firefox WebExtension. The engine, CLI and daemon
are **Python stdlib only** — no pip dependencies, ever. The web UI is
dependency-free vanilla JS. The extension is Manifest V2.

State at authorship: **1.4.0 released**; test suite green, `web-ext
lint` 0 errors, `rpmlint` 0/0, `fedora-review` no failed checks, mock
builds cleanly on Fedora 43 and rawhide.

> Path tip for this machine: the repo lives at
> `~/.zcode/workspace/7e483cf8-f9d2-44df-a23e-2b69c52213e0/ferdim`.
> Two similarly-named workspace directories have existed; always
> resolve with `ls -d ~/.zcode/workspace/*/ferdim` (the one with the
> `origin` remote is real) before editing.

## Hard rules

1. **Never reference third-party download-manager products or vendors**
   anywhere — code, comments, docs, commit messages, release notes,
   issue text. Describe behaviour generically instead: "segmented
   multi-connection downloads", "aggressive link scaling",
   "browser-integrated download handoff". This is a maintainer-set
   legal constraint; there are no exceptions.
2. `ferdim/` (engine, CLI, daemon, GTK app) and `tests/` use the
   Python stdlib only. PyGObject is an optional *runtime* for the GTK
   front-end, never imported by core modules.
3. User-facing strings go through `ferdim/i18n.py` (the `S` dict) and
   `extension/_locales/{en,tr,es,ru}/messages.json` — a new key must be
   added to **all four** languages; CI checks locale parity. Engine
   internal error strings are ASCII-Turkish by convention (no
   diacritics).
4. Engine code comments are Turkish, ASCII-safe, terse.
5. Metadata identity when adding files: **Enes Aksu \<eaksu@pm.me\>**
   (@enesaksuu34 on X). Git identity for commits:
   `proxy-turkey \<proxysatinalcom@gmail.com\>`. GitHub remote:
   `https://github.com/proxy-turkey/ferdim` (gh CLI is authenticated).

## Repository map

| Path | Role |
|---|---|
| `ferdim/engine/job.py` | The core: segmented multi-connection engine, throughput governor, adaptive server limits, parked worker pool, work stealing, dynamic range sizing, end-game donation, crash-safe resume, mirrors, checksum verify, DASH dispatch |
| `ferdim/engine/segments.py` | Shared HLS/DASH pipeline: parallel segment fetch to temp files, ordered assembly, per-segment resume |
| `ferdim/engine/hls.py` | m3u8 parsing (variants/master), thin wrapper over `segments.assemble` |
| `ferdim/engine/dash.py` | MPD parsing: best-representation selection, SegmentTemplate (`$Number$`/`$Time$`/SegmentTimeline) and SegmentList |
| `ferdim/engine/queue.py` | QueueManager: concurrency, night scheduler, smart queue (background size prober, smallest-first), persistence |
| `ferdim/engine/httpcli.py` | Probe/download primitives, headers, auth, proxy, cookies |
| `ferdim/metalink.py` | `.metalink` / `.meta4` (RFC 5854) parsing |
| `ferdim/api.py` | Unix-socket JSON-RPC + localhost HTTP API, token auth, metalink expansion, ZIP preview |
| `ferdim/server/web.py` | HTTP server; serves `web/` with the token injected into `index.html` |
| `ferdim/cli.py`, `ferdim/gui/` | CLI front-end; GTK 4 app (`app.py`, `__main__.py`) |
| `ferdim/nativehost.py`, `bin/ferdim-nm` | Firefox native messaging bridge (4-byte LE length + JSON frames) |
| `ferdim/clipboard.py` | Clipboard watching: `wl-paste --watch` event mode, polling fallback |
| `ferdim/util.py`, `ferdim/i18n.py`, `ferdim/ratelimit.py` | Helpers, 4-language strings, token bucket |
| `web/` | Vanilla-JS web UI (no build step) |
| `extension/` | Firefox MV2 WebExtension + `_locales` |
| `packaging/ferdim.spec` | Fedora spec — `%check` runs the full suite at build time |
| `tests/` | stdlib unittest; `tests/helpers.py` has counting HTTP servers, DASH/manifest fixtures, `FakeTime` for governor determinism |
| `scripts/build_srpm.sh`, `scripts/install.sh` | SRPM builder (tarball matches the GitHub tag archive layout), user-level installer |
| `docs/` | Man pages, `fedora-submission.md` (paste-ready review request), screenshots |

## Engine mental model

Read `job.py` before touching the engine. The invariants that matter:

- Default is **maximum power**: up to 32 links per job, no speed cap,
  8 concurrent jobs. The governor then tunes continuously toward
  whichever ceiling is reachable (line or server). A "turbo off" mode
  exists for politeness; never change the default direction.
- The governor (`_governor_tick`) samples a 2.5 s EMA, probes +1 link
  upward while speed keeps improving, probes −1 when pinned at a
  ceiling (keep-throughput check, revert threshold 0.85), and honours a
  `server_floor` penalty window after 429/503. Governor unit tests use
  `FakeTime` — keep them deterministic; never sleep in tests.
- Throttling (429/503, crowded-connection resets) **must never fail a
  job**: halve the pool, honour `Retry-After`, requeue ranges, remember
  the floor across resumes.
- Workers park (`_acquire`/`_release` on a Condition) instead of dying,
  so the governor can re-arm instantly.
- Range bites are sized to ~8 s of measured work (256 KB–32 MB); after
  end-game donation reads must stay bounded:
  `resp.read(min(self._chunk, remaining))`.
- Servers without byte ranges degrade to a single stream
  (`_min_split = inf`, segments = 1) — splitting them causes an
  infinite 200-restart loop.
- Resume is validated with ETag/Last-Modified via If-Range; with
  mirrors in play the validator is only sent to the primary source.
- HLS and DASH never go through the range engine; they call
  `segments.assemble` (parallel fetch, ordered assembly, per-segment
  checkpointing).

## Commands

```bash
python3 -m unittest discover -s tests        # full suite (must be green)
npx -y web-ext lint --source-dir extension   # 0 errors required
bash scripts/build_srpm.sh                   # SRPM; tarball mirrors tag archive
rpmlint ~/rpmbuild/SRPMS/ferdim-*.src.rpm packaging/ferdim.spec
sudo mock -r fedora-43-x86_64 --clean --rebuild ~/rpmbuild/SRPMS/ferdim-X.Y.Z-1.fc43.src.rpm
bash scripts/install.sh && systemctl --user restart ferdim   # user-level deploy
TOKEN=$(cat ~/.local/state/ferdim/token)     # live API probe:
curl -s -H "X-FerDim-Token: $TOKEN" http://127.0.0.1:8378/api/stats
```

`fedora-review` (needs the SRPM and spec together in one empty dir):

```bash
sudo REVIEW_NO_MOCKGROUP_CHECK=1 fedora-review -n ferdim
```

Note: `mock` requires sudo **with a TTY** — don't run it as a
background task; it will fail on the password prompt.

## Release checklist

1. Bump **all** version spots: `ferdim/__init__.py` (`__version__`),
   `packaging/ferdim.spec` (`Version:` + `%changelog` entry),
   `docs/ferdim.1` / `ferdim-nm.1` / `ferdim-gui.1` (`.TH` string),
   `extension/manifest.json`, `CHANGELOG.md`.
2. Gates: tests green, web-ext lint 0 errors, rpmlint 0/0, mock build
   OK (its `%check` re-runs the suite).
3. Commit, push `main`, tag `vX.Y.Z`, push the tag. **Never
   force-push an existing tag** — GitHub rejects it; bump the version
   instead.
4. `gh release create vX.Y.Z` with notes; attach the mock-built RPM.
5. Rebuild the SRPM from the *actual* GitHub tag archive (download it
   over `~/rpmbuild/SOURCES/vX.Y.Z.tar.gz`, `rpmbuild -bs`) so Source0
   provenance holds for the Fedora review.
6. Locally: `sudo dnf upgrade <mock rpm>`, `bash scripts/install.sh`,
   `systemctl --user restart ferdim`, verify the version in
   `~/.local/state/ferdim/ferdim.log` and via the API.

## Known gotchas (each cost real debugging time)

- ElementTree: **childless elements are falsy** — use `is not None`,
  never `or` chains (broke `dash.py` and `metalink.py` once).
- In threaded HTTP test servers, keep `import time` at module level; a
  function-level import made `time` an UnboundLocal across `do_GET`.
- `os.replace` across filesystems throws EXDEV — `.part` files live in
  the *target* directory for this reason.
- The SRPM tarball transform must anchor on the first path component
  from the repo **parent** directory (`s,^ferdim\(/┃$\),ferdim-$VER\1,`
  — GNU tar basic regex); otherwise the inner Python package directory
  gets renamed into the archive root and `%install` breaks inside mock.
- `firefox --headless --screenshot` needs an **absolute** output path;
  with a relative path it silently leaves the old file in place.
- If the systemd user service crash-loops with "zaten calisiyor", an
  orphan daemon PID is holding the socket — kill that PID and let
  systemd's restart take over.
- GitHub tag archives extract as `<repo>-<version>/` (the leading `v`
  is stripped from the directory name).

## Fedora submission status

`docs/fedora-submission.md` documents the process and contains the
paste-ready review request. As of 1.4.0 the package is review-clean
(`fedora-review`: no failed checks). The blocker is procedural: the
maintainer needs a sponsor — read that doc before touching packaging.

## Roadmap

Future work lives in [ROADMAP.md](ROADMAP.md). Keep it in sync when a
release closes items.
