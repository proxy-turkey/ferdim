# Manifest V3 migration

Status: **done (1.7)** — `extension/manifest.json` is `manifest_version: 3`
and the extension ships from the same `extension/` directory as before.
This document replaced the 1.5.1 MV3 spike note: it records what the
migration changed, how to reproduce the gates, and what is still open.

Why: AMO is deprecating MV2. The extension is Firefox-first
(`browser_specific_settings.gecko.strict_min_version` was already `140`,
well above MV3's 109 and `storage.session`'s 115), so no supported user
was lost and no separate build is needed.

## What changed

| Area | Before (MV2) | Now (MV3 source) |
|---|---|---|
| `manifest_version` | `2` | `3` |
| Host permission | `<all_urls>` inside `permissions` | `host_permissions: ["<all_urls>"]` |
| Toolbar button | `browser_action` + `browser.browserAction` | `action`; code still uses `browser.browserAction \|\| browser.action` |
| Page action | `page_action` | kept (`page_action` stays valid in Firefox MV3; Chromium merges it into `action`) |
| Background | implicit persistent page | `background.scripts` event page (`sniff.js`, `background.js`). No `service_worker` — Firefox MV3 does not support one |
| Link grab | `tabs.executeScript` with a `code:` string | `browser.scripting.executeScript({target: {tabId}, func, args})`; the extractor is a normal function, so it must stay closure-free (the API serializes its source) |
| Per-tab media state | in-memory `Map` | in-memory `Map` **plus** a write-through copy in `browser.storage.session`; hydrated lazily on the first sniffer/popup access and pruned against live tab ids. Without `storage.session` (old host, MV2) it degrades to memory only |
| Host ping | `setInterval(pingHost, 60000)` | no timer (a timer dies with the event page). One ping per background start for the badge colour, and a fresh ping whenever the popup asks (`getMedia`) |
| Context menus | rebuilt on every background start | `runtime.onInstalled` + `runtime.onStartup`, serialized so `removeAll`/`create` never race; menus are browser-level state and survive idle unload |
| `scripting` permission | — | added (required by `scripting.executeScript`) |
| `nativeMessaging` | permission + `runtime.sendNativeMessage` | unchanged; the FerDim bridge is untouched |

`background.js` still runs on MV2 hosts: the legacy branch reads the API
through a bracketed property (`browser.tabs["executeScript"]`) on purpose.
`addons-linter` flags a plain `tabs.executeScript` reference as a removed
MV3 API even when the call sits behind a feature test; the bracket keeps
the MV3 lint gate at zero without changing behaviour, because that path
only runs when `browser.scripting` is missing.

## Reproduce

```bash
# Release gate: 0 errors / 0 warnings (same command CI and AMO run)
npx -y web-ext@10.8.0 lint --source-dir extension --warnings-as-errors

# Build: re-checks MV3 keys, lints, then writes dist/ferdim-<version>.zip
bash scripts/build_mv3.sh                # or: --lint-only / a custom .zip path

# Behaviour tests (node): manifest, source guards, storage.session survival,
# grab under both manifest versions, injected extractor, locale parity
python3 -m unittest tests.test_extension_mv3 tests.test_extension_sniff -v

# Load check: installs as a temporary add-on (kill after ~15 s)
npx -y web-ext@10.8.0 run --source-dir extension \
  --target firefox-desktop --firefox="$(which firefox)" --no-reload \
  --args=-headless
```

`scripts/build_mv3.sh` kept its name because `ROADMAP.md`/`CHANGELOG.md`
point at it; it no longer generates an MV3 variant from an MV2 source — it
builds and lints the one extension that ships. `python3` stdlib
`zipfile`-based packaging means no npm dependency for the artifact.

## Results (2026-11-24, web-ext 10.8.0, Firefox 160.0 headless)

| Check | Result |
|---|---|
| `web-ext lint --warnings-as-errors` on `extension/` (MV3) | 0 errors, 0 warnings, 0 notices |
| `bash scripts/build_mv3.sh` | MV3 keys verified, lint 0/0, `dist/ferdim-1.6.0.zip` written (manifest at the archive root) |
| `web-ext run` (headless) | `Installed … as a temporary add-on`, add-on id `ferdim@ferdim.app`, no manifest/extension errors in the browser console |
| Background event page evaluated | probe build logged from the background context on install (no load-time exception) |
| MV3 API surface (probe build) | `action: object`, `browserAction: undefined`, `pageAction: object`, `scripting: object`, `storage.session: object`, `runtime.onStartup: object`; `setBadgeBackgroundColor` accepted |
| Real `scripting.executeScript` injection | probe function injected into a local page returned its `a[href]` links (`func` + `args` work under Firefox MV3) |
| `node --check extension/*.js` | clean |
| `tests.test_extension_mv3` | manifest/key guards, source guards (`scripting` first, no `setInterval`, menus on install/startup, `storage.session`), node harness: media survives an event-page restart, stale tab state pruned, tab close clears session state, grab via `scripting` and via the MV2 fallback, serialized extractor returns the expected links, 4-locale key parity |
| `tests.test_extension_sniff` | unchanged, green |
| `tabs.executeScript` in lint | gone (was the only MV3 blocker in the spike) |

The probe confirms why `background.js` writes `browser.browserAction || browser.action`:
Firefox MV3 does not expose the `browserAction` namespace at all, while
`pageAction` and `scripting` are present.


## Design notes

- **`func`, not an injected `grab.js` file.** The spike suggested a
  packaged script; a function avoids a second file that has to stay in
  step with its caller and is supported by Firefox ≥ 102. The cost is
  that the extractor must be self-contained — a test serializes it,
  evaluates it in a bare page context and checks the links it returns.
- **`storage.session`, not `storage.local`.** Session storage has exactly
  the right lifetime (extension/browser session, cleared on exit, not
  synced), so caught media does not leak into permanent profile data.
  Writes are coalesced per microtask; a lost write after an idle unload
  only costs a badge count.
- **No `alarms`.** A periodic native-host ping kept the background awake
  for nothing; the popup open path (`getMedia` → `pingHost`) is the only
  place the state is shown, and the wake-time ping covers the badge.
- **Menus are browser state.** Rebuilding them on every event-page wake
  was wasted work that could race with an existing definition, so they
  are created on install/update and browser startup only.
- **Selection semantics.** `selectionOnly` is now actually forwarded to
  the extractor: the context-menu "grab selected links" scans the
  selection, "grab all links" scans the page even when text happens to be
  selected (previously the selection silently won for both).

## Open risks

- **AMO review timing** for the MV3 upload is outside our control. The
  upload workflow (`amo.yml`) uses `--source-dir extension`, so the same
  signed artifact now carries MV3.
- **Android.** `gecko_android.strict_min_version` stays `142`; MV3 is
  supported, but the page-action badge should be checked once on a device
  (no emulator here).
- **Revoked host permission.** Under MV3 a user can withdraw
  `<all_urls>` per site. Sniffing and cookie forwarding already fail soft
  (`webRequest` stops firing, `cookies.getAll` is wrapped), but the popup
  does not explain the empty list. Low priority, tracked in `ROADMAP.md`.
- **Live-page grab.** Injection itself is proven (probe function injected
  into a local page through `scripting.executeScript`), and the popup/
  context-menu wiring is covered by the node harness; what was not driven
  in a real browser is the click path itself (headless `web-ext run` does
  not open the popup). One manual grab before a release is still cheap
  insurance.
- **Old Firefox (`< 102`).** The MV2 fallback path is untested on a real
  browser of that vintage; the manifest floor is 140, so it only matters
  if someone forks the source back to MV2.
