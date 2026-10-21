Name:           ferdim
Version:        1.0.0
Release:        1%{?dist}
Summary:        Dynamic self-tuning download manager with Firefox integration

License:        MIT
URL:            https://github.com/proxy-turkey/ferdim
BuildArch:      noarch
Requires:       python3 >= 3.9
Recommends:     python3-gobject
Recommends:     firefox
Source0:        https://github.com/proxy-turkey/ferdim/archive/refs/tags/v%{version}.tar.gz

%description
FerDim is a dynamic, self-tuning download manager for Linux:
 * multi-connection segmented downloads with work stealing (up to 32 links)
 * throughput governor that continuously calibrates the connection count to
   saturate either the line or the server, whichever ceiling is reachable
 * crash-safe resume (ETag/Last-Modified validated), speed limiter, scheduler
 * graceful adaptation to server connection limits (HTTP 429/503, resets)
 * Firefox WebExtension + native messaging host that takes over browser
   downloads, detects media (video/audio/HLS) and offers link grabbing
 * Unix-socket RPC daemon, CLI, web UI and GTK4 front-ends, 4 languages

%prep
%setup -q

%build
# yok - saf Python

%install
mkdir -p %{buildroot}%{_libdir}/ferdim
cp -r ferdim %{buildroot}%{_libdir}/ferdim/ferdim
cp -r web %{buildroot}%{_libdir}/ferdim/web
install -D -m 755 bin/ferdim %{buildroot}%{_bindir}/ferdim
install -D -m 755 bin/ferdim-nm %{buildroot}%{_bindir}/ferdim-nm
install -D -m 755 bin/ferdim-gui %{buildroot}%{_bindir}/ferdim-gui
install -D -m 644 scripts/icons/ferdim.svg \
  %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/ferdim.svg
sed 's|@BIN@|%{_bindir}/ferdim-gui|' scripts/ferdim.desktop > \
  %{buildroot}%{_datadir}/applications/ferdim.desktop
sed 's|@BIN@|%{_bindir}/ferdim|' scripts/systemd/ferdim.service > \
  %{buildroot}%{_userunitdir}/ferdim.service
mkdir -p %{buildroot}%{_libdir}/mozilla/native-messaging-hosts
cat > %{buildroot}%{_libdir}/mozilla/native-messaging-hosts/ferdim.json <<'EOF'
{
  "name": "ferdim",
  "description": "FerDim Download Manager integration",
  "path": "/usr/bin/ferdim-nm",
  "type": "stdio",
  "allowed_extensions": ["ferdim@ferdim.app"]
}
EOF
mkdir -p %{buildroot}%{_datadir}/ferdim
if command -v zip >/dev/null; then
  (cd extension && zip -qr %{buildroot}%{_datadir}/ferdim/ferdim-extension.zip .)
fi
mkdir -p %{buildroot}%{_docdir}/%{name}
install -m 644 README.md LICENSE %{buildroot}%{_docdir}/%{name}/

%files
%{_libdir}/ferdim
%{_bindir}/ferdim
%{_bindir}/ferdim-nm
%{_bindir}/ferdim-gui
%{_libdir}/mozilla/native-messaging-hosts/ferdim.json
%{_datadir}/icons/hicolor/scalable/apps/ferdim.svg
%{_datadir}/applications/ferdim.desktop
%{_userunitdir}/ferdim.service
%{_datadir}/ferdim/ferdim-extension.zip
%doc %{_docdir}/%{name}/README.md
%license %{_docdir}/%{name}/LICENSE

%post
%systemd_user_post ferdim.service
if [ -x /usr/bin/gtk-update-icon-cache ]; then
  gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor || :
fi

%changelog
* Wed Oct 21 2026 FerDim <ferdim@ferdim.app> - 1.0.0-1
- Ilk surum: segmentli indirme, resume, HLS, Firefox entegrasyonu, CLI/web/GTK
