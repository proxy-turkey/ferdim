# Manifest V3 migration spike

Status: **spike only** — 1.5.1 still ships Manifest V2. This document
records what breaks when the extension moves to MV3 in Firefox, what the
spike build already proves, and the concrete migration steps. It is not
a promise of dates.

Why now: AMO is deprecating MV2, and the extension is Firefox-first
(`browser_specific_settings.gecko.strict_min_version` is already 140, so
no user is lost by requiring MV3 — MV3 has been available since
Firefox 109).

## Reproduce

```bash
# MV2 baseline (must stay 0/0, this is the release gate)
npx -y web-ext@10.8.0 lint --source-dir extension --warnings-as-errors

# MV3 variant: copied to a temp dir, manifest transformed, then linted
bash scripts/build_mv3.sh            # prints the output path
bash scripts/build_mv3.sh /tmp/mv3   # fixed directory (files are copied over)
```

`scripts/build_mv3.sh` copies `extension/` unchanged, rewrites only the
generated `manifest.json` with `python3` stdlib + `json` (the shipped
manifest is never touched), and runs `web-ext@10.8.0 lint` on the result.
It does not pass `--warnings-as-errors`: the known MV3 warning is the
finding, not a build failure.

Results on this branch (2026-11-24, web-ext 10.8.0, Firefox target 140):

| Build | errors | warnings | notices |
|---|---|---|---|
| MV2 `extension/` | 0 | 0 | 0 |
| MV3 generated variant | 0 | 1 | 0 |

The single MV3 warning:

```
UNSUPPORTED_API  "tabs.executeScript" has been removed in Manifest Version 3
                 file: background.js  line: 245
```

That is the link-grab feature (`grabTab`). Everything else in the
manifest transforms cleanly.

## What breaks under Firefox MV3

| Area | Today (MV2) | Firefox MV3 | Impact / action |
|---|---|---|---|
| `manifest_version` | `2` | `3` | Build script flips it; one-key change. |
| Background lifetime | implicit persistent page | non-persistent **event page**; `background.persistent: true` is an error | `mediaByTab`, `grabsByTab`, `opts`, `hostOk/hostPort/hostTurbo` live in memory and vanish on idle unload; `setInterval(pingHost, 60000)` dies with the page. Move timers to `alarms` and per-tab media to `storage.session`. |
| Background scripts vs service worker | `background.scripts` | Firefox MV3 supports `scripts` (event pages); `background.service_worker` is **not** supported in Firefox (bug 1573659) | None for us: keep `background.scripts`; do not add a service worker. `sniff.js` stays first in the array. |
| Toolbar button | `browser_action` + `browser.browserAction` | `action` + `browser.action` | Key is renamed by the transform; code already uses `toolbar = browser.browserAction \|\| browser.action`. |
| Page action | `page_action` + `browser.pageAction` | Firefox **retains** `page_action` in MV3 (Chromium merges it into `action`) | Keep the key; code uses `pageButton = browser.pageAction \|\| <no-op shim>` so a Chromium-style merge would not throw. |
| Host permissions | `<all_urls>` inside `permissions` | `host_permissions`; users may grant/revoke per site | Split done by the transform. Runtime must tolerate a revoked grant for webRequest/cookies/content scripts (`permissions.contains`, `permissions.onRemoved`). |
| `webRequest` | `webRequest`, `onHeadersReceived` with `["responseHeaders"]` | Firefox keeps `webRequest`; `webRequestBlocking` also remains available in Firefox (with host permissions) | No change: this extension only observes, it never passes `"blocking"`. Do not assume blocking exists on Chromium if that ever matters. |
| `tabs.executeScript` + `code:` strings | `grabTab()` injects a string | API removed; `scripting` API takes a file/function and has no `code:` parameter | **The one real break.** Add `scripting` permission and move the extractor into a packaged script (`grab.js` injected with `scripting.executeScript({target, files})`), or message the already-declared `overlay.js` content script. |
| Context menus | `refreshMenus()` runs at every background start | Event pages must register menus in `runtime.onInstalled`; the `onclick` property is forbidden | We already use `onClicked`. Move creation to `onInstalled` (plus keep the options-change refresh) so menus are not rebuilt/race on each wakeup. |
| CSP | default extension CSP | `content_security_policy.extension_pages`, stricter; no remote code; content scripts share it | None: no remote code, no `eval`, no inline `<script>` in our pages. |
| `web_accessible_resources` | absent | must list matches/resources | None (no packaged resource is exposed). |
| `nativeMessaging` | permission + `runtime.sendNativeMessage` | unchanged in MV3 | None; the FerDim bridge keeps working. |
| Other permissions (`downloads`, `cookies`, `contextMenus`, `storage`, `tabs`, `notifications`) | — | unchanged keywords | None. |
| Version string | `1.5.1` | numeric, max 4 parts | Conforms. |
| Android | `gecko_android.strict_min_version: 142` | MV3 supported | Verify badge/page-action behaviour on Android during the switch. |

## Findings table

| Check | Result | Evidence |
|---|---|---|
| MV3 manifest accepted by addons-linter | **pass** | 0 errors on the generated variant |
| `action` accepts the existing title/icon/popup | **pass** | no lint diagnostics |
| `page_action` kept in Firefox MV3 | **pass** | accepted by linter; MDN migration guide (Firefox retains it) |
| Event page with `background.scripts` | **pass** | accepted; `sniff.js` + `background.js` order preserved |
| Blocking webRequest not required | **pass** | listener options are `["responseHeaders"]` only |
| Native messaging unaffected | **pass** | permission not flagged, no API warning |
| Link grab (`tabs.executeScript`) | **fail** | `UNSUPPORTED_API` warning, `background.js:245` |
| Timers/state under event-page unload | **not migrated** | code still uses `setInterval` + in-memory `Map`s |
| Runtime load of the MV3 variant in Firefox | **not run** | lint-only spike; see "Open risks" |

## Migration plan

1. **Manifest switch (proven).** Adopt what the spike build produces:
   `manifest_version: 3`, `host_permissions: ["<all_urls>"]`,
   `browser_action` → `action`, keep `page_action`, keep
   `background.scripts`. Minimum Firefox stays 140.
2. **Event-page proofing.** Persist per-tab media in `storage.session`
   (keyed by tab id) and restore it lazily in the `getMedia` handler;
   replace `setInterval(pingHost, …)` with `alarms.create("ferdim-ping",
   {periodInMinutes: 1})` + `alarms.onAlarm` (add the `alarms`
   permission); re-ping on popup open (already covered by the
   `pingHost` message). Badge/page-action state must be recomputed on
   wakeup because the in-memory map is gone.
3. **Link grab.** Move the extractor out of the code string into
   `extension/grab.js` and call
   `browser.scripting.executeScript({target: {tabId}, files: ["grab.js"]})`;
   add the `scripting` permission. Keep the declarative `overlay.js`
   content script as is. This closes the only lint warning.
4. **Menus.** Register `menuDefs()` in `runtime.onInstalled` (and on
   options change); drop the unconditional rebuild from the startup path.
5. **Permission resilience.** If `<all_urls>` is revoked at runtime,
   sniffing and cookie forwarding must fail soft, and the popup should
   say so (`permissions.onRemoved` → clear per-tab state).
6. **Release.** Ship MV2 until steps 2–3 land, then tag an MV3 release;
   run the full gate set (tests, web-ext lint 0/0 on the MV3 source,
   `tests/cli_smoke.sh`) before uploading to AMO.

## Open risks

- **Event-page state.** A user who leaves a tab idle loses the caught
  media list until the player requests something again. Acceptable, but
  decide explicitly (reset + badge recompute vs `storage.session`).
- **`webRequestBlocking` parity.** Not needed today. If a future feature
  wants to cancel a request before `downloads.onCreated`, verify the
  permission on the target Firefox version instead of assuming Chromium
  behaviour.
- **Runtime validation.** The variant was lint-validated, not loaded.
  Before the migration lands, run
  `web-ext run --source-dir /tmp/mv3` and exercise: browser download
  takeover, HLS manifest detection + badge count, popup download, turbo
  toggle, grab, context menus, native host reconnect after an idle
  unload.
- **AMO review timing** for the MV3 upload is outside our control; the
  manifest switch is small, the code changes in steps 2–3 are the real
  review surface.

## Files in this spike

- `extension/sniff.js` — generic response-header classifier (separate
  1.6 item; loaded before `background.js`).
- `scripts/build_mv3.sh` — generate + lint the MV3 variant.
- `docs/mv3-spike.md` — this document.
- `tests/test_extension_sniff.py` — node-driven tests for the classifier.
