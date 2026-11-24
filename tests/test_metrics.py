"""Prometheus metin bicimi (ROADMAP 1.8): sayac deltalari, kartesite siniri,
kuyruk orneklemesi, job sayac kancasi ve /metrics HTTP yolu."""
from __future__ import annotations

import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ferdim.metrics as metrics
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from ferdim.server.web import make_server
from tests.helpers import tmp_root


class FakeJob:
    """Collector.observe icin en az alan kumesi (ag/thread yok)."""

    def __init__(self, jid, host, done=0, state=JobState.QUEUED, url=""):
        self.id = jid
        self._host = host
        self.url = url or f"https://{host}/f.bin"
        self.done = done
        self.state = state
        self._active = 0
        self.speed = 0.0
        self.stats_counters = {"retries": {}, "throttles": {}}


def fresh_env(case, prefix="ferdim-metrics-") -> AppEnv:
    return AppEnv(root=tmp_root(case, prefix))


class CollectorTests(unittest.TestCase):
    def test_01_families_and_format(self):
        col = metrics.Collector()
        job = FakeJob("a", "example.com", done=1000)
        col.observe([job], coexist_active=True)
        text = col.render()
        for family in ("ferdim_bytes_total", "ferdim_jobs_total",
                       "ferdim_active_links", "ferdim_retries_total",
                       "ferdim_throttles_total", "ferdim_speed_bytes",
                       "ferdim_queue_jobs", "ferdim_coexist_active",
                       "ferdim_build_info"):
            self.assertIn(f"# TYPE {family} ", text, family)
        self.assertIn('ferdim_bytes_total{host="example.com"} 1000', text)
        self.assertIn('ferdim_queue_jobs{state="queued"} 1', text)
        self.assertIn('ferdim_speed_bytes{scope="aggregate"} 0', text)
        self.assertIn("ferdim_coexist_active 1", text)
        self.assertRegex(text, r'ferdim_build_info\{version="[^"]+"\} 1')
        self.assertTrue(text.endswith("\n"))

    def test_02_bytes_counter_is_monotonic(self):
        col = metrics.Collector()
        job = FakeJob("a", "example.com", done=1000)
        col.observe([job])
        job.done = 3000
        col.observe([job])
        self.assertIn('ferdim_bytes_total{host="example.com"} 3000',
                      col.render())
        # is kuyruktan silinse de sayac geri sarmaz
        col.observe([])
        self.assertIn('ferdim_bytes_total{host="example.com"} 3000',
                      col.render())
        # yeniden eklenen is ayni ana makineye yeni bayt ekler (sayac artar)
        job.done = 10
        col.observe([job])
        self.assertIn('ferdim_bytes_total{host="example.com"} 3010',
                      col.render())

    def test_02b_restart_resets_the_baseline(self):
        """job.done geri sararsa (yeniden baslatma / merkle onarimi) taban
        duser: negatif delta yok, yeniden indirilen baytlar sayilir."""
        col = metrics.Collector()
        job = FakeJob("a", "example.com", done=3000)
        col.observe([job])
        job.done = 10                     # bastan indirme
        col.observe([job])
        self.assertIn('ferdim_bytes_total{host="example.com"} 3000',
                      col.render())
        job.done = 500
        col.observe([job])                # 10 -> 500 yeniden indirildi
        self.assertIn('ferdim_bytes_total{host="example.com"} 3490',
                      col.render())

    def test_03_retry_and_throttle_deltas(self):
        col = metrics.Collector()
        job = FakeJob("a", "example.com", done=5)
        job.stats_counters["retries"]["example.com"] = 2
        col.observe([job])
        job.stats_counters["retries"]["example.com"] = 5
        job.stats_counters["throttles"]["example.com"] = 1
        col.observe([job])
        text = col.render()
        self.assertIn('ferdim_retries_total{host="example.com"} 5', text)
        self.assertIn('ferdim_throttles_total{host="example.com"} 1', text)

    def test_04_jobs_total_counts_terminal_once(self):
        col = metrics.Collector()
        job = FakeJob("a", "example.com", state=JobState.DOWNLOADING)
        col.observe([job])
        self.assertNotIn("ferdim_jobs_total{", col.render())
        job.state = JobState.DONE
        col.observe([job])
        col.observe([job])           # ayni is iki kez sayilmaz
        self.assertIn('ferdim_jobs_total{state="done"} 1', col.render())

    def test_05_host_cardinality_is_bounded(self):
        col = metrics.Collector()
        jobs = [FakeJob(f"j{i}", f"h{i}.example", done=i + 1)
                for i in range(metrics.MAX_HOSTS + 25)]
        col.observe(jobs)
        text = col.render()
        labels = [ln for ln in text.splitlines()
                  if ln.startswith("ferdim_bytes_total{")]
        self.assertEqual(len(labels), metrics.MAX_HOSTS + 1)
        self.assertTrue(any('host="other"' in ln for ln in labels), labels[:3])
        # kalanlar toplandi: first 25 en cok baytli ana makine other'a duser
        other = int(next(ln for ln in labels
                         if 'host="other"' in ln).rsplit(" ", 1)[1])
        self.assertEqual(other, sum(range(1, 26)))

    def test_06_label_escaping(self):
        col = metrics.Collector()
        col.observe([FakeJob("a", 'a"b\\c', done=1)])
        self.assertIn(r'host="a\"b\\c"', col.render())
        # collect: ornekleyici yoksa bos govde degil, gecerli metin
        self.assertIn("# TYPE ferdim_build_info",
                      metrics.render(object()))

    def test_07_broken_job_does_not_abort_sampling(self):
        """Bir isin okunamasi tick'in tamamini dusurmemeli."""
        class Bad:
            id = "a"
            done = 50

            @property
            def state(self):
                raise RuntimeError("bozuk is")

        col = metrics.Collector()
        col.observe([Bad(), FakeJob("b", "ok.example", done=100)])
        text = col.render()
        self.assertIn('ferdim_bytes_total{host="ok.example"} 100', text)
        self.assertNotIn("bad.example", text)

    def test_08_counters_snapshot_takes_job_lock(self):
        """stats_counters is kilidi altinda kopyalanir (isciler ayni
        sozlugu degistirir; gezinme RuntimeError firlatabilirdi)."""
        class ProbeLock:
            entered = 0

            def __enter__(self):
                ProbeLock.entered += 1

            def __exit__(self, *exc):
                return False

        job = FakeJob("a", "example.com")
        job._lock = ProbeLock()
        job.stats_counters = {"retries": {"example.com": 3}, "throttles": {}}
        col = metrics.Collector()
        col.observe([job])
        self.assertEqual(ProbeLock.entered, 1)
        self.assertIn('ferdim_retries_total{host="example.com"} 3',
                      col.render())


class QueueWiringTests(unittest.TestCase):
    def test_01_queue_samples_jobs(self):
        env = fresh_env(self)
        cfg = env.load_config()
        qm = QueueManager(env, cfg)
        try:
            job = qm.add(JobSpec(url="http://example.com/a.bin"), paused=True)
            job.done = 2000
            qm._tick()
            text = metrics.render(qm)
            self.assertIn('ferdim_bytes_total{host="example.com"}', text)
            self.assertIn('ferdim_queue_jobs{state="paused"} 1', text)
            job._active = 2
            job.speed = 4096.0
            qm._tick()
            text = metrics.render(qm)
            self.assertIn("ferdim_active_links 2", text)
            self.assertIn('ferdim_speed_bytes{scope="aggregate"} 4096', text)
        finally:
            qm.shutdown()


class JobCounterHookTests(unittest.TestCase):
    """job.py'deki en kucuk kanca: _src_fail / _note_limit / _demote."""

    def test_01_counters_fill(self):
        env = fresh_env(self)
        cfg = env.load_config()
        job = DownloadJob(env, cfg, JobSpec(url="http://example.com/x.bin"))
        src = job._srcs[0]
        self.assertEqual(job.stats_counters, {"retries": {}, "throttles": {}})
        job._src_fail(src, "conn")
        self.assertEqual(job.stats_counters["retries"]["example.com"], 1)
        job._src_fail(src, "stall")
        self.assertEqual(job.stats_counters["retries"]["example.com"], 2)
        job._note_limit(src)
        self.assertEqual(job.stats_counters["throttles"]["example.com"], 1)
        job._demote(src, "HTTP 429", 1.0)
        self.assertEqual(job.stats_counters["throttles"]["example.com"], 2)
        # baglanti kalabaligi 429/503 sayilmaz
        job._demote(src, "baglanti reddedildi (limit?)", 1.0)
        self.assertEqual(job.stats_counters["throttles"]["example.com"], 2)


class MetricsHttpTests(unittest.TestCase):
    def setUp(self):
        self.env = fresh_env(self)
        self.cfg = self.env.load_config()
        self.qm = QueueManager(self.env, self.cfg)
        self.ctx = {"qm": self.qm, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}
        self.env.token_path.write_text("testtoken\n")
        self.httpd = make_server(self.env, self.ctx, 0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.qm.shutdown()

    def _status(self, path: str) -> int:
        try:
            with urllib.request.urlopen(f"{self.base}{path}") as r:
                r.read()
            return 200
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_01_requires_token_header(self):
        self.assertEqual(self._status("/metrics"), 401)
        # sorgu dizesinde token kabul edilmez
        self.assertEqual(self._status("/metrics?token=testtoken"), 401)

    def test_02_serves_exposition(self):
        req = urllib.request.Request(f"{self.base}/metrics",
                                     headers={"X-FerDim-Token": "testtoken"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.headers["Content-Type"].startswith("text/plain"))
            body = r.read().decode()
        for family in ("ferdim_bytes_total", "ferdim_active_links",
                       "ferdim_speed_bytes", "ferdim_build_info"):
            self.assertIn(family, body, family)


if __name__ == "__main__":
    unittest.main(verbosity=2)
