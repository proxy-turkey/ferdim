# FerDim

**A dynamic, self-tuning download manager for Linux — built Fedora-first, with native Firefox integration.**

![CI](https://github.com/proxy-turkey/ferdim/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![Languages](https://img.shields.io/badge/UI-tr%20%7C%20en%20%7C%20es%20%7C%20ru-green.svg)

FerDim saturates whatever ceiling is reachable — your line **or** the remote
server — and adapts continuously while a download runs. It is pure Python
(stdlib only for the engine), ships a Firefox WebExtension, and offers three
front-ends: CLI, web UI, and GTK 4.

```
┌──────────┐  native messaging   ┌────────────┐  Unix socket / HTTP   ┌────────────┐
│ Firefox  │◄───────────────────►│ ferdim-nm  │◄────────────────────►│  daemon    │
│ extension│  (4-byte + JSON)    │  (bridge)  │   (JSON-RPC)          │ (queue +   │
└──────────┘                     └────────────┘                       │  engine)   │
                                                                      └─────┬──────┘
   ┌────────────┐   ┌─────────────┐   ┌──────────────────┐                 │
   │ ferdim CLI │   │ ferdim-gui  │   │ Web UI           │◄────────────────┘
   └────────────┘   │ (GTK 4)     │   │ 127.0.0.1:8378   │   segmented, self-tuning
                    └─────────────┘   └──────────────────┘   engine (1–32 links)
```

| Web UI | GTK 4 app |
|:---:|:---:|
| ![Web UI](docs/screenshots/web-ui.png) | ![GTK app](docs/screenshots/gtk-gui.png) |

## Why it is fast — and smart

| Mechanism | What it does |
|---|---|
| **Throughput governor** | Measures aggregate speed every 2.5 s. Probes **+1 link** upward when speed keeps improving (servers that throttle per connection are scaled out this way); probes **downward** when speed is pinned at a ceiling, releasing redundant connections without losing throughput. Re-probes periodically as conditions change. |
| **Adaptive server limits** | HTTP `429/503`, connection resets or refusals while crowded → the pool halves (32 → 16 → … → 1), `Retry-After` is honored, remaining ranges are re-queued, and the job **never fails** because of throttling. The learned floor is remembered across resumes — but re-explored once the penalty window passes. |
| **Dynamic range sizing** | Each worker takes a bite sized to its measured throughput (~8 s of work, 256 KB – 32 MB): fewer reconnects on fast servers, more parallelism on slow ones. |
| **End-game donation** | Near the end of a file, workers holding large remainders donate tail ranges to parked workers, so the queue never collapses to a single connection. |
| **Parked worker pool** | Shrinking parks threads instead of killing them; the governor can re-arm them instantly. |
| **Crash-safe resume** | Sparse `.part` files in the target directory, metadata checkpointed every 2 s, `If-Range` (ETag / Last-Modified) validation; changed sources restart automatically. |
| **Big sockets** | Every connection requests a large `SO_RCVBUF` so the kernel keeps deep receive queues per link. |
| **HLS assembler** | Picks the best variant, fetches segments in parallel (1–8 workers), assembles in order, resumes per segment. |
| **DASH (MPD)** | Selects the best representation by resolution/bandwidth, resolves SegmentTemplate (`$Number$`/`$Time$`/SegmentTimeline) and SegmentList, then drives the same parallel-segment pipeline. |
| **FTP / FTPS** | Single-stream downloads over `ftplib`: credentials from the URL, the add form or anonymous; passive mode; `SIZE` for progress/ETA and `REST` for resuming a partial `.part` after a pause or crash, validated with the server's `MDTM` when offered. Transient failures retry from the last checkpoint. `ftps://` negotiates explicit TLS with an encrypted data channel. The same stall timeout, speed limit, checksum and verification rules apply. |
| **Proxies** | `--proxy` accepts `http(s)://` and `socks5://` / `socks5h://` (RFC 1928 with optional RFC 1929 username/password). SOCKS5 is pure stdlib: each connection gets its own tunnel, TLS keeps its SNI and verification against the target host, and stall detection still runs per read. `socks5h://` leaves DNS to the proxy (no local leak); FTP/FTPS control **and** passive data connections share the same tunnel. Proxy passwords never appear in job snapshots or error messages. |
| **Mirrors & metalink** | Repeatable `--mirror` URLs are blended into one job; `.metalink` / `.meta4` documents expand into queued files with preference-sorted mirrors and checksums. |
| **Source health** | Work is balanced across the primary URL and mirrors by measured per-connection throughput (smooth weighted round-robin). A source that fails, stalls (no data for `stall_timeout` s) or serves a different file (`Content-Range` is checked on every response) is demoted, and the interrupted byte range **continues from another source** — one dead mirror never fails a job. Retries back off exponentially with jitter. |
| **Integrity** | Per-job `--sha256` / `--md5` / `--sha1` / `--sha512` / `--checksum algo:digest`, all computed in one pass. Checksum sidecar files next to the URL (`.sha256`, `.md5`, …) are discovered automatically. Verification completes **before** a job is reported as finished; an explicit mismatch fails the job while keeping the data for inspection. |
| **Automation** | Watched folders queue `.metalink` / `.meta4` documents and URL-list text files automatically; schedule windows can be set per category; `ferdim add @list.txt` queues a whole list. |
| **Smart queue** | A background prober learns the size of queued files and runs smallest-first, so a 2 MB PDF never waits behind a 4 GB ISO. |
| **Live telemetry** | The web UI plots aggregate throughput and the open-connection count over the last two minutes, plus a per-job speed sparkline — plain canvas, no chart library, client-side ring buffers. |
| **Queue history** | Finished jobs (done, error, canceled) are stored in a SQLite database in the state directory (`history.db`, mode 0600, WAL). Search by file, URL or host, filter by state, re-download with the saved URL and options, and read totals plus per-host averages (links, speed). Retention is `history_days` (default 365, `0` keeps forever); a URL's password is stored only for re-download, never in the searchable record. |
| **Learned host profiles** | Each host remembers the link count the governor settled at, its safe ceiling after 429/503, a response-time band and the per-link speed (`hosts.json`, 500 hosts, 90-day expiry). New jobs start there instead of cold; the governor still probes both ways. `ferdim hosts` lists them, `learn_hosts = false` turns learning off. |
| **Merkle verification** | A `.ferdim-merkle` manifest (content-defined chunks, SHA-256 per chunk, Merkle root) is discovered next to the URL or given with `--merkle`. A corrupt chunk is re-fetched **by range only** (at most twice) before the job completes; `ferdim merkle update` rebuilds a new version of a file from an old local copy and fetches only the changed chunks. |
| **Coexistence mode** (opt-in) | `ferdim coexist on`: while other traffic on the line exceeds `coexist_threshold` (15 % of observed capacity) the queue halves every job's links, and rebuilds them after 10 quiet seconds. Off by default — maximum power stays the default. |
| **HTTP/3** (optional) | `--http3 auto` (or the `http3` setting) uses QUIC when the server advertises it in `Alt-Svc` and falls back to TCP on any failure; `force` never falls back. One QUIC connection per origin carries every range, sidecar and HLS/DASH segment as separate streams. Needs the optional `aioquic` module (`python3-aioquic`, `pip install ferdim[h3]`); without it nothing changes. |
| **Archives** | ZIP (nested up to three levels) and tar (`.tar`, `.gz`, `.xz`, `.bz2`) listing, selective extraction and repair of an interrupted extraction (`.ferdim-extract.json` records size + CRC per member; only damaged members are rewritten). Path traversal, escaping links and decompression bombs are rejected. |
| **Desktop integration** | Desktop notifications for completions, failures and verification results (portal → freedesktop → `notify-send`, bursts coalesced; `notify`, `notify_errors_only`). `ferdim-dbus` exports `org.ferdim.Daemon1` on the session bus (D-Bus activated); a GNOME Shell extension (47–49) shows speed and job controls in the panel. |
| **Speed plan** | `speed_plan` rules (`mon-fri 09:00–18:00 → 2MB`, crossing midnight allowed, first match wins) set the global limit by time of day; a manual `ferdim limit` overrides the plan until `ferdim limit plan`. `ferdim speedplan`, web settings editor. |
| **Congestion awareness** | Range workers read the kernel's `TCP_INFO` (RTT, retransmits) per link; when an extra link brings no throughput while retransmits and RTT climb, the governor stops probing upward (`adaptive = congestion`). `tcp_info = false` disables it. |
| **LAN peer cache** (opt-in) | `peer_cache = true`: machines on the same LAN announce the Merkle roots they hold and serve chunks by SHA-256 only (private source addresses, rate-limited). A job with an explicit Merkle manifest takes matching chunks from peers — each verified before it is written — and fetches only the rest from the origin. |
| **Metrics** | `GET /metrics` (token header required) in Prometheus text format: bytes, retries and 429/503 throttles per host, jobs per state, active links, aggregate speed, coexistence state. |
| **Publisher tooling** | `ferdim publish DIR --base-url URL` writes `.ferdim-merkle`, `.sha256` and RFC 5854 `.meta4` files (plus `index.meta4`) next to release artefacts, byte-stable across runs — see [docs/publishing.md](docs/publishing.md). |

Also included: queue with concurrency limits and a night-window scheduler,
watched folders, global + per-job speed limits, clipboard watching (event-driven on Wayland),
HTTP and SOCKS5 proxy support, Basic-Auth, custom headers/cookies/referer/UA passthrough
from the browser extension, generic media sniffing from response headers
(HLS/DASH manifests, large direct files), and graceful
single-stream fallback for servers without byte ranges.

**Deliberate limits:** DRM-protected streams and live (non-VOD) HLS are not
downloaded; site-specific scraping is out of scope (generic media sniffing
only); `blob:` URLs are not reachable from outside the browser; FTP/FTPS is
single-stream (no segmentation or mirror blending) and its resume checks the
server's `MDTM` only when the server offers it — otherwise a remote file
replaced with the same size cannot be detected; SOCKS5 proxies carry TCP
transfers only, and FTP over SOCKS5 requires passive mode (active mode
cannot traverse a CONNECT tunnel).

## Installation

### Fedora (recommended)

The RPM is verified review-ready: it builds in a sterile `mock` chroot with
the full test suite running inside `%check`, and `rpmlint` is clean. See
[docs/fedora-submission.md](docs/fedora-submission.md) for the path into the
official repositories, and [docs/packaging.md](docs/packaging.md) for COPR.

Fedora 43 and newer (x86_64, aarch64) — from COPR, rebuilt automatically on
every release tag:

```bash
sudo dnf copr enable proxy-turkey/ferdim
sudo dnf install ferdim
```

openSUSE Tumbleweed and Leap 16.0 (x86_64, aarch64) — from OBS:

```bash
sudo zypper addrepo https://download.opensuse.org/repositories/home:proxy-turkey/openSUSE_Tumbleweed/home:proxy-turkey.repo
sudo zypper install ferdim
```

(Use `16.0` instead of `openSUSE_Tumbleweed` on Leap 16.0.)

Flatpak: `packaging/flatpak/` (app id `io.github.proxy_turkey.FerDim`); every
release attaches a `.flatpak` bundle — see [docs/flatpak.md](docs/flatpak.md).

Arch Linux: `packaging/aur/PKGBUILD` builds with `makepkg -si`; the AUR
package `ferdim` is updated from the same file on every release tag.

Other distributions: install from a release tarball or from source (below).

### From source (any modern Linux, user-level)

```bash
git clone https://github.com/proxy-turkey/ferdim.git
cd ferdim
bash scripts/install.sh
```

This installs `~/.local/bin/{ferdim,ferdim-nm,ferdim-gui}`, the desktop entry
and icon, Firefox native-messaging registrations (RPM, Flatpak and LibreWolf
profile roots), a systemd user service, and the extension ZIP.

Optional: `sudo dnf install python3-gobject` for the GTK 4 front-end.

Enable the background service (recommended):

```bash
systemctl --user enable --now ferdim
```

Uninstall: `bash scripts/uninstall.sh` (add `--purge` to remove settings and
queue state).

### RPM build

```bash
mkdir -p ~/rpmbuild/SOURCES
tar czf ~/rpmbuild/SOURCES/ferdim-1.3.0.tar.gz \
    --transform 's,^ferdim,ferdim-1.3.0,' ferdim
rpmbuild -bb packaging/ferdim.spec
sudo dnf install ~/rpmbuild/RPMS/noarch/ferdim-*.noarch.rpm
```

## Firefox integration

The extension takes over browser downloads, detects media on pages, and adds
context-menu items. Install it from addons.mozilla.org (signed, auto-updating):
<https://addons.mozilla.org/firefox/addon/ferdim-integration-module/>.
New versions are submitted automatically on every release tag
(`.github/workflows/amo.yml`).

**Quick trial / development:** `about:debugging#/runtime/this-firefox` →
*Load Temporary Add-on* → `~/.local/share/ferdim/ferdim-extension.zip`

Once installed: downloads are handed to FerDim automatically (configurable:
all / media only / off), a floating button appears when a page serves video or
audio, and the toolbar popup lists caught media plus a full link-grabber with
filters and batch sending.

## Usage

```bash
ferdim add https://example.com/big.iso --wait   # download and watch progress
ferdim list                                     # queue
ferdim pause all | ferdim resume <id>           # control
ferdim limit 2MB                                # politeness mode
ferdim turbo off|on                             # default is max power
ferdim web                                      # open the web UI
ferdim add https://example.com/big.iso --mirror https://mirror.example.com/big.iso
ferdim add https://example.com/data.metalink             # mirrors + checksums
ferdim add https://example.com/big.iso --sha256 <digest> # verify when done
ferdim add https://example.com/distro.iso --checksum sha512:<digest> \
        --mirror https://mirror1.example.org/distro.iso --mirror https://mirror2.example.org/distro.iso
ferdim add @liste.txt                           # queue every URL in a file
ferdim add ftp://example.org/pub/distro.iso --wait       # FTP or ftps:// (anonymous or user:pass in URL)
ferdim add https://example.com/big.iso --proxy socks5h://user:pass@127.0.0.1:1080
                                                        # HTTP(S)/FTP(S) through a SOCKS5 proxy
ferdim config --set '{"language":"ru"}'
ferdim config --set '{"watch_dirs":["~/İndirilenler/ferdim-kuyruk"]}'   # auto-queue dropped lists
ferdim history                                 # finished jobs, newest first
ferdim history debian --state done              # search file/URL/host + state
ferdim history --stats                         # bytes served, per-host averages
ferdim history --redownload <id>                # queue the same URL/options again
ferdim history --clear --older-than 30          # retention (history_days config)
ferdim hosts                                    # learned per-host link profiles
ferdim archive list <id> --nested               # nested ZIP / tar listing
ferdim archive extract <id> docs/a.txt --dest ~/out   # selective extraction
ferdim archive repair <id>                      # fix an interrupted extraction
ferdim merkle make big.iso                      # publish big.iso.ferdim-merkle
ferdim merkle update old.iso https://example.com/new.iso --manifest new.iso.ferdim-merkle
ferdim coexist on                               # shed links while the line is busy
ferdim add https://example.com/big.iso --http3 auto    # QUIC when advertised
ferdim speedplan set '[{"days":"mon-fri","from":"09:00","to":"18:00","limit":"2MB"}]'
ferdim publish ./release --base-url https://example.org/dl   # .ferdim-merkle + .meta4
curl -s -H "X-FerDim-Token: $(cat ~/.local/state/ferdim/token)" http://127.0.0.1:8378/metrics
```

- **Web UI** — `http://127.0.0.1:8378`: live queue, categories, speed/ETA,
  per-job controls, throughput and connection graphs, download history with
  search, re-download and statistics, settings, scheduler
  (global and per-category), watched folders, language picker. Fully
  keyboard- and screen-reader-friendly.
- **GTK 4 app** — `ferdim-gui` or from your app menu.
- **GNOME Shell** — `bash scripts/install.sh` installs the panel extension
  when GNOME Shell is present (package users: see
  `/usr/share/ferdim/gnome-shell-extension/INSTALL.md`), then
  `gnome-extensions enable ferdim@proxy-turkey.github.io`.
- **CLI** — full control; `ferdim --help`.

Defaults are *maximum power*: up to 32 links per file, no speed limit, 8
concurrent jobs. The governor then continuously tunes link count to the
reachable ceiling. Use `ferdim turbo off` or `ferdim limit` to share the line.

## Security model

- The daemon's RPC socket is a UNIX domain socket with `0600` permissions.
- The HTTP API binds to `127.0.0.1` only and requires a per-install token
  (`X-FerDim-Token`, constant-time compare). Cross-origin sites cannot call
  it: responses require the header, and no CORS headers are ever emitted.
- The native-messaging host is registered only for FerDim's own extension ID.

## Internationalization

Turkish, English, Spanish and Russian ship built-in. The web UI auto-detects
the browser language (overridable in settings), the extension follows the
Firefox UI locale, and the CLI/GTK apps follow the system locale or
`language` in the config. Adding a language is a single dictionary in
[`ferdim/i18n.py`](ferdim/i18n.py) plus `_locales/<lang>/messages.json`.

## Development

```bash
python3 -m unittest discover -s tests -v   # 20 tests, no external deps
```

The suite (26 tests) spins up local HTTP and FTP servers (range support,
throttling, connection limits, auth, redirects, HLS manifests, passive-mode
FTP with `REST`) and verifies SHA-256
integrity end-to-end — graceful shrinking on connection-capped servers —
plus deterministic, clock-faked unit tests for every governor decision
(up-probe accept/reject, down-probe efficiency release, server-penalty floor,
limit-window blocking).

```
ferdim/            Python package (engine, queue, daemon, servers, CLI, GUI)
extension/         Firefox MV2 WebExtension (background, popup, options, overlay)
web/               Web UI (vanilla JS/CSS)
scripts/           install/uninstall, desktop entry, systemd unit
packaging/         RPM spec
tests/             unittest suite + local HTTP fixtures
```

## Roadmap

- [x] DASH (MPD) downloads
- [x] Checksum verification (sha256/md5) per job
- [x] ZIP preview in the web UI
- [x] Metalink / mirror blending
- [x] Wayland clipboard deep integration (`wl-paste --watch`)
- [x] Mirror health scoring, fail-over, stall detection (1.4.1)
- [x] Sidecar checksum discovery, SHA-1 / SHA-512 (1.4.1)
- [x] Per-category schedule windows and watched folders (1.4.1)
- [x] Web UI + GTK accessibility pass (1.4.1)
- [x] FTP/FTPS, SOCKS5, SQLite history, COPR/OBS/AUR (1.5)
- [x] D-Bus, GNOME Shell, notifications, archives, host profiles (1.6)
- [x] Coexistence mode, HTTP/3 extra, Merkle verification, Turkish man pages (1.6)
- [x] MV3 extension, speed plan, metrics, congestion awareness, LAN peer cache, publisher tooling, fuzzing, Flatpak (1.7)

What is left (official Fedora package, new locales, MV3) lives in
[ROADMAP.md](ROADMAP.md).

## Contributing

PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Please keep the UI
and code free of third-party product names and trademarks.

## Author

**Enes Aksu** — <eaksu@pm.me> — [@enesaksuu34](https://x.com/enesaksuu34)

## License

MIT © Enes Aksu — see [LICENSE](LICENSE).

---

### Türkçe hızlı başlangıç

FerDim, Fedora için geliştirilen dinamik ve kendini kalibre eden bir indirme
yöneticisidir: azami güç varsayılandır, motor hattınızın ya da karşı sunucunun
hangisi önce doyarsa ona göre bağlantı sayısını sürekli ayarlar. Kurulum:
`bash scripts/install.sh`, servis: `systemctl --user enable --now ferdim`,
arayüz: `ferdim web` (http://127.0.0.1:8378). Ayrıntılar yukarıdadır.
