# Publishing release artefacts

`ferdim publish` turns a directory of release artefacts into a static
download page that FerDim itself (and any RFC 5854 metalink client) can
consume. It writes the integrity files next to the artefacts, so a plain
`rsync`/`aws s3 sync` of the directory publishes everything.

```bash
ferdim publish dist \
  --base-url https://dl.example.org/releases \
  --mirror https://mirror.example.net/ferdim \
  --mirror https://cdn.example.org/ferdim
```

Nothing needs the daemon: the command is pure local file work, like
`ferdim merkle make`.

## What is written

For every regular file in the directory (`alpha.iso` in the example):

| File | Contents |
|---|---|
| `alpha.iso.ferdim-merkle` | Content-defined (FastCDC) chunk offsets, a SHA-256 per chunk and the Merkle root — the same format a downloader picks up automatically from `<url>.ferdim-merkle` |
| `alpha.iso.sha256` | Coreutils format: `<hex>  alpha.iso\n`, so `sha256sum -c alpha.iso.sha256` verifies the artefact |
| `alpha.iso.meta4` | RFC 5854 metalink for this file: name, size, `sha-256`, the merkle `metaurl` (`application/x-ferdim-merkle`) and `ferdim-merkle-root` hash, plus every download address |
| `index.meta4` | The same entries for the whole directory in one document |

`index.meta4` is what you link from the download page; `ferdim add-file
index.meta4` queues every artefact with its mirrors, checksum and chunk
manifest in one step.

Addresses are ordered by preference: `--base-url` joined with the file
name gets priority 1, then every `--mirror` (priority 2, 3, …). File names
are percent-encoded, so `beta file.zip` becomes `…/beta%20file.zip`.

## Why re-runs are stable

Static publishing usually rides on a synchronisation step that only
uploads changed objects, so the command is byte-stable by construction:

* files are processed in name order; elements and attributes are emitted
  in a fixed order;
* the only timestamp is `<published>`, taken from the artefact mtime in
  RFC 3339 UTC (files in `index.meta4` use the newest mtime);
* a file whose content did not change is not rewritten at all, so even
  its mtime stays put.

Running the same command twice on an unchanged directory produces no
diff, and therefore no uploads.

## Work avoidance and overrides

Chunking is the expensive part (pure Python, roughly 6 MB/s), so an
existing `.ferdim-merkle` is reused when it is still current: the
manifest records the artefact size and its own mtime is not older than the
artefact. Options:

* `--force` — regenerate the manifests even when they look current (use
  when you changed the chunker or want a clean rebuild).
* `--glob '*.iso'` — publish only matching file names; `index.meta4`
  covers exactly the selected set.
* `--no-merkle` — skip the chunk manifests entirely. `.sha256` and
  `.meta4` are still written, minus the `metaurl` and merkle-root hash.

FerDim's own outputs (`.ferdim-merkle`, `.sha256`, `.meta4`, `.part`) are
never published themselves, so a second run does not pick up the files the
first run wrote. Subdirectories are not descended into: flatten the
release directory, or run the command once per subdirectory with its own
`--base-url`.

## Safety on a shared release directory

The directory is treated as untrusted input, because release trees are
often shared or written by another account:

* every output is written through a temporary file created with `O_EXCL`
  in the *same* directory (`mkstemp`), fsynced, chmod `0644` and then
  renamed onto the final name. The temporary name is unpredictable and
  the rename replaces a symlink instead of writing through it, so a
  planted `alpha.iso.sha256 -> ~/.bashrc` link (or the old predictable
  `.part` names) can never clobber a file outside the release set;
* symlinked artefacts are skipped — `notes -> ~/.ssh/id_ed25519` is not
  hashed, listed, or given sidecars. Only regular files are published;
* file names containing control characters (newline, tab, escape, DEL …)
  are skipped with a warning on stderr: they cannot be represented in
  valid XML and would inject a line into the coreutils `.sha256` format;
* content that did not change is not rewritten, so the symlink handling
  never churns an already-correct release tree.


## Verifying a published tree

```bash
# sidecars match the artefacts
cd dist && sha256sum -c *.sha256

# the metalink documents parse and resolve (no daemon needed for parsing)
python3 -c "from ferdim import metalink; print(metalink.parse(open('dist/index.meta4').read()))"

# chunk manifests match their artefacts
for f in dist/*.ferdim-merkle; do
  ferdim merkle verify "dist/$(basename "$f" .ferdim-merkle)"
done
```

## Notes and limits

* `--base-url` and every `--mirror` must be `http(s)`; anything else is
  rejected before a single file is written, so a produced `.meta4` is
  always readable by FerDim's own metalink parser.
* The merkle root is a *root over chunk hashes*, not a plain file digest:
  it is what makes `ferdim merkle update` able to fetch only the chunks
  that changed between two releases.
* Raw `.tar`/`.zip` members are not individually published; publish the
  artefacts you want downloadable.
