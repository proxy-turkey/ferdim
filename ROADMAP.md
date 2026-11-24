# Roadmap

FerDim 1.4.0 closed the original feature roadmap (adaptive streams,
mirrors/metalink, checksums, ZIP preview, smart queue, Wayland
clipboard — all shipped). This file tracks what comes next. Items are
grouped by intended release; nothing here is a promise, and community
contributions can pull any item forward.

Priorities, in one line: **distribution first, robustness second,
reach third, research last.**

## 1.4.x — robustness polish

Shipped in **1.4.1** (2026-11-23):

- [x] Mirror health scoring: weight the round-robin by measured
      throughput; demote mirrors that repeatedly fail or stall.
- [x] Retry policy: exponential backoff with jitter across sources;
      never retry a dead mirror within a job without penalty.
- [x] Checksum sidecar auto-discovery: probe `<url>.sha256` /
      `<url>.md5` before the transfer finishes and verify
      automatically when found.
- [x] More digests: SHA-1 and SHA-512 (`--sha1`, `--sha512`).
- [x] Per-category scheduler windows (e.g. "ISOs only at night").
- [x] Watch folders: auto-queue metalink/URL text files dropped into a
      configured directory.
- [x] Web UI + GTK accessibility pass: full keyboard navigation,
      reduced-motion preferences, screen-reader labels.

## 1.5 — distribution & reach

- [x] COPR repository with automatic builds on tag push; document
      `dnf copr enable` as the one-liner install.
- [ ] Official Fedora package submission completed (sponsor found;
      `docs/fedora-submission.md` has the template).
- [ ] openSUSE OBS and Arch AUR community packages. (OBS done:
      `home:proxy-turkey/ferdim`; AUR: `packaging/aur/PKGBUILD` builds and
      `.github/workflows/aur.yml` publishes on tag — the first push waits for
      the publish key to be registered on the AUR account.)
- [ ] Extension published on AMO (signed) with an update URL; MV3
      migration spike to stay ahead of MV2 deprecation. (Submitted to the
      listed channel on every tag, awaiting Mozilla review; spike done —
      `docs/mv3-spike.md`, `scripts/build_mv3.sh`: the only MV3 break is
      `tabs.executeScript` in the link-grab path.)
- [ ] More locales: German, French, Portuguese (Brazil), Arabic —
      community-reviewed.
- [x] FTP/FTPS downloads via stdlib `ftplib`.
- [x] SOCKS5 proxy support — shipped in the stdlib core itself (1.5.1),
      so no extra is needed.
- [x] Web UI speed graph (per-job and aggregate) and a live
      connection-count timeline; canvas-based, no dependencies.
- [x] Queue history in SQLite: search, re-download, aggregate
      statistics (bytes served, average link counts per host).
- [x] Carried over from 1.4.1: probe mirrors in the background (not only
      when a worker first needs them) so an unreachable mirror is reported
      before it slows anything down; error codes in the API so the UIs can
      translate engine messages (the engine's own strings are Turkish).
- [x] GTK front-end: migrate the add dialog from the deprecated
      `Gtk.Dialog` to `AdwDialog` / `Gtk.AlertDialog` and add a settings
      window on par with the web UI.

## 1.6 — desktop integration

Shipped in **1.6.0** together with the 2.0 research items below.

- [x] D-Bus interface (`org.ferdim.*`) for desktop shells; a GNOME
      Shell extension driving the daemon.
- [x] Extension media sniffing v2: generic response-header analysis to
      catch more HLS/DASH manifests and large-file direct links
      (strictly generic — no site-specific scraping, ever).
- [x] Learned per-host connection profiles: remember the observed
      link ceiling, RTT band and per-link cap per host class; apply
      them as the starting point for new jobs.
- [x] Archive browsing: nested ZIP listing, selective extraction,
      partial-extract repair.
- [x] Desktop notifications (portals) for job completion and
      verification results.

## 2.0 — research / stretch

- [x] HTTP/3 (QUIC) transport as an optional extra
      (`pip install ferdim[h3]`); the stdlib path stays the default.
- [x] Content-defined chunking with Merkle verification: integrity
      that survives re-downloads of only the changed ranges.
- [x] Coexistence mode (opt-in): automatically shed links when
      interactive traffic is detected on the line. Default stays
      maximum power; this is the polite opt-out made smarter.
- [x] Localized man pages (Turkish first).

## Open from earlier releases (blocked outside the code)

- [ ] Official Fedora package (needs a sponsor; `docs/fedora-submission.md`).
- [ ] AUR publishing live (needs the publish key registered on the AUR
      account; the workflow already builds and tests every tag).
- [ ] Signed AMO listing (every tag is submitted; waits for Mozilla review).
- [ ] German, French, Portuguese (Brazil) and Arabic locales —
      community-reviewed, plus RTL layout checks for Arabic.

## 1.7 — finish what 1.6 started

Shipped in **1.7.0** together with the 1.8 and 2.x items below.

Every item here closes a gap that 1.6 shipped knowingly.

- [x] Manifest V3 build: replace `tabs.executeScript` in the link-grab path
      with the `scripting` API, keep event-page state in
      `storage.session`, ship MV3 once `scripts/build_mv3.sh` lints 0/0
      and loads (`docs/mv3-spike.md` has the plan).
- [x] Host profiles in the web UI and GTK app (today: CLI and API only),
      with per-host "forget"; let HLS/DASH segment pools and FTP jobs learn
      too.
- [x] HTTP/3: reuse one QUIC connection per origin instead of one per
      range; carry sidecars and HLS/DASH segments over h3 when enabled.
- [x] Coexistence mode: add a latency signal (RTT inflation on our own
      links) next to foreign-traffic accounting, so a busy line is detected
      before it saturates.
- [x] Merkle: show `merkle` / `checksum` warnings in the web UI and GTK
      app; accept Merkle manifests inside metalink documents.
- [x] GTK app parity: archive browsing, history and coexistence toggle.
- [ ] GNOME Shell extension on extensions.gnome.org (reviewed, versioned
      with the release). (Review-ready and packed on every tag by
      `.github/workflows/gnome-ext.yml`; the upload needs the maintainer's
      EGO account.)

## 1.8 — reach and robustness

- [ ] Flatpak on Flathub (daemon + GTK app + native-messaging bridge
      for sandboxed Firefox). (Manifests in `packaging/flatpak/` build,
      lint and run locally; `.github/workflows/flatpak.yml` bundles every
      tag; the Flathub submission is a maintainer PR — `docs/flatpak.md`.)
- [x] Fuzz the parsers that read untrusted input — m3u8, MPD, metalink,
      `.ferdim-merkle`, archive listings, SOCKS replies — with a
      stdlib-only harness in CI.
- [x] Time-of-day speed profiles (e.g. 2 MB/s during work hours, maximum
      power at night) on top of the existing schedule windows.
- [x] Metrics endpoint: per-host and aggregate counters (bytes, links,
      retries, throttles) in a plain text format for local monitoring.
- [x] Faster chunker for `ferdim merkle make` (today ~6 MB/s in pure
      Python; verification is already hashlib-bound).

## 2.x — research

- [x] LAN peer cache (opt-in): machines on the same network share
      already-verified chunks by Merkle hash instead of fetching them again
      from the internet.
- [x] Publisher tooling: a small static-site helper that emits
      `.ferdim-merkle` and metalink files next to release artefacts.
- [x] Congestion awareness: read the kernel's TCP info (RTT, retransmits)
      per link to steer the governor, not only throughput.

## 1.9 — everyday safety

Planned after 1.7.0. Each item closes a gap found in the current code.

- [ ] Disk-space preflight for downloads: check free space against the
      announced size before allocating, pause the queue (not fail jobs) on
      `ENOSPC` or below a configurable floor, resume when space returns.
      Today only archive extraction checks free space.
- [ ] Duplicate detection before queuing: same URL, same final file
      path, or same checksum / Merkle root as a finished job ⇒ ask (UIs)
      or refuse with a code (API), with an explicit "download again".
- [ ] `~/.netrc` credentials for HTTP and FTP (stdlib `netrc`), used only
      for the matching host and never forwarded to mirrors on other
      origins.
- [ ] Post-download actions per category: open folder, extract archive,
      or run a user command as an argv list (no shell), with the job's
      path passed as an argument, after verification only.
- [ ] Bulk actions in the web UI and GTK app: multi-select pause, resume,
      remove, move to category; drag-and-drop of URLs and
      `.metalink` / `.meta4` files onto the window.
- [ ] Shell completions (bash, zsh, fish) generated from the CLI parser
      and shipped in every package.
- [ ] Settings and queue export / import (one JSON file, secrets
      excluded) for moving to a new machine.

## 1.10 — hardening and supply chain

- [ ] systemd user-unit hardening (`NoNewPrivileges`, `ProtectSystem`,
      `PrivateTmp`, `RestrictAddressFamilies`, `SystemCallFilter`, …),
      checked with `systemd-analyze security --user` in CI and kept
      compatible with the download folder and peer cache.
- [ ] Fault-injection test proxy in CI: connection resets, truncated
      bodies, slow drip, wrong `Content-Range`, flapping `ETag`, 429
      storms — every job must end `done` with a verified file or a clear
      error code, never a silent corruption.
- [ ] Nightly soak run: hours of randomized jobs against the fault proxy
      with pause / resume / restart of the daemon, checking file hashes
      and that memory and thread counts stay flat.
- [ ] Release integrity: `SHA256SUMS` plus a detached signature and an
      SPDX SBOM attached to every GitHub release; reproducible SRPM/RPM
      (`SOURCE_DATE_EPOCH`) verified by a second build in CI.
- [ ] Debian / Ubuntu packages built on OBS next to the openSUSE ones,
      with the same `%check`-style test run.
- [ ] RTL readiness in the web UI, GTK app and extension (`dir="rtl"`
      layout tests with a pseudo-locale) so the Arabic locale only needs
      translated strings when it arrives.

## 2.x — research (next)

- [ ] Delta updates: given an older local copy and the new version's
      `.ferdim-merkle`, fetch only the changed chunks and rebuild the new
      file, verified against the new root.
- [ ] Authenticated LAN peer cache: an optional shared group secret
      (TLS-PSK or HMAC on beacons and chunk requests) so only trusted
      machines take part.
- [ ] Multi-interface downloads (opt-in): spread links over several
      uplinks (e.g. Ethernet + Wi-Fi) by binding sockets per interface,
      with the governor tuning each path separately.
- [ ] Governor replay: record anonymized throughput traces and replay
      them offline, so tuning changes are measured against real lines
      before they ship.

## Path to the definitive release

A full audit of 1.7.0 (engine, API and UIs, extension, tests, CI,
packaging, docs) produced the phases below. Together with the open
items above they are everything between today and **3.0 LTS**, the
release FerDim considers finished: secure by default, unable to lose or
corrupt data silently, fast with thousands of jobs, fully tested and
reproducibly shipped. Phases are ordered by risk; security and data
integrity come first. File references point at the code as of 1.7.0.

## 1.7.x — security hotfixes

Fixed in point releases, before any new feature work.

- [ ] Stop handing the API token to anyone who asks: `GET /` serves
      `index.html` with the token inlined and no check
      (`server/web.py:103`), so every local account on the machine gets
      full control. Bootstrap the page with a one-time loopback nonce or
      verify the peer's uid before serving it; change
      `test_web_security.test_02` to match.
- [ ] Confine `add --dir` and `archive-extract` / `archive-repair` `dest`
      to the downloads root or an explicit allow-list
      (`api.py:69`, `api.py:410`): today any absolute path plus a
      server-chosen file name is an arbitrary-file-write primitive.
- [ ] Drop `Authorization`, `Cookie` and custom headers when a redirect
      or a mirror leaves the configured origin; urllib copies them to the
      new host (`httpcli.py:155`).
- [ ] Extension: hand the download off first and cancel the browser's
      copy only after the daemon accepts it (`background.js:253`); today
      a stopped daemon loses the user's download.
- [ ] Extension: read cookies from the download's own store
      (`cookieStoreId`) so container tabs keep their sessions apart, and
      never persist private-window cookies to disk.
- [ ] Cap HTTP request bodies (1 MiB) and require `application/json`;
      cap socket RPC lines; send `Connection: close` on every early
      reject so an unread body can't be parsed as the next request.
- [ ] Always re-apply mode 0600 to the token, `config.json`,
      `queue.json`, `hosts.json` and the log; add `ferdim token rotate`.

## 1.11 — data integrity and crash safety

Nothing may ever finish as `done` with wrong bytes, and no restart may
silently lose the queue.

- [ ] Refuse to resume without a validator: when a server sends neither
      `ETag` nor `Last-Modified`, `"" == ""` currently resumes a changed
      file (`job.py:671`). Restart, or resume only with a checksum.
- [ ] Reject a response `Content-Encoding` other than `identity` on
      ranged downloads; a server that compresses anyway now yields a
      silently corrupt file.
- [ ] fsync the `.part` before a range leaves the checkpoint and fsync
      `atomic_write_json` (file + directory), so a power cut can't leave
      zero-filled "finished" ranges.
- [ ] Corrupt state never crash-loops or empties the queue: validate
      `queue.json` item by item (`queue.py:535`), quarantine a bad file
      as `queue.json.bad`, keep the last good copy, report it in the UI.
- [ ] Schema versions and ordered migrations for `config.json`,
      `queue.json` and `hosts.json` (as `history.py` already has), plus
      downgrade safety: unknown keys are preserved, unknown states
      degrade to `queued`.
- [ ] Single-instance lock (`flock` on the state directory) so a stale
      PID can't let two daemons share the socket and state; reload
      `hosts.json` when another process changed it.
- [ ] Reserve the final file name atomically (`O_EXCL`) so two jobs with
      the same name can't overwrite each other (`util.py:64`).
- [ ] Clean up every temporary artefact on cancel, remove and error:
      orphaned `.part` files, HLS/DASH `.segs/` folders, and the `.part`
      descriptor leaked when `_prepare` fails (`job.py:684`).
- [ ] Reset the verification fields (`checksum`, `merkle`, `verified`,
      `peer_bytes`) when a job restarts, so old warnings don't stick.
- [ ] Detect soft-404s: an HTML body for a URL or name that promises
      something else finishes with a warning code instead of a silent
      `done`.
- [ ] Verify in a single cancellable pass with progress, instead of
      hashing large files twice after the transfer.

## 1.12 — protocol completeness and edge cases

- [ ] Make `speed_limit` global as documented: one queue-wide token
      bucket shared by all workers, with `--limit` as the per-job cap
      (today 8 jobs × 2 MB/s = 16 MB/s).
- [ ] HLS/DASH per-segment retries with backoff; one transient error
      should not abort a 2000-segment stream (`segments.py:127`).
- [ ] HLS: support `#EXT-X-BYTERANGE` (fetch the declared sub-ranges in
      order instead of whole, duplicated files).
- [ ] HLS: decrypt `#EXT-X-KEY` `METHOD=AES-128` segments (AES-128-CBC
      with the playlist's IV or the media-sequence IV, key rotation
      mid-playlist, keys fetched once per URI with the job's cookies and
      headers, never written to disk or logs). Uses the `cryptography`
      library as an optional dependency: imported lazily, `Recommends:` in
      the packages, `ferdim[hls-aes]` for pip; without it the job fails
      with a clear `hls_crypto_missing` code. Other methods and key
      formats (`SAMPLE-AES`, non-`identity` `KEYFORMAT`) are DRM and fail
      with `hls_drm` (see "Explicitly not planned").
- [ ] DASH: concatenate every period of a static MPD and add the best
      audio representation instead of dropping it.
- [ ] Bounded manifest reads (HLS / DASH size limit like `fetch_small`).
- [ ] Ranges: honour a shorter `206` end the server chooses; degrade to a
      single stream when a server advertises ranges but ignores them;
      fall back to a plain GET when the `bytes=0-0` probe itself is
      rejected.
- [ ] Respect a long `Retry-After` with a job-level "not before" gate
      instead of 30-second naps per worker.
- [ ] TLS options: custom CA bundle, client certificates (mTLS) and a
      minimum TLS version, shared by HTTP, h3 and FTPS.
- [ ] Normalise hosts to IDNA once, so profiles, cookies and limits
      match whichever spelling the user pasted.
- [ ] File names: truncate to 255 **bytes** keeping the extension; parse
      `Content-Disposition` with `email.message` (RFC 6266 / 5987, all
      charsets) and keep the regex as a fallback.
- [ ] Running out of file descriptors (`EMFILE`) gets its own code and
      pauses instead of retrying; links are clamped to
      `RLIMIT_NOFILE`.
- [ ] Schedules and speed plans: a defined policy for DST transitions
      (spring-forward and fall-back) with tests in a synthetic time zone;
      `Retry-After` dates checked against the response `Date`.
- [ ] Coexistence on VPN and tunnel links: follow the default-route
      interface or a configured interface list instead of skipping
      `wg*` / `tun*`.
- [ ] Best-effort `posix_fallocate` with a sparse fallback.
- [ ] Clipboard: show a notification with undo (or ask) before queuing a
      copied URL.

## 1.13 — performance and scale

Target: 10 000 queued jobs, 8–16 running, 1 Gbit/s, with the web UI
responsive and the daemon under 5 % of a core when idle.

- [ ] Persist the queue incrementally (dirty flag + debounce or SQLite
      like history), never rewriting all jobs under the queue lock per
      add or state change (a 500-file import is O(n²) today).
- [ ] Control verbs return immediately: resume, cancel and "all" run in
      the background instead of blocking a request thread on probes and
      joins.
- [ ] `stats()` without the queue lock held over every snapshot; deltas
      by revision (`since`) and paging (`limit` / `offset`) in the API.
- [ ] Push updates to the web UI over Server-Sent Events from the
      existing watcher channel; stop polling when the tab is hidden;
      incremental / virtualised job list; paged history with "load more".
- [ ] Non-blocking socket watchers with bounded per-client queues, so a
      slow client can't stall job threads (`sock.py:93`).
- [ ] Engine hot paths: `pwrite` outside the job lock, a heap for
      pending ranges with a running byte total, notify-driven worker
      wakeups and token bucket (no 0.3 s / 0.2 s polling).
- [ ] Keep-alive connection reuse per source (TLS session resumption),
      so short ranges stop paying a full handshake each.
- [ ] Bounded size prober (2–3 in parallel) that forgets removed jobs.
- [ ] Benchmarks in CI with stored baselines: engine throughput against
      a local server, chunker MB/s, queue load / save with 10 000 jobs,
      `stats()` latency, idle CPU and RSS; a regression of more than 20 %
      fails the build.

## 1.14 — API platform and observability

- [ ] Versioned API (`/api/v1/…`) with a machine-readable spec
      (`docs/openapi.yaml`) generated from the dispatch table, a test that
      every command is documented, and a deprecation policy.
- [ ] Stable error codes on **every** API failure (some still return only
      a Turkish sentence) and no exception text in HTTP 500 replies;
      every code translated in all UIs.
- [ ] Read-only vs full-control tokens (dashboards and `/metrics` get
      read-only), with rotation and revocation.
- [ ] Per-client rate limits on `/api/*`, `/metrics` and the socket;
      handler timeouts and a connection cap on the HTTP server; security
      headers (`Content-Security-Policy` with a nonce,
      `X-Content-Type-Options`, `frame-ancestors`, `Referrer-Policy`).
- [ ] Validate `headers`, `cookies` and `proxy` (no control
      characters, size caps) and cap `add-batch` item counts.
- [ ] Optional private-address guard: refuse loopback, link-local and
      RFC 1918 targets from metalinks and the API unless allowed.
- [ ] Apply `web_port` / `web_enabled` changes live (rebind) instead of
      only at the next start.
- [ ] Structured engine logging: module loggers, rate-limited warnings,
      job ids in every line, no secrets, size-based log rotation; a
      counter in `/metrics` for every error class the engine now
      swallows silently.
- [ ] Stored credentials: document what is kept, add "forget stored
      credentials", and stop persisting session cookies by default.
- [ ] `ferdim doctor`: checks folders, permissions, ports, the native
      messaging manifest, the D-Bus name and versions, and prints a
      redacted support bundle.

## 1.15 — managed and multi-user deployments

- [ ] System-wide defaults and admin-locked settings: read
      `/etc/ferdim/config.json` (and `XDG_CONFIG_DIRS`) with a `locked`
      list that `config-set` and every UI respect and show as locked.
- [ ] Policies an admin can enforce: allowed / blocked hosts and
      schemes, maximum links and speed, peer cache and HTTP/3 on/off,
      download-folder roots, extension takeover rules (Firefox enterprise
      policy for the extension's managed storage).
- [ ] Documented single-user model: one daemon per uid, refuse to run as
      root or with a group- or world-accessible socket.
- [ ] Audit log (opt-in): who added, removed or changed what and when,
      in JSON lines, without URLs' query strings or credentials.
- [ ] Proxy auto-configuration: follow the desktop proxy settings
      (GSettings / portal) as well as environment variables.
- [ ] Extension: narrow `webRequest` observation to the request types it
      needs, normalise every loopback form in the self-download guard, a
      user deny-list for takeover, and a restore path for downloads it
      should not have taken.
- [ ] GNOME Shell extension localised (`gettext-domain`, `po/`),
      required for extensions.gnome.org.
- [ ] Web UI renders `lang` / `dir` from the negotiated locale on first
      paint; history refreshes after a re-download; malformed URLs in
      the grab list can't break the popup.

## 1.16 — quality engineering

- [ ] Coverage measured in CI (including subprocesses: daemon, socket
      server, native host) with a floor that only rises; 72 % today with
      `cli.py` at 45 %, `server/sock.py` and `nativehost.py` at 14 %,
      `daemon.py` and the GTK app at 0 %. Target ≥ 90 % for `ferdim/`.
- [ ] Unit tests for the CLI parser and every verb, the socket framing
      (`req_id` vs `id`), the clipboard watcher and the daemon lifecycle.
- [ ] Run the existing optional gates in CI: `tests/ui/run.sh web`
      (Playwright), `gtk` (Xvfb), and `tests/cli_smoke.sh` against a
      freshly installed build.
- [ ] Static analysis on every PR: `ruff` (61 findings today),
      `mypy` (blocking for core modules), `shellcheck` for `scripts/` and
      `bin/`, `web-ext lint`, and CodeQL for Python and JavaScript.
- [ ] Wider matrices: Python 3.9 through 3.15 (the declared floor is
      3.9, CI starts at 3.11), aarch64, and the suite inside Fedora,
      openSUSE and Arch containers on PRs, not only at tag time.
- [ ] State-migration tests from real 1.4 / 1.5 / 1.6 / 1.7 state files,
      upgrade and downgrade.
- [ ] Property tests (stdlib, seeded) for size parsing, schedules, range
      bookkeeping and config sanitising; a mutation-testing run on
      `job.py` and `util.py` before each minor release.
- [ ] Nightly fuzzing: longer runs, random seeds, and new targets for the
      state files (`config.json`, `queue.json`, `hosts.json`,
      `.ferdim-extract.json`).
- [ ] Flaky-test detection: repeat the timing-sensitive suites three
      times in CI and fail on any disagreement.
- [ ] One version source: a test that every version string (package,
      spec, PKGBUILD, manifests, metainfo, man pages) matches
      `ferdim.__version__`; tests that README / docs numbers match
      reality.
- [ ] Man page checks cover `ferdim-dbus.1` too.

## 1.17 — packaging, release and supply chain

- [ ] Fix the Python package: console scripts and the web UI as package
      data, so `pip install ferdim` (and `[h3]`) actually works; test it
      by installing a wheel and serving the UI.
- [ ] Release workflow: create the GitHub release, attach every artefact
      (RPM, SRPM, Flatpak, GNOME zip, wheel, checksums, SBOM) and fail the
      tag if one is missing; no manual steps left in the checklist.
- [ ] Build provenance (SLSA attestations) for release artefacts.
- [ ] CI hygiene: actions pinned to commit SHAs, Dependabot for actions,
      secret scanning with push protection, `permissions`,
      `concurrency` and timeouts on `ci.yml`, packaging lint (`rpmlint`,
      desktop file, AppStream) on PRs.
- [ ] Ship the AppStream metainfo in the RPM and other packages so
      software centres show a proper listing.
- [ ] SELinux: confirm the daemon, socket and native host work under
      enforcing targeted policy; ship a module only if one is needed.
- [ ] Complete uninstall: `install.sh --uninstall` also disables and
      removes the systemd user unit; package scriptlets tested for
      upgrade, downgrade and removal.
- [ ] AUR PKGBUILD carries the real checksum instead of `SKIP`.
- [ ] Rollback story: documented downgrade path between adjacent
      releases, backed by the downgrade-safety tests above.

## 1.18 — documentation and project governance

- [ ] `SECURITY.md` (private reporting, supported versions, response
      times) and `docs/threat-model.md` (local users, browser pages,
      LAN peers, servers, mirrors, proxies).
- [ ] User guide, administrator guide (policies, system defaults,
      hardening, monitoring) and API reference built from the spec.
- [ ] `docs/architecture.md`: the engine invariants from `AGENTS.md`
      written for human contributors.
- [ ] Support and deprecation policy: semantic versioning, how long an
      LTS line gets fixes, how API and config changes are announced.
- [ ] Governance files: `CODEOWNERS`, issue and PR templates, a code of
      conduct, and a sign-off (DCO) rule in `CONTRIBUTING.md`.
- [ ] Fix stale docs (test counts in README and
      `docs/fedora-submission.md`, "MV2" in the README tree, the tag in
      `docs/flatpak.md`) and keep an `## [Unreleased]` CHANGELOG section.
- [ ] Accessibility audit of the web UI, GTK app, popup and shell
      extension with a screen reader and keyboard only, against
      WCAG 2.2 AA.

## 3.0 LTS — the definitive release

3.0 adds no features. It ships when every box below is ticked, and it
then receives security and data-integrity fixes for at least two years.

- [ ] Every item in 1.7.x through 1.18 is done, or moved to "Explicitly
      not planned" with a reason.
- [ ] Zero open high or medium findings from a fresh independent code
      review and security audit of the release candidate.
- [ ] Coverage ≥ 90 %, mutation score recorded, fuzzing for 24 hours
      without a new crash, and a 72-hour soak run under fault injection
      with every file verified and flat memory and thread counts.
- [ ] Performance budgets met on reference hardware: line-rate transfer
      at 1 Gbit/s, 10 000 queued jobs with the UI responsive, idle CPU
      under 5 % of a core.
- [ ] Reproducible, signed artefacts with SBOM and provenance for every
      package format; upgrade from every release since 1.4 tested
      automatically.
- [ ] Distribution: official Fedora package, COPR, OBS (openSUSE, Debian,
      Ubuntu), AUR, Flathub, a signed AMO listing and extensions.gnome.org
      all at 3.0.
- [ ] Documentation complete: user, admin, API, architecture, security
      and support policy, in every shipped language.

## Explicitly not planned

- DRM-protected streams and live (non-VOD) HLS.
- Site-specific scraping or paywall/ad circumvention.
- Non-FOSS components of any kind.
- Windows/macOS ports (Linux-first; community forks would carry the
  burden).

## Help wanted

- i18n reviewers for Spanish and Russian strings; translators for the
  new locales.
- GTK 4 contributor for HiDPI and narrow-window layout polish.
- Packagers for non-Fedora distributions (Debian/Ubuntu, Flathub).
- Someone with Fedora packaging sponsorship history to shepherd the
  official review.
