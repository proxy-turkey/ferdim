# Changelog

All notable changes to FerDim are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
semantic versioning.

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
