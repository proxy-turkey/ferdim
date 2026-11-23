# Changelog

All notable changes to FerDim are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
semantic versioning.

## [Unreleased]

### Added
- The Firefox extension is published on addons.mozilla.org (listed,
  signed, auto-updating); `.github/workflows/amo.yml` submits each release tag.
- COPR repository `proxy-turkey/ferdim` (Fedora 43+, x86_64/aarch64),
  rebuilt automatically on every release tag by `.github/workflows/copr.yml`.
- Queue history in SQLite: every job that reaches a terminal state
  (`done`, `error`, `canceled`) is recorded in `history.db` in the state
  directory (mode 0600, WAL, versioned schema with forward migrations).
  The record holds the redacted URL, filename, host, size/bytes,
  duration, average and peak link counts, state, error code, checksum
  result and finish time; re-download parameters (headers, cookies,
  mirrors, checksums, per-job limit, credentials) are kept for the
  *re-download* action and never appear in listings. A database failure
  is logged and swallowed, so history can never block or fail the queue.
- API commands shared by the socket and HTTP servers: `history` (text
  search over file/URL/host, state filter, limit/offset),
  `history-redownload` (re-add with the saved URL and options),
  `history-stats` (total bytes, record count, per-host average links and
  speed) and `history-clear` (all records, or `older_than` days).
- CLI: `ferdim history [QUERY]` with `--state`, `--limit`, `--stats`,
  `--redownload ID`, `--clear [--older-than DAYS]`.
- Web UI: a download-history panel with search, state filter, statistics
  summary, re-download buttons and clear, keyboard navigable and built
  with `textContent` only.
- `history_days` config (default 365, `0` keeps forever), clamped by the
  config sanitiser and pruned at daemon start; exposed in the web
  settings dialog.
- SOCKS5 proxy support (`--proxy socks5://…` and `socks5h://…`) in pure
  standard-library Python — no extra package, so it ships by default while
  the engine keeps its stdlib-only rule. RFC 1928 CONNECT plus optional
  RFC 1929 username/password; credentials may come from the proxy URL
  (percent-decoded). `socks5h://` sends the hostname to the proxy so DNS
  does not leak locally, `socks5://` resolves it first. Each HTTP(S)
  connection opens its own tunnel; TLS keeps SNI and certificate
  verification against the target host, and per-read stall detection still
  applies inside the tunnel. FTP and FTPS route both the control connection
  and passive data connections through the same tunnel (active FTP is not
  possible over SOCKS and is rejected with a clear message). A proxy
  authentication or permission failure fails the job at once with the
  stable `auth` / `forbidden` error code instead of burning retries, and
  proxy passwords never appear in snapshots, logs or error messages.

## [1.5.0] — 2026-11-24

First 1.5 release; it also ships the 1.4.2 fixes below (1.4.2 was never
tagged on its own).

### Added
- Web UI speed graph: aggregate throughput and a live connection-count
  timeline over the last two minutes, plus a per-job speed sparkline in the
  queue. Canvas-based with no dependencies, HiDPI-aware, with textual ARIA
  summaries; history lives in client-side ring buffers pruned when a job
  disappears and is redrawn only on the 500 ms poll (no animation).
- API error codes: a failed job now carries a stable machine `error_code`
  (`not_found`, `forbidden`, `auth`, `source_changed`, `no_sources`,
  `checksum_mismatch`, `disk`, `network`, `stall`, `size_mismatch`,
  `incompatible`, `rate_limited`, `unknown`) next to the unchanged Turkish
  message, so interfaces can translate it (`err_*` keys exist in all four
  languages; the web UI and CLI use the translation and fall back to the raw
  message). Source warnings are exposed as `warning_codes`
  (`mirror_dead`, `checksum_mismatch`) and the per-mirror state as
  `mirror_health` (`{url, state, code}`).
- Background mirror probing: as soon as an HTTP job starts, every mirror is
  probed in a background thread, so an unreachable, wrong-size or
  range-less mirror is marked dead before a worker relies on it. The probe
  honours pause/cancel, never probes the source that is currently serving,
  and never fails the job by itself.
- FTP and FTPS downloads (`ftp://`, `ftps://`) through the standard library
  only. Credentials come from the URL userinfo (percent-decoded), the add
  form's user/password fields, or anonymous login; transfers use passive mode,
  obtain the size with `SIZE`, and resume an interrupted `.part` file with
  `REST` (validated against the server's `MDTM` when available). FTPS uses
  explicit `AUTH TLS` and an encrypted data channel (`PROT P`). Transient
  network, stall and 4xx failures retry with backoff from the checkpointed
  offset; permanent 5xx failures stop at once. Stall timeout, speed limits,
  pause/resume, checksums, crash-safe resume and verification-before-DONE
  behave exactly as for HTTP jobs. Watched URL lists and clipboard capture
  accept both schemes.

### Changed
- GTK front-end: the add window is a plain modal `Gtk.Window` instead of
  the deprecated dialog widget (no new dependency); Enter activates the
  default "Download" button, Escape closes it and every field keeps its
  mnemonic label.
- GTK front-end: new Settings window mirroring the web UI settings —
  downloads folder, language, connections and concurrent jobs, turbo,
  speed limit, night schedule and per-category windows, watched folders,
  clipboard watching, sidecar verification and advanced retries/timeouts.
  Settings are read with `config-get` and written with `config-set`.

### Fixed
- FTP transfers no longer treat a server-aborted data stream as complete: a
  clean data EOF must now be followed by a 2xx final reply, so a `426` (or any
  other non-2xx) can never mark a truncated file as done — especially when the
  server does not implement `SIZE`. Transient network, stall and 4xx failures
  retry with jittered backoff from the checkpointed offset; permanent 5xx
  responses fail immediately.
- FTP URLs are now redacted in queue snapshots and mirror health (the
  password is stripped, the user name is kept); the private `queue.json`
  state file is written with mode `0600`. `ferdim restart` re-probes the file
  size, and FTP size probes are capped at five seconds so a slow host cannot
  stall the smart queue.

## [1.4.2] — 2026-11-23

Bug-fix release for 1.4.1, found by an end-to-end run of the CLI against the
real daemon.

### Fixed
- `ferdim pause`, `resume`, `restart`, `cancel` and `remove <id>` never
  worked: the local-socket server mistook the job `id` for a request
  correlation id and dropped it from the parameters, so every job command ran
  with an empty id. The GTK per-job buttons were silently affected too (the
  web UI uses HTTP and was not). Request correlation now uses a separate
  `req_id` field; all job commands are covered by an end-to-end socket test.
- `pause` returned before the job had actually paused. It now returns once the
  worker pool has folded, the remaining ranges are queued and the checkpoint
  is written.
- Ranges being downloaded by a worker were missing from the checkpoint. Pausing
  (or crashing) while a connection was in the middle of a slow read could
  resume with a gap and end in "eksik veri". In-flight ranges are now part of
  every checkpoint and are re-downloaded from their last completed offset.
- A worker still blocked in a read when its job was paused and resumed could
  keep running next to the new workers. Workers now belong to a generation and
  exit quietly when it changes.
- A worker that died from an unexpected exception no longer takes its range
  with it.
- Pausing or cancelling a speed-limited job could take minutes: 32 workers
  waiting on the shared token bucket only noticed the stop signal when their
  turn came, and each worker was joined with its own timeout. The bucket wait
  is now aborted by the stop signal and workers are joined under one shared
  deadline — `ferdim pause <id>` returns in well under a second.
- Changing the default downloads folder retroactively rewrote the reported
  path of every existing job (a finished download appeared under the new
  folder, and re-queued after a daemon restart). Each job now keeps the
  folder it was added to, and the folder is persisted with the queue.

## [1.4.1] — 2026-11-23

### Added
- Source health model for the primary URL and its mirrors: work is balanced
  by smooth weighted round-robin over measured per-connection throughput,
  sources carry a soft concurrency cap (halved on HTTP 429/503, doubled back
  after a quiet minute), failures accumulate a streak with jittered
  cooldowns, and fail-over is transparent — a range interrupted on one
  source continues from another without requeueing.
- Stall detection: a connection that receives no data for `stall_timeout`
  seconds (default 20) releases its range to another source.
- `Content-Range` validation on every response: mirrors that ignore `Range`
  or serve a different file are rejected instead of restarting the job.
- Retry policy: exponential backoff with jitter (2·2ⁿ s, capped at 15 s,
  scaled ×0.5–1.5) so parallel workers never retry in lockstep.
- More digests: `--sha1`, `--sha512` and `--checksum algo:digest` (or a bare
  digest, detected by its length); all requested digests are computed in a
  single pass.
- Checksum sidecar auto-discovery: `<url>.sha256`, `.sha256sum`, `.sha512`,
  `.md5`, `.sha1` are fetched in the background and verified automatically.
  A stale sidecar that does not match marks the job with a warning and keeps
  the file; an explicit checksum that does not match still fails the job.
- Per-category schedule windows (`schedule.categories`), editable in the
  web UI settings.
- Watch folders: `.metalink` / `.meta4` documents and URL-list text files
  dropped into a configured folder are queued automatically
  (`watch_dirs`; `watch_done` = delete | rename | keep).
- `ferdim add @list.txt` and `ferdim add-file FILES...`.
- "Start immediately" switch when adding a download (web UI and GTK): the
  job is queued paused when it is off.
- Web UI: mirrors and checksum fields in the add dialog, verification /
  unavailable-source / schedule-window badges, and settings for watched
  folders, sidecar verification and advanced retries/timeouts.
- Web UI accessibility pass: server data is never parsed as HTML (rows are
  built with `textContent` and updated in place, so keyboard focus survives
  polling), ARIA tabs / table / progressbar roles, a skip link, a live
  region for finished and failed jobs, visible focus rings,
  `prefers-reduced-motion`, WCAG AA text and control contrast.
- GTK: accessible names on icon buttons, mnemonic labels in the add dialog,
  a removal confirmation, error dialogs, mirrors / checksum / start-paused
  fields, and automatic reconnection when the daemon restarts.
- Extension: `data_collection_permissions: none` declared (required by AMO
  for new extensions since November 2025); minimum Firefox raised to 140.

### Changed
- Size units are consistent everywhere: KB, MB and GB are 1024-based in
  `--limit`, `ferdim limit`, the web UI and the extension thresholds, so a
  limit entered as `2MB` is displayed as `2.0 MB/sn`.
- Choosing a speed limit while turbo mode is on now leaves turbo mode — the
  turbo button and the applied limit can no longer contradict each other.
  A concurrency value set during turbo mode survives turning turbo off.
- Job errors are short and readable (`kaynak bulunamadi (HTTP 404)`) instead
  of raw library exceptions.

### Fixed
- Packaging findings that `fedora-review` had been reporting since 1.4.0
  unnoticed: the shipped `.desktop` file is now validated at build time
  (`desktop-file-validate`), the systemd **user** unit gets its
  `systemd_user_post` / `systemd_user_preun` scriptlets, and the package
  owns `/usr/share/mozilla/native-messaging-hosts`
  (`Requires: mozilla-filesystem`, `Requires: hicolor-icon-theme`).
- A mirror that returned 404, or a different file size, failed the whole
  job; it is now dropped and the remaining sources finish the download.
- A job was reported `done` before its checksum was verified, so
  `ferdim add --wait`, the extension and the web UI could see a finished
  job that turned into an error a moment later. Verification now completes
  before the finished state is published; a sidecar that arrives late is
  still verified afterwards.
- Changing the downloads folder in the settings dialog had no effect until
  the daemon restarted. An unusable folder now falls back to the default
  with a notice instead of breaking the API.
- The "Start immediately" checkbox in the web UI did nothing.
- An invalid checksum given from the command line or the UI was silently
  ignored; it is now rejected with a message.
- Header tooltips in the web UI stayed in Turkish in other languages.
- `ferdim config --set` could drop per-category schedule windows.
- The paused state of a job showed a second, ineffective "Pause" button.
- MiB / GiB suffixes were not recognised by the size parser.

## [1.4.0] — 2026-10-21

### Added
- DASH (MPD) downloads: best-representation selection, SegmentTemplate
  (`$Number$`/`$Time$`/SegmentTimeline) and SegmentList, parallel segment
  fetch with per-segment resume (shared with the HLS pipeline).
- Multi-mirror downloads: repeatable `--mirror`, round-robin blending
  across primary and mirrors, mirror size validation.
- Metalink / `.meta4` (RFC 5854) expansion: preference-sorted mirrors and
  checksums are applied to each queued file.
- Per-job integrity verification: `--sha256` / `--md5` (also in the web UI);
  mismatches mark the job as failed without deleting data.
- ZIP preview: list archive contents from the web UI and the API without
  opening external tools.
- Smart queue: a background prober learns file sizes and schedules
  smallest-first so short downloads never wait behind giants.
- Event-driven clipboard watching on Wayland (`wl-paste --watch`) with a
  polling fallback.

### Changed
- WebExtension hardened for linter cleanliness: no dynamic `innerHTML`
  interpolation, `pageAction.show` instead of non-standard `open`.
- `python -m ferdim.gui` now works (GTK app gained `__main__`).

## [1.3.3] — 2026-10-21

### Changed
- Maintainer metadata across the project: named copyright holder
  (Enes Aksu), RPM changelog packager, README author section,
  WebExtension `author` field, man page author section.

## [1.3.2] — 2026-10-21

### Changed
- Packaging polish: `/usr/share/mozilla` manifest location, user-unit
  scriptlets dropped (systemd file triggers), man pages for `ferdim`,
  `ferdim-nm` and `ferdim-gui`; hermetic per-run test roots.

## [1.3.1] — 2026-10-21

### Changed
- Review-grade RPM packaging: `%check` runs the full test suite at build
  time, man page (`ferdim(1)`), proper systemd user scriptlets, centralized
  native-messaging registration under `/usr/lib*/mozilla`, arch-independent
  payload at `/usr/share/ferdim`, executable wrappers locate both packaged
  and user-level installs.

## [1.3.0] — 2026-10-21

### Added
- **Throughput governor**: continuously measures aggregate speed and
  calibrates the connection count with upward/downward probes to reach
  whichever ceiling is reachable (line or server), then keeps efficiency
  probes running.
- **Adaptive server-limit handling**: HTTP 429/503 and crowded connection
  resets/refusals shrink the worker pool instead of failing the job;
  `Retry-After` honored; learned floor remembered across resume and
  re-explored after a penalty window.
- **Dynamic range sizing** (~8 s of work per bite, 256 KB–32 MB) and
  **end-game tail donation** to parked workers.
- **Parked worker pool**: shrinking parks threads; growth re-arms them
  instantly.
- Probe retries on 429/503; clean single-stream fallback for servers
  without byte ranges.
- Scaled HLS worker pool (1–8 by segment count).
- Live connection count and adaptive-limit indicator in UIs (4 languages).

## [1.2.0] — 2026-10-21

### Changed
- **Maximum power is the default**: 32 links, unlimited speed, 8 concurrent
  jobs out of the box; the turbo toggle now serves as a politeness switch.

## [1.1.0] — 2026-10-21

### Added
- Turbo (max power) mode with per-surface toggles (CLI, web, GTK, extension).
- Internationalization: Turkish, English, Spanish, Russian across web UI,
  extension (`_locales`), GTK front-end and CLI.

## [1.0.0] — 2026-10-21

### Added
- Segmented multi-connection engine (1–32 links) with work stealing.
- Crash-safe resume with ETag/Last-Modified validation and automatic restart
  on source change.
- Firefox WebExtension + native messaging host: download takeover, media
  sniffing with badge/floating button, link grabber with filters and batch
  send, options page.
- Queue manager with concurrency limit, night-window scheduler, global and
  per-job speed limits, optional clipboard watcher.
- Daemon with UNIX-socket JSON-RPC and token-protected localhost HTTP API;
  web UI, GTK 4 app, full CLI; systemd user service; RPM packaging.
