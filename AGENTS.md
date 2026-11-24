# AGENTS.md — orientation for AI coding agents (and fast-onboarding humans)

FerDim is a dynamic, self-tuning download manager for Linux, built
Fedora-first, with a Firefox WebExtension. The engine, CLI and daemon
are **Python stdlib only** — no pip dependencies, ever. The web UI is
dependency-free vanilla JS. The extension is Manifest V3.

State at authorship: **1.6.0 released**; test suite green (381 tests),
`web-ext lint` 0 errors / 0 warnings, UI smoke checks green
(`bash tests/ui/run.sh all`), `rpmlint` 0/0, `fedora-review` no failed
checks, mock builds cleanly on Fedora 43.

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
| `ferdim/engine/job.py` | The core: segmented multi-connection engine, throughput governor, adaptive server limits, parked worker pool, work stealing, dynamic range sizing, end-game donation, crash-safe resume, source health (mirrors, fail-over, stall), checksum + sidecar verify, DASH dispatch |
| `ferdim/engine/segments.py` | Shared HLS/DASH pipeline: parallel segment fetch to temp files, ordered assembly, per-segment resume |
| `ferdim/engine/ftp.py` | FTP/FTPS via `ftplib`: single stream, REST resume, MDTM change detection, transient-error retries, 2xx final reply required (truncation guard), codes on `FtpError` |
| `ferdim/engine/hls.py` | m3u8 parsing (variants/master), thin wrapper over `segments.assemble` |
| `ferdim/engine/dash.py` | MPD parsing: best-representation selection, SegmentTemplate (`$Number$`/`$Time$`/SegmentTimeline) and SegmentList |
| `ferdim/engine/queue.py` | QueueManager: concurrency, night scheduler + per-category windows, smart queue (background size prober, smallest-first), persistence (mirrors, checksums and per-job limits survive restarts) |
| `ferdim/engine/socks.py` | SOCKS5 tunnel client (RFC 1928 / RFC 1929), pure stdlib: `socks5://` resolves locally, `socks5h://` at the proxy |
| `ferdim/engine/httpcli.py` | Probe/download primitives (per-read timeout ⇒ stall detection), `fetch_small` for sidecars, headers, auth, proxy, cookies |
| `ferdim/profiles.py` | Learned per-host connection profiles (`hosts.json`): seed for new jobs, report on done/pause |
| `ferdim/coexist.py` | Opt-in coexistence: `/proc/net/dev` line monitor minus own traffic, hysteresis → queue sets `set_external_cap` |
| `ferdim/engine/h3.py` | Optional HTTP/3 via lazily imported `aioquic`; Alt-Svc cache, broken-origin backoff, blocking `Response` adapter |
| `ferdim/cdc.py` | FastCDC chunker, Merkle root, `.ferdim-merkle` manifests, `merkle update` |
| `ferdim/archive.py` | Nested ZIP / tar listing, selective extraction, `.ferdim-extract.json` repair |
| `ferdim/notify.py`, `ferdim/dbusbridge.py` | Desktop notifications (gdbus/notify-send argv, coalesced); optional Gio session-bus bridge `org.ferdim.Daemon1` |
| `gnome-shell-extension/` | GNOME Shell 47–49 panel extension (talks to `ferdim-dbus`) |
| `extension/sniff.js` | Pure, testable response-header media classifier used by `background.js` |
| `ferdim/peer.py`, `ferdim/tcpinfo.py` | Opt-in LAN peer cache (chunks by SHA-256, explicit manifests only); `TCP_INFO` reader + congestion signal |
| `ferdim/metrics.py`, `ferdim/publish.py` | Prometheus `/metrics` collector; `ferdim publish` sidecar writer |
| `tests/fuzz/` | Stdlib fuzz harness (`python3 -m tests.fuzz --seconds 60 --seed 1`); `known.json` must stay empty |
| `packaging/flatpak/`, `docs/flatpak.md` | Flatpak manifests, AppStream metadata, native-messaging wrapper |
| `packaging/aur/PKGBUILD` | Arch package; `.github/workflows/aur.yml` publishes it on tag |
| `ferdim/metalink.py` | `.metalink` / `.meta4` (RFC 5854) parsing |
| `ferdim/api.py` | Command handlers shared by socket and HTTP: token-auth API, metalink expansion (remote + local `add-file`), config sanitising (`_sanitize_config`), turbo/limit consistency, ZIP preview |
| `ferdim/watch.py` | Watched folders: `.metalink`/`.meta4` and URL-list files are queued, then deleted / renamed `.done` / kept |
| `ferdim/server/web.py` | HTTP server; serves `web/` with the token injected into `index.html` |
| `ferdim/cli.py`, `ferdim/gui/` | CLI front-end; GTK 4 app (`app.py`, `__main__.py`) |
| `ferdim/nativehost.py`, `bin/ferdim-nm` | Firefox native messaging bridge (4-byte LE length + JSON frames) |
| `ferdim/clipboard.py` | Clipboard watching: `wl-paste --watch` event mode, polling fallback |
| `ferdim/util.py`, `ferdim/i18n.py`, `ferdim/ratelimit.py` | Helpers (digests, checksum-text parsing, **1024-based** size parsing, schedule windows), 4-language strings, token bucket |
| `web/` | Vanilla-JS web UI (no build step). **No `innerHTML`**: server data is written with `textContent`; rows are keyed by job id and updated in place so keyboard focus survives the 500 ms polling (a static test enforces both) |
| `extension/` | Firefox MV2 WebExtension + `_locales` |
| `packaging/ferdim.spec` | Fedora spec — `%check` runs the full suite at build time |
| `tests/` | stdlib unittest; `tests/helpers.py` has counting HTTP servers, DASH/manifest fixtures, `FakeTime` for governor determinism. `tests/test_roadmap.py` covers source health, checksums, watch folders, API behaviour and static UI rules |
| `tests/cli_smoke.sh` | End-to-end CLI smoke test against the *running* daemon with the installed binary (isolated download folder, restores settings, removes only its own jobs) |
| `tests/ui/run.sh` | Optional UI gates (not part of `unittest`): live web UI in headless Chromium (Playwright) + GTK smoke under Xvfb, against an isolated daemon |
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
- Resume is validated with ETag/Last-Modified via If-Range. The
  validator is only sent to the source that provided it (`_vsrc`,
  persisted as `validator_src`); more than 3 restarts of one job fails
  it ("kaynak surekli degisiyor").
- **Source health** (primary + mirrors are `_Src` records): selection is
  smooth weighted round-robin over measured per-connection throughput
  (`_pick_locked`); every source has a soft `cap` (halved by `_demote`
  on 429/503, doubled back after 60 s) and a failure `streak`.
  `kind == "gone"` (404/410, mirror auth errors, `SourceInvalid`) kills
  a source at once, other kinds need `streak > retries`. A failing
  source hands its range to another one (fail-over); the job fails only
  when no source is alive (`_dead_reason()`).
- `_open` validates the `Content-Range` start and total of every
  response: a mirror that ignores `Range` or serves another file is
  dropped, never a reason to restart the job.
- `open_range(timeout=self._stall)` applies the timeout to every read:
  `stall_timeout` seconds (default 20) without data ⇒ `TimeoutError` ⇒
  range requeued, source failed as `stall`.
- Retries back off with `backoff_delay` (2ⁿ s capped at 15, ×0.5–1.5
  jitter). A range is requeued only when its worker gives up, so the
  same range is never processed twice.
- **In-flight ranges are checkpointed.** Every worker registers the range
  it holds in `self._inflight[wid]` (advanced per chunk, narrowed on
  donation) and `_checkpoint` writes `_pending` + `_inflight`. A pause or
  a crash mid-read therefore re-downloads that range from its last
  completed offset instead of resuming with a gap ("eksik veri").
  `requeue()` pops the entry in the same locked section, so an entry is
  never both queued and in flight.
- Workers belong to a **generation** (`self._gen`, bumped in `_spawn`).
  A worker that was still inside a read when its job was paused and
  resumed sees `gen != self._gen` and returns without requeueing or
  writing — its range is already in the new pending list.
- `pause()` is synchronous: sets `_stop`, wakes parked workers, joins the
  monitor (≤ 8 s) and returns when the state is `paused` and the
  checkpoint exists. Never rely on a pause that only sets `_stop`.
- Every blocking wait in a worker must honour `_stop`:
  `TokenBucket.take(n, stop)` returns `False` when stopped (the caller
  requeues its range), reads time out through `stall_timeout`, and
  `_join_threads()` joins all workers under ONE shared deadline (per-thread
  timeouts multiply by worker count). Without this a speed-limited job
  took minutes to pause or cancel.
- `target_dir` is pinned at first use and persisted (`"dir"` in
  `queue.json` is always written): a job keeps its folder when the default
  downloads folder changes. Anything a snapshot exposes must be stable —
  the old property followed the *current* default and retroactively
  rewrote `dir`/`fullpath` of finished jobs.
- **Verification runs before DONE.** `_finalize_file` verifies while the
  state is still `downloading` (`extra["verifying"] = True`) and only
  then sets `done`; anything that sees `done` may trust the file. A
  sidecar that arrives after that goes through `_verify_late` (the
  `_finalized`/`_sc_lock` handshake closes the race) and can only add a
  warning. Explicit mismatch ⇒ `error` with the file kept; sidecar-only
  mismatch ⇒ `done` + `checksum = "warn"`.
- Size units are 1024-based end to end (`parse_size` ↔ `human_size`,
  the web limit options and the extension thresholds agree): `2MB` is
  shown as `2.0 MB`.
- Turbo and limits never contradict each other: setting a speed limit
  (`limit` command or the web select) leaves turbo; `config-set` keeps
  `turbo_prev` in sync so a concurrency value chosen during turbo
  survives turning turbo off.
- The downloads folder is resolved from the live config
  (`AppEnv.reload_download_dir(cfg)` sets `_dir_cfg`); an unusable
  folder falls back to the default with a `download_dir` warning code.
  `AppEnv(root=...)` pins the folder (`_fixed_dir`) so tests never
  write into the real one.
- Jobs added with `paused=True` stay paused until the user resumes;
  `schedule.categories` windows gate only that category.
- HLS and DASH never go through the range engine; they call
  `segments.assemble` (parallel fetch, ordered assembly, per-segment
  checkpointing).
- A `socks5://` / `socks5h://` proxy is a **tunnel**, not an HTTP proxy:
  `httpcli._make_opener` swaps the urllib proxy handler for SOCKS
  handlers, TLS is wrapped over the tunnel with the target's SNI, and
  FTP opens control + passive data connections through `_SocksFTP`
  (active mode is impossible over CONNECT). Proxy `auth` / `forbidden`
  codes are terminal — never burn retries on them.
- **Ceilings compose.** `_ceiling_locked()` = min(segment max, external
  cap). Coexistence sets the external cap; the profile seed, resume-restored
  links and every governor up-probe are clamped to it under the job lock.
  With `coexist` off the cap is `None` and nothing changes.
- **Merkle repair before DONE.** `_finish()` may return "repair queued":
  `_merkle_repair()` puts only the bad chunk ranges back into `_pending`
  and restarts workers via `_start_workers` (never a second monitor), at
  most `MERKLE_REPAIR_MAX` (2) times, then `merkle_mismatch`.
  Only an explicit `--merkle` manifest can fail a job; an auto-discovered
  `<url>.ferdim-merkle` mismatch is a warning (no repair), like a stale
  `.sha256` sidecar. Repair and verification honour `_stop` (pause stays
  synchronous, cancel stays canceled).
- **HTTP/3 is off by default** and `aioquic` must never be imported at core
  import time (a test checks `sys.modules`). `auto` falls back to TCP;
  `force` does not; proxies disable h3.
- **Queue starts are asynchronous.** `_tick` dispatches `job.start` on a
  short-lived thread; `_starting` reserves the slot until `start()` sets
  `CONNECTING`, so `max_concurrent` holds across overlapping ticks.
- **Peer prefill is explicit-only and bounded.** It runs only with an
  explicit Merkle manifest, verifies every chunk, honours `_stop`, drops
  its result when the run token changed, and stays within 3.5 s so pause
  stays synchronous.
- Archive extraction writes `member.part` then `os.replace` and records the
  member in `.ferdim-extract.json`; link and device members are never
  created; files FerDim did not write are not overwritten without `force`.

## Commands

```bash
python3 -m unittest discover -s tests        # full suite (must be green)
npx -y web-ext lint --source-dir extension   # 0 errors and 0 warnings required
bash tests/ui/run.sh all                     # optional UI gates: web (Playwright) + GTK (Xvfb)
bash tests/cli_smoke.sh                      # installed binary vs the RUNNING daemon (restores settings)
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
   `docs/ferdim.1` / `ferdim-nm.1` / `ferdim-gui.1` and their Turkish
   twins in `docs/tr/` (`.TH` string; `tests/test_manpages.py` checks),
   `extension/manifest.json`, `pyproject.toml`, `packaging/aur/PKGBUILD`
   (`pkgver`), the GNOME extension `metadata.json` (`version-name`),
   `packaging/flatpak/*.metainfo.xml` (`<release>`) and the Flatpak
   manifest's tag archive + sha256, `CHANGELOG.md`.
2. Gates: tests green, web-ext lint 0/0, `bash tests/ui/run.sh all`
   green, `bash tests/cli_smoke.sh` green against the installed build,
   rpmlint 0/0, mock build OK (its `%check` re-runs the suite),
   `fedora-review` clean.
2b. Docs travel with the code: README feature rows + usage block,
   `ROADMAP.md` ticks, `CHANGELOG.md` entry, man pages (`man --warnings
   -l docs/ferdim.1` must be silent) and the state line at the top of
   this file.
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
- A line starting with `.metalink` / `.meta4` is a troff macro call —
  escape it as `\&.metalink` in man pages.
- GTK 4 via PyGObject 3.54 (the installed GTK is 4.20): mnemonic labels
  use `use_underline` + `set_label("_…")` + `mnemonic_widget` (there is
  no `use_mnemonic` / `set_label_with_mnemonic`); `update_relation`
  from Python emits a GLib warning, so label↔field relations come from
  mnemonics only; `Gtk.ScrolledWindow` takes `hscrollbar_policy` /
  `vscrollbar_policy` (not `policy`); `Gtk.AlertDialog` is not a
  `Gtk.Window` (absent from `list_toplevels`); `Gtk.Dialog.response` is
  deprecated. Do not guess GTK names — `dir(Gtk.Label)` first.
- Playwright (Python) here has no `page.accessibility`; assert
  accessibility through the DOM (`aria-*` attributes) instead.
- `ensure_daemon()` called from a UI test spawns a detached daemon. Run
  UI tests with temporary `XDG_STATE_HOME` / `XDG_CONFIG_HOME` and
  `PYTHONPATH=<repo>` (otherwise the *installed* daemon starts) and
  kill it through `state/ferdim/ferdim.pid`; `tests/ui/run.sh` does all
  of this.
- Multi-step edit scripts must assert every marker **before** writing
  anything. A half-applied run once left a function with a docstring but
  no body (`_bad_checksum` returned `None`), and a hand-typed 8-space
  marker silently matched inside a 12-space line.
- `fedora-review` does **not** print `PASS:`/`FAIL:` lines. The verdict
  is in `review-ferdim/review.json` → `results[MUST|SHOULD|EXTRA]` →
  lists of `{name, result, note}`. 1.4.0 shipped claiming "review-clean"
  while `CheckDesktopFileInstall`, `CheckSystemdUserunitdirScriplets` and
  directory ownership were failing and nobody looked. Always run:
  `sudo python3 -c "import json;d=json.load(open('review-ferdim/review.json'));print([c['name'] for g in d['results'].values() for l in g.values() for c in l if c['result']=='fail'])"`
  — the expected answer after a tagged release is `[]`. `pending`
  results are the human reviewer's manual checks, not failures.
- `systemd-rpm-macros` ≥ 258 requires the unit name as an argument
  (`%systemd_user_post ferdim.service`); with none, rpm aborts with
  "%systemd_user_post macro requires some arguments".
- `fedora-review` matches scriptlets against the verbatim macro expansion
  (last line `fi` + space + newline). **rpm 6 trims that trailing
  whitespace**, so `CheckSystemdUserunitdirScriplets` false-FAILs for any
  user-unit package (stock packages included). Keep the inert trailing
  comment inside `%post` / `%preun` — it preserves the newline and the
  check passes. Other packaging MUSTs this project once missed:
  `desktop-file-validate` on the shipped `.desktop`
  (`BuildRequires: desktop-file-utils`) and owning every directory the
  package creates (`%dir %{_datadir}/mozilla/native-messaging-hosts`).
- The review directory is root-owned (`sudo rm -rf`), and running
  `cd` into it resets the shell's working directory — `cd` back to the
  repo explicitly.
- **Two smoke layers exist and both matter:** `bash tests/ui/run.sh all`
  (isolated daemon; web UI in Chromium, GTK under Xvfb) and
  `bash tests/cli_smoke.sh` (the *running* daemon and the *installed*
  binary; restores every setting it touches). The second one found the
  socket `id` bug, the slow pause under a speed limit and the moving
  `target_dir` in a single run.
- **Socket protocol:** the request correlation field is `req_id`, never
  `id` — `id` is the job id. Until 1.4.2 the server popped `id` as the
  correlation id, so `ferdim pause/resume/cancel/restart/remove` (and
  the GTK buttons) silently did nothing since 1.3.0. The web UI uses
  HTTP and hid the bug; only an end-to-end run of the real CLI verbs
  against the real daemon exposed it. Before a release, drive every
  user-visible verb through the installed binary — unit tests of
  `api.handle()` do not go through the socket.
- When a smoke test cleans up, remove **only the jobs it created** (by
  id); the user's real queue lives in the same daemon.
- CLI output follows the shell's locale (`LANG=en_US.UTF-8` here ⇒
  English strings); never assert on Turkish CLI text.
- Timing-sensitive assertions are a smell: if a test can pass or fail
  depending on poll timing, the state machine is wrong (that is how
  "done before verify" was found). Fix the ordering, do not add sleeps.

## Fedora submission status

`docs/fedora-submission.md` documents the process and contains the
paste-ready review request. Since 1.4.1 `fedora-review` reports no
failed checks on the tag-built SRPM (36 pass, 27 pending = the reviewer's
manual items); before a tag exists only `CheckSourceDownloads` fails. The
blocker is procedural: the
maintainer needs a sponsor — read that doc before touching packaging.

## Roadmap

Future work lives in [ROADMAP.md](ROADMAP.md). Keep it in sync when a
release closes items.
