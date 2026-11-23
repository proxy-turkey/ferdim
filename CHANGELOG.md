# Changelog

All notable changes to FerDim are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
semantic versioning.

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
