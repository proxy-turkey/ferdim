%global srcname ferdim

Name:           %{srcname}
Version:        1.8.1
Release:        1%{?dist}
Summary:        Dynamic self-tuning download manager with Firefox integration

License:        MIT
URL:            https://github.com/proxy-turkey/ferdim
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  zip
BuildRequires:  systemd-rpm-macros
BuildRequires:  desktop-file-utils
%if 0%{?suse_version}
# OBS: ikon dizinlerinin sahibi derleme kokunde olmali
BuildRequires:  hicolor-icon-theme
# python3-base sqlite3 icermez (gecmis veritabani)
BuildRequires:  python3
%endif
Requires:       python3 >= 3.9
Requires:       hicolor-icon-theme
%if 0%{?suse_version}
# openSUSE: /usr/share/mozilla dizini bu pakete ait (mozilla-filesystem yok)
%else
Requires:       mozilla-filesystem
# /usr/share/dbus-1/{services,interfaces} sahibi
Requires:       dbus-common
%endif
Recommends:     python3-gobject
# optional: encrypted HLS (AES-128); imported lazily, see ferdim/engine/segments.py
Recommends:     python3-cryptography
# optional: HTTP/3 (QUIC) transport, see ferdim/engine/h3.py
Suggests:       python3-aioquic
Recommends:     firefox
Supplements:    (firefox and python3-gobject)

%description
FerDim is a dynamic, self-tuning download manager for Linux:
* multi-connection segmented downloads with work stealing (up to 32 links)
* a throughput governor that continuously calibrates the connection count
  to saturate either the line or the server, whichever ceiling is reachable
* crash-safe resume (ETag/Last-Modified validated), speed limiter, scheduler
* graceful adaptation to server connection limits (HTTP 429/503, resets)
* a Firefox WebExtension plus native messaging host that takes over browser
  downloads, detects media (video/audio/HLS) and offers link grabbing
* Unix-socket RPC daemon, CLI, web UI and GTK4 front-ends in four languages.

%prep
%autosetup -n %{srcname}-%{version}

%build
# pure Python; nothing to compile

%install
# application payload (arch-independent)
mkdir -p %{buildroot}%{_datadir}/%{srcname}
cp -a ferdim web gnome-shell-extension %{buildroot}%{_datadir}/%{srcname}/
find %{buildroot}%{_datadir}/%{srcname} -name '__pycache__' -type d -exec rm -rf {} +

# executables
install -D -m 0755 bin/ferdim     %{buildroot}%{_bindir}/ferdim
install -D -m 0755 bin/ferdim-nm  %{buildroot}%{_bindir}/ferdim-nm
install -D -m 0755 bin/ferdim-gui %{buildroot}%{_bindir}/ferdim-gui
install -D -m 0755 bin/ferdim-dbus %{buildroot}%{_bindir}/ferdim-dbus

# desktop entry, icon, manual page
mkdir -p %{buildroot}%{_datadir}/applications
sed 's|@BIN@|%{_bindir}/ferdim-gui|' scripts/ferdim.desktop > \
  %{buildroot}%{_datadir}/applications/%{srcname}.desktop
desktop-file-validate %{buildroot}%{_datadir}/applications/%{srcname}.desktop
install -D -m 0644 scripts/icons/ferdim.svg \
  %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{srcname}.svg
install -D -m 0644 docs/%{srcname}.1 %{buildroot}%{_mandir}/man1/%{srcname}.1
install -D -m 0644 docs/%{srcname}-nm.1 %{buildroot}%{_mandir}/man1/%{srcname}-nm.1
install -D -m 0644 docs/%{srcname}-gui.1 %{buildroot}%{_mandir}/man1/%{srcname}-gui.1
install -D -m 0644 docs/%{srcname}-dbus.1 %{buildroot}%{_mandir}/man1/%{srcname}-dbus.1
for m in %{srcname} %{srcname}-nm %{srcname}-gui %{srcname}-dbus; do
  install -D -m 0644 docs/tr/$m.1 %{buildroot}%{_mandir}/tr/man1/$m.1
done

# systemd user unit
mkdir -p %{buildroot}%{_userunitdir}
sed 's|@BIN@|%{_bindir}/ferdim|' scripts/systemd/ferdim.service > \
  %{buildroot}%{_userunitdir}/%{srcname}.service

# D-Bus: arayuz XML'i ve oturum veriyolu etkinlestirme dosyasi
install -D -m 0644 data/org.ferdim.Daemon1.xml \
  %{buildroot}%{_datadir}/dbus-1/interfaces/org.ferdim.Daemon1.xml
mkdir -p %{buildroot}%{_datadir}/dbus-1/services
sed 's|@BIN@|%{_bindir}/ferdim-dbus|' data/org.ferdim.Daemon.service > \
  %{buildroot}%{_datadir}/dbus-1/services/org.ferdim.Daemon.service

# Firefox native messaging registration (system-wide; /usr/share/mozilla is
# a documented manifest location and keeps the package arch-independent)
mkdir -p %{buildroot}%{_datadir}/mozilla/native-messaging-hosts
cat > %{buildroot}%{_datadir}/mozilla/native-messaging-hosts/%{srcname}.json <<EOF
{
  "name": "%{srcname}",
  "description": "FerDim Download Manager integration",
  "path": "%{_bindir}/ferdim-nm",
  "type": "stdio",
  "allowed_extensions": ["ferdim@ferdim.app"]
}
EOF

# unsigned WebExtension bundle (users sign once via AMO; see README)
mkdir -p %{buildroot}%{_datadir}/%{srcname}
(cd extension && zip -qr %{buildroot}%{_datadir}/%{srcname}/%{srcname}-extension.zip .)

%check
python3 -m unittest discover -s tests -v

%files
%license LICENSE
%doc README.md CHANGELOG.md
%{_bindir}/ferdim
%{_bindir}/ferdim-nm
%{_bindir}/ferdim-gui
%{_bindir}/ferdim-dbus
%{_datadir}/%{srcname}/
%{_datadir}/applications/%{srcname}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{srcname}.svg
%if 0%{?suse_version}
%dir %{_datadir}/dbus-1
%dir %{_datadir}/dbus-1/interfaces
%dir %{_datadir}/dbus-1/services
%endif
%{_datadir}/dbus-1/interfaces/org.%{srcname}.Daemon1.xml
%{_datadir}/dbus-1/services/org.%{srcname}.Daemon.service
%if 0%{?suse_version}
%dir %{_datadir}/mozilla
%endif
%dir %{_datadir}/mozilla/native-messaging-hosts
%{_datadir}/mozilla/native-messaging-hosts/%{srcname}.json
%{_userunitdir}/%{srcname}.service
%{_mandir}/man1/%{srcname}.1*
%{_mandir}/man1/%{srcname}-nm.1*
%{_mandir}/man1/%{srcname}-gui.1*
%{_mandir}/man1/%{srcname}-dbus.1*
%lang(tr) %{_mandir}/tr/man1/%{srcname}*.1*

# systemd USER unit: presets are applied by the user-unit scriptlets
# (Packaging Guidelines, Scriptlets: User Units); file triggers do not
# cover user units and the macros need the unit name as argument.
# The trailing comment keeps the macro's final newline inside the
# scriptlet: rpm 6 trims it otherwise, which trips fedora-review's
# verbatim macro-expansion check (CheckSystemdUserunitdirScriplets).
%post
%systemd_user_post %{srcname}.service
# end of user-unit post scriptlet

%preun
%systemd_user_preun %{srcname}.service
# end of user-unit preun scriptlet

%changelog
* Wed Nov 25 2026 Enes Aksu <eaksu@pm.me> - 1.8.1-1
- tests: pause before the transfer ends (fast builders)
* Wed Nov 25 2026 Enes Aksu <eaksu@pm.me> - 1.8.0-1
- data integrity: no resume without validator, fsync, atomic names
- corrupt queue recovery, state schemas, single-instance lock
* Tue Nov 24 2026 Enes Aksu <eaksu@pm.me> - 1.7.2-1
- tests: wait for the history row (slow builders)
* Tue Nov 24 2026 Enes Aksu <eaksu@pm.me> - 1.7.1-1
- security: no API token in the web page; one-time launcher links
- security: confine HTTP download folders; scope credentials to origin
- security: web server answers only the daemon's user; token rotated once
* Tue Nov 24 2026 Enes Aksu <eaksu@pm.me> - 1.7.0-1
- Manifest V3 extension; host profiles and integrity badges in web and GTK
- HTTP/3 connection reuse; latency-aware coexistence; speed plan; metrics
- TCP_INFO congestion awareness; opt-in LAN peer cache; ferdim publish
- security: Host/Origin checks on the local web server, scoped cookies
* Tue Nov 24 2026 Enes Aksu <eaksu@pm.me> - 1.6.0-1
- learned per-host connection profiles; coexistence mode (opt-in)
- archive browsing: nested ZIP / tar, selective extraction, repair
- desktop notifications; ferdim-dbus session-bus bridge; GNOME Shell extension
- content-defined chunking with Merkle verification and range-level repair
- optional HTTP/3 transport (python3-aioquic); Turkish man pages
* Tue Nov 24 2026 Enes Aksu <eaksu@pm.me> - 1.5.1-1
- SOCKS5/SOCKS5h proxy support in the standard library (HTTP, HTTPS, FTP)
- download history in SQLite: search, re-download, per-host statistics
- fix: pause is synchronous across the job start window; HLS/DASH pause
- fix: --insecure was ignored for HTTPS; descriptor leak on pause/resume
- packaging: openSUSE conditionals; COPR, OBS and AMO publish on tag
* Tue Nov 24 2026 Enes Aksu <eaksu@pm.me> - 1.5.0-1
- FTP/FTPS downloads via the standard library (resume, retries, MDTM check)
- stable API error codes; web UI and CLI translate engine errors
- mirrors are probed in the background before workers rely on them
- web UI speed graph and connection timeline
- GTK: modal add window and a settings window; Gtk.Dialog removed
- state files are created private (0600); passwords redacted in the API
* Mon Nov 23 2026 Enes Aksu <eaksu@pm.me> - 1.4.2-1
- fix: job commands over the local socket lost their job id (pause, resume,
- restart, cancel, remove never worked from the CLI or the GTK app)
- fix: pause returns once the job is paused; in-flight ranges are checkpointed
- fix: workers of a paused generation exit quietly after a resume
* Mon Nov 23 2026 Enes Aksu <eaksu@pm.me> - 1.4.1-1
- source health model for mirrors: throughput-weighted balancing, fail-over,
- stall detection, Content-Range validation, jittered exponential backoff
- SHA-1/SHA-512 and sidecar checksum auto-discovery, verified before "done"
- per-category schedule windows, watched folders, add-file / add @list.txt
- web UI and GTK accessibility pass, consistent 1024-based size units
- packaging: desktop-file-validate, systemd user-unit scriptlets, owned
  native-messaging-hosts directory (review findings)
* Wed Oct 21 2026 Enes Aksu <eaksu@pm.me> - 1.4.0-1
- DASH (MPD) downloads, multi-mirror blending, metalink/.meta4 expansion,
- per-job SHA-256/MD5 verification, ZIP preview, size-probing smart queue,
- event-driven Wayland clipboard watching, GTK via python -m ferdim.gui,
- WebExtension lint hardening

* Wed Oct 21 2026 Enes Aksu <eaksu@pm.me> - 1.3.3-1

- Maintainer metadata: named copyright holder, README author section,
  WebExtension author field, man page author section

* Wed Oct 21 2026 Enes Aksu <eaksu@pm.me> - 1.3.2-1
- Packaging polish: /usr/share/mozilla manifest, no user-unit scriptlets,
  man pages for all three executables

* Wed Oct 21 2026 The FerDim Authors <ferdim@ferdim.app> - 1.3.1-1
- Review-grade packaging: %%check test run, man page, systemd user scriptlets,
  centralized native-messaging registration, arch-independent payload path

* Wed Oct 21 2026 The FerDim Authors <ferdim@ferdim.app> - 1.3.0-1
- Throughput governor, adaptive server-limit handling, dynamic range sizing,
  end-game donation; maximum power defaults; four UI languages

* Wed Oct 21 2026 The FerDim Authors <ferdim@ferdim.app> - 1.0.0-1
- Initial package: segmented engine, resume, HLS, Firefox integration,
  CLI/web/GTK front-ends
