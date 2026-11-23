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
- [ ] openSUSE OBS and Arch AUR community packages.
- [ ] Extension published on AMO (signed) with an update URL; MV3
      migration spike to stay ahead of MV2 deprecation.
- [ ] More locales: German, French, Portuguese (Brazil), Arabic —
      community-reviewed.
- [x] FTP/FTPS downloads via stdlib `ftplib`.
- [ ] SOCKS5 proxy support as an optional pure-Python extra
      (engine default remains stdlib-only).
- [x] Web UI speed graph (per-job and aggregate) and a live
      connection-count timeline; canvas-based, no dependencies.
- [ ] Queue history in SQLite: search, re-download, aggregate
      statistics (bytes served, average link counts per host).
- [x] Carried over from 1.4.1: probe mirrors in the background (not only
      when a worker first needs them) so an unreachable mirror is reported
      before it slows anything down; error codes in the API so the UIs can
      translate engine messages (the engine's own strings are Turkish).
- [x] GTK front-end: migrate the add dialog from the deprecated
      `Gtk.Dialog` to `AdwDialog` / `Gtk.AlertDialog` and add a settings
      window on par with the web UI.

## 1.6 — desktop integration

- [ ] D-Bus interface (`org.ferdim.*`) for desktop shells; a GNOME
      Shell extension driving the daemon.
- [ ] Extension media sniffing v2: generic response-header analysis to
      catch more HLS/DASH manifests and large-file direct links
      (strictly generic — no site-specific scraping, ever).
- [ ] Learned per-host connection profiles: remember the observed
      link ceiling, RTT band and per-link cap per host class; apply
      them as the starting point for new jobs.
- [ ] Archive browsing: nested ZIP listing, selective extraction,
      partial-extract repair.
- [ ] Desktop notifications (portals) for job completion and
      verification results.

## 2.0 — research / stretch

- [ ] HTTP/3 (QUIC) transport as an optional extra
      (`pip install ferdim[h3]`); the stdlib path stays the default.
- [ ] Content-defined chunking with Merkle verification: integrity
      that survives re-downloads of only the changed ranges.
- [ ] Coexistence mode (opt-in): automatically shed links when
      interactive traffic is detected on the line. Default stays
      maximum power; this is the polite opt-out made smarter.
- [ ] Localized man pages (Turkish first).

## Explicitly not planned

- DRM-protected streams and live (non-VOD) HLS.
- Site-specific scraping or paywall/ad circumvention.
- Non-FOSS components of any kind.
- Windows/macOS ports (Linux-first; community forks would carry the
  burden).

## Help wanted

- i18n reviewers for Spanish and Russian strings.
- GTK 4 contributor for HiDPI and narrow-window layout polish.
- Packagers for non-Fedora distributions.
- Someone with Fedora packaging sponsorship history to shepherd the
  official review.
