# Benchmarks

`benchmarks/` is the executable performance gate for ROADMAP 1.13. It
measures the engine, the content-defined chunker, queue persistence and
`stats()`, and the idle cost of the daemon — then compares the result
with a stored baseline. A metric that regresses by **more than 20 %**
makes the run exit non-zero, which is what the CI `bench` job enforces.

Everything is Python stdlib only; the only network traffic is loopback
against a server the benchmark starts itself.

## Running

```bash
python3 -m benchmarks                     # full run + baseline comparison
python3 -m benchmarks --quick             # small workloads, no comparison
python3 -m benchmarks --only queue        # one suite (engine, chunker, queue, idle)
python3 -m benchmarks --json bench.json   # machine-readable results + verdicts
python3 -m benchmarks --update-baseline   # rewrite benchmarks/baseline.json
```

A full run takes about a minute on a laptop-class machine; `--quick`
takes a few seconds and is meant for local iteration, not for gate
decisions (it therefore skips the comparison). `--only` runs a subset
of the suites and never fails on metrics it did not measure.

Temporary roots (`/tmp/ferdim-bench-*`), the local HTTP server and the
benchmark daemon are removed/terminated in `finally` blocks: a run
leaves no directories, sockets or processes behind.

## What is measured

| Metric | How | Reported as |
|---|---|---|
| `engine_mib_s` | 256 MiB sparse file served over loopback with HTTP Range support by a server in a **separate process** (so it cannot share the GIL with the client), downloaded by a real `DownloadJob` with 8 segments in a temporary `AppEnv(root=...)`; the clock stops when the last byte reaches the `.part` file, so the final fsync/rename does not mix storage speed into the metric | MiB/s, best of 3 |
| `cdc_mib_s` | `ferdim.cdc.make_manifest` (FastCDC + SHA-256) over 64 MiB of deterministic pseudo-random bytes | MiB/s, best of 3 |
| `queue_save_jobs_s` | 10 000 paused jobs in a `QueueManager`; one `qm.add()` plus the wait until `queue.json` on disk contains the new job (works for synchronous or debounced persistence) | jobs/s, best of 5 |
| `queue_load_jobs_s` | a new `QueueManager` on the same state directory loads those jobs | jobs/s, best of 5 |
| `stats_calls_s` | `qm.stats()` over the 10 000-job queue | calls/s, best of 9 |
| `idle_cpu_pct`, `idle_rss_mib` | `python3 -m ferdim.daemon` in a throwaway XDG root, empty queue, sampled from `/proc/<pid>` for 10 s | % of one core, MiB |
| `idle_loaded_cpu_pct`, `idle_loaded_rss_mib` | the same daemon started on a queue of 10 000 paused jobs | % of one core, MiB |

Paused jobs are used on purpose: the size prober only touches `queued`
jobs and the scheduler only starts `queued` jobs, so no network probe or
download runs during the persistence/`stats()` benchmarks.
`--quick` shrinks the payloads (64 MiB engine, 16 MiB chunker) and the
job count (2 000).

## Baselines and machine normalisation

`benchmarks/baseline.json` stores one raw value per metric plus the
calibration score of the machine that produced it. The calibration is a
fixed workload (4 MiB SHA-256 + a 200 000-step pure-Python loop); its
best-of-5 score stands in for "how fast is this machine at this kind of
work".

Rates are compared after scaling the stored baseline by
`current_calibration / baseline_calibration`, so a faster CI runner is
not reported as a faster FerDim and a genuine slowdown stays visible.
Absolute metrics are not scaled: RSS must stay within +20 %, and idle CPU
fails only above `max(baseline * 1.2, 2.0 % of a core)` so tick-level
noise on an idle daemon cannot fail a build.

RSS and CPU are also sensitive to the interpreter build, so they are
compared only when the baseline and the current run share the same
platform and Python minor version; otherwise they are reported as
`skip` and the rate metrics still gate the run.

## Regenerating the baseline

After merging a change that legitimately moves performance (and after
merging the other 1.13 performance lanes), regenerate the stored
baseline on the machine that should define it and commit the file:

```bash
python3 -m benchmarks --update-baseline   # full run only
git add benchmarks/baseline.json && git commit -m "bench: refresh baseline"
```

Never regenerate the baseline to silence a regression — investigate the
metric first. Keep the run on an otherwise idle machine; a busy host
lowers the best-of samples even when the code is unchanged.

CI (`.github/workflows/ci.yml`, job `bench`) runs
`python3 -m benchmarks --json bench.json` on Ubuntu and uploads
`bench.json` as an artifact, including when the gate fails, so the
verdict and every metric are visible on the build page.

## Adding a metric

1. Add the name, label and unit to `RATE_METRICS` or `ABS_METRICS` in
   `benchmarks/compare.py`.
2. Emit it from the matching suite in `benchmarks/suites.py`.
3. Regenerate the baseline; `tests/test_p113_bench.py` asserts that the
   stored baseline contains exactly the known metrics, so a missing entry
   fails the test suite instead of silently skipping the gate.
