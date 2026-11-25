# Benchmarks

`benchmarks/` is the executable performance gate for ROADMAP 1.13. It
measures the engine, the content-defined chunker, queue persistence and
`stats()`, the full stats frame, and the idle cost of the daemon — then
compares the result with a stored baseline and with absolute product
floors. A metric that regresses by **more than 20 %** or violates a floor
makes the run exit non-zero, which is what the CI `bench` job enforces.

Everything is Python stdlib only; the only network traffic is loopback
against a server the benchmark starts itself.

## Running

```bash
python3 -m benchmarks                     # full run + baseline comparison
python3 -m benchmarks --quick             # small workloads, no comparison
python3 -m benchmarks --only queue        # one suite (engine, chunker, queue, idle)
python3 -m benchmarks --json bench.json   # results + verdicts + sample spread
python3 -m benchmarks --update-baseline   # rewrite benchmarks/baseline.json
```

A full run takes roughly 85 s on a laptop-class machine (four daemon
startups and 512 MiB of engine payload dominate); `--quick` takes about
30 s, is meant for local iteration, and skips the comparison. The `bench`
gate must be decided with the full run.

`--json` records every metric, the raw samples per metric and a spread
summary (`min` / `median` / `max` / `rel`), together with the per-metric
verdicts, so a failing build page shows both the number and its noise.

Temporary roots (`/tmp/ferdim-bench-*`), the local HTTP server, the SSE
client and the benchmark daemon are removed/terminated in `finally`
blocks: a run leaves no directories, sockets or processes behind.

## What is measured

| Metric | How | Aggregation |
|---|---|---|
| `engine_mib_s` | 512 MiB sparse file served over loopback with HTTP Range support by a server in a **separate process** (no GIL sharing); downloaded by a real `DownloadJob` with 8 segments. The clock stops when the last byte reaches the `.part` file, so the final fsync/rename does not mix storage speed into the metric. | best of 3 |
| `cdc_mib_s` | `ferdim.cdc.make_manifest` (FastCDC + SHA-256) over 64 MiB of deterministic pseudo-random bytes | best of 3 |
| `queue_bulk_add_jobs_s` | 500 jobs added inside one `qm.bulk_save()` (job creation + the single durable queue write on exit), on a separate copy of the 10 000-job queue | median of 5 |
| `queue_load_jobs_s` | a new `QueueManager` on the same state directory loads the 10 000 jobs | median of 5 |
| `stats_calls_s` | `qm.stats()` over the 10 000-job queue (measured before the bulk adds) | median of 9 |
| `stats_frame_ms` | full frame on the 10 000-job queue: `qm.stats()` + `hub.sock_frame(...)` JSON encoding (the socket watcher payload) | median of 9 |
| `idle_cpu_pct`, `idle_rss_mib` | daemon, empty queue, sampled from `/proc/<pid>` for 8 s | mean CPU over the window / max RSS |
| `idle_loaded_*` | same daemon on 10 000 **paused** jobs | same |
| `idle_queued_*` | 10 000 jobs, the first 500 **queued** (the rest paused): the busy tick scans the whole queue and the size prober works | same |
| `idle_sse_*` | the queued layout plus one HTTP client on `/api/events` (token read from the state dir); the client drains frames and the run fails if no frame arrives | same |

Paused jobs are used for the bulk/persistence benchmarks: the size prober
only touches `queued` jobs and the scheduler only starts `queued` jobs, so
no network probe or download runs during them. In the queued daemon
layouts the URLs point at `127.0.0.1:1` (connection refused) and a
category window (`video`, `00:00–00:00`, always closed) prevents any job
from starting, so the prober can run without external traffic while the
queue stays loaded. 500 queued jobs (rather than all 10 000) are enough to
exercise the tick and prober paths: the prober scans the job list per
claim, and an all-queued queue would spend minutes in that O(n²) startup
scan, measuring startup rather than idle behaviour.

`--quick` shrinks the payloads (128 MiB engine, 16 MiB chunker) and the
job counts (2 000 jobs, 100 bulk adds, 100 queued).

### Engine checkpoint path

The engine benchmark must cross the crash-safe checkpoint path, not just
the happy-path transfer. `CHECKPOINT_INTERVAL` (2 s in production) is
therefore temporarily overridden to 0.2 s inside the benchmark process;
the 512 MiB transfer then passes the checkpoint (fsync + meta write) path
five to six times. A counting wrapper around `DownloadJob._checkpoint`
asserts that at least three checkpoints happened during the measured
transfer, so a future refactor that stops checkpointing fails the
benchmark instead of silently measuring nothing. The override is restored
in a `finally` block and never leaves the benchmark process.

## Calibration and normalisation

Two fixed workloads score the host; their best-of-5/3 times are stored in
the baseline next to the metrics:

- **CPU** (`calibration`): 4 MiB SHA-256 + a 200 000-step pure-Python
  loop. Pure computation.
- **I/O** (`io_calibration`): write ~15 MiB of JSON + `fsync`, then read
  and parse it. This stands in for the kernel/storage path.

Rates are compared after scaling the stored baseline by
`current_score / baseline_score`:
CPU-bound metrics (`cdc_mib_s`, `queue_load_jobs_s`, `stats_calls_s`)
use the CPU score; I/O-path metrics (`engine_mib_s`,
`queue_bulk_add_jobs_s`) use the I/O score. Scaling the loopback engine
or an fsync-bound queue write by the CPU score would mis-attribute a
faster CPU to a faster disk, and vice versa. A baseline without
`io_calibration` (written by an older revision) is compared with an I/O
factor of 1.0 and the run prints a reminder to refresh it.

Latency is the mirror image: `stats_frame_ms` is compared as
`baseline / cpu_factor`, and its floor is divided by the same factor.

## Thresholds

Two independent gates are applied to every metric:

1. **Drift** ±20 % against the machine-normalised baseline (rates: fail
   below 80 % of the target; latencies and absolute metrics: fail above
   120 %).
2. **Product floor** — an absolute bar that does not move with the
   baseline:
   - `engine_mib_s` ≥ 150 MiB/s
   - `idle_loaded_cpu_pct` and `idle_queued_cpu_pct` ≤ 5 % of a core
   - loaded `idle_*_rss_mib` ≤ 300 MiB
   - `stats_frame_ms` ≤ 150 ms (CPU-normalised: divided by the CPU factor,
     so a slower host is allowed proportionally longer)

Idle CPU drift never fails below `max(baseline * 1.2, 2 %)`, so tick-level
noise on an idle daemon cannot fail a build. The SSE variant has no
separate CPU floor: its steady-state cost is the deliberate per-second
stats recomputation, already bounded by the `stats_frame_ms` floor, so it
is drift-gated only.

RSS and CPU are sensitive to the interpreter and OS build, so they are
compared (drift **and** floors) only when the baseline and the current run
share the same platform and Python minor version; otherwise they are
reported as `skip`. Rate, latency and their floors are always checked.
Metrics missing from the baseline are reported as `new` and are still
subject to their floors — a stale baseline cannot hide a floor violation.

## Regenerating the baseline

After merging a change that legitimately moves performance (and after
merging the other 1.13 performance lanes), regenerate the stored baseline
on the machine that should define it and commit the file:

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
verdict, each metric and its sample spread are visible on the build page.

## Adding a metric

1. Add a `Metric` entry (label, unit, family, calibration scale, optional
   floor) to `METRICS` in `benchmarks/compare.py`.
2. Emit it from the matching suite in `benchmarks/suites.py` and return
   its raw samples.
3. Test the comparison path in `tests/test_p113_bench.py` and regenerate
   the baseline.
