%global srcname ferdim

Name:           %{srcname}
Version:        1.3.3
Release:        1%{?dist}
Summary:        Dynamic self-tuning download manager with Firefox integration

License:        MIT
URL:            https://github.com/proxy-turkey/ferdim
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  zip
BuildRequires:  systemd-rpm-macros
Requires:       python3 >= 3.9
Recommends:     python3-gobject
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
cp -a ferdim web %{buildroot}%{_datadir}/%{srcname}/
find %{buildroot}%{_datadir}/%{srcname} -name '__pycache__' -type d -exec rm -rf {} +

# executables
install -D -m 0755 bin/ferdim     %{buildroot}%{_bindir}/ferdim
install -D -m 0755 bin/ferdim-nm  %{buildroot}%{_bindir}/ferdim-nm
install -D -m 0755 bin/ferdim-gui %{buildroot}%{_bindir}/ferdim-gui

# desktop entry, icon, manual page
mkdir -p %{buildroot}%{_datadir}/applications
sed 's|@BIN@|%{_bindir}/ferdim-gui|' scripts/ferdim.desktop > \
  %{buildroot}%{_datadir}/applications/%{srcname}.desktop
install -D -m 0644 scripts/icons/ferdim.svg \
  %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{srcname}.svg
install -D -m 0644 docs/%{srcname}.1 %{buildroot}%{_mandir}/man1/%{srcname}.1
install -D -m 0644 docs/%{srcname}-nm.1 %{buildroot}%{_mandir}/man1/%{srcname}-nm.1
install -D -m 0644 docs/%{srcname}-gui.1 %{buildroot}%{_mandir}/man1/%{srcname}-gui.1

# systemd user unit
mkdir -p %{buildroot}%{_userunitdir}
sed 's|@BIN@|%{_bindir}/ferdim|' scripts/systemd/ferdim.service > \
  %{buildroot}%{_userunitdir}/%{srcname}.service

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
%{_datadir}/%{srcname}/
%{_datadir}/applications/%{srcname}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{srcname}.svg
%{_datadir}/mozilla/native-messaging-hosts/%{srcname}.json
%{_userunitdir}/%{srcname}.service
%{_mandir}/man1/%{srcname}.1*
%{_mandir}/man1/%{srcname}-nm.1*
%{_mandir}/man1/%{srcname}-gui.1*

# systemd ships file triggers that reload user units installed under
# the user unit dir, so explicit scriptlets are unnecessary.

%changelog
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
