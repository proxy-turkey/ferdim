# Contributing to FerDim

Thanks for your interest! FerDim is a small, dependency-light project —
please help keep it that way.

## Development setup

```bash
git clone https://github.com/proxy-turkey/ferdim.git
cd ferdim
python3 -m unittest discover -s tests -v
```

Requirements: Python 3.9+ (engine/CLI/daemon use only the standard library).
The GTK front-end additionally needs `python3-gobject`. The web UI is vanilla
JS/CSS; the extension is plain MV2 WebExtension JS.

## Ground rules

1. **Engine stays stdlib-only.** `ferdim/` must run on a bare Python install.
2. **No third-party trademarks.** Do not reference other products (or their
   module names) in code, comments, UI strings, docs, or tests. Describe
   behavior in neutral terms.
3. **Tests required** for engine changes. The suite runs local HTTP fixtures —
   no network, no flaky external servers. Keep new tests deterministic.
4. **All UI strings in four languages.** If you add a user-facing string,
   add it to `ferdim/i18n.py` (all four locales) and, for the extension, to
   `_locales/{en,tr,es,ru}/messages.json`. CI parity tests will catch misses.
5. **Match the existing style**: Turkish-free code identifiers, terse Turkish
   comments are fine historically but prefer English for new code comments;
   4-space indent; no formatters/linters mandated.

## Adding a language

1. Add the code to `LANGS` and a full entry set to `S` in `ferdim/i18n.py`
   (the parity test enforces completeness).
2. Add `extension/_locales/<lang>/messages.json` (mirror `en`).
3. Add the language to the web UI settings `<select>` and README badge.

## Packaging notes

- RPM spec lives in `packaging/ferdim.spec` (noarch).
- `scripts/install.sh` handles user-level installs (used by most testers).
- COPR builds: see `docs/packaging.md`.

## Submitting

1. Fork, create a feature branch.
2. Add tests; make sure `python3 -m unittest discover -s tests` passes.
3. Open a PR describing *what* and *why*; screenshots for UI changes please.
