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

Every item here closes a gap that 1.6 shipped knowingly.

- [ ] Manifest V3 build: replace `tabs.executeScript` in the link-grab path
      with the `scripting` API, keep event-page state in
      `storage.session`, ship MV3 once `scripts/build_mv3.sh` lints 0/0
      and loads (`docs/mv3-spike.md` has the plan).
- [ ] Host profiles in the web UI and GTK app (today: CLI and API only),
      with per-host "forget"; let HLS/DASH segment pools and FTP jobs learn
      too.
- [ ] HTTP/3: reuse one QUIC connection per origin instead of one per
      range; carry sidecars and HLS/DASH segments over h3 when enabled.
- [ ] Coexistence mode: add a latency signal (RTT inflation on our own
      links) next to foreign-traffic accounting, so a busy line is detected
      before it saturates.
- [ ] Merkle: show `merkle` / `checksum` warnings in the web UI and GTK
      app; accept Merkle manifests inside metalink documents.
- [ ] GTK app parity: archive browsing, history and coexistence toggle.
- [ ] GNOME Shell extension on extensions.gnome.org (reviewed, versioned
      with the release).

## 1.8 — reach and robustness

- [ ] Flatpak on Flathub (daemon + GTK app + native-messaging bridge
      for sandboxed Firefox).
- [ ] Fuzz the parsers that read untrusted input — m3u8, MPD, metalink,
      `.ferdim-merkle`, archive listings, SOCKS replies — with a
      stdlib-only harness in CI.
- [ ] Time-of-day speed profiles (e.g. 2 MB/s during work hours, maximum
      power at night) on top of the existing schedule windows.
- [ ] Metrics endpoint: per-host and aggregate counters (bytes, links,
      retries, throttles) in a plain text format for local monitoring.
- [ ] Faster chunker for `ferdim merkle make` (today ~6 MB/s in pure
      Python; verification is already hashlib-bound).

## 2.x — research

- [ ] LAN peer cache (opt-in): machines on the same network share
      already-verified chunks by Merkle hash instead of fetching them again
      from the internet.
- [ ] Publisher tooling: a small static-site helper that emits
      `.ferdim-merkle` and metalink files next to release artefacts.
- [ ] Congestion awareness: read the kernel's TCP info (RTT, retransmits)
      per link to steer the governor, not only throughput.

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
