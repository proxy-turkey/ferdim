"""Ogrenilen ana makine profilleri (ROADMAP 1.6).

Kapsam: kalici depo (atomik yazim, LRU, suresi dolma), anahtar/RTT bandi,
isin ogrenilmis tavandan baslamasi (ceza tabani dahil), learn_hosts
anahtari, API ve CLI komutlari. Governor kararlari deterministik sahte
zamanla dogrulanir (uyku yok).
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ferdim.api as api
import ferdim.cli as cli
import ferdim.engine.job as jobmod
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.paths import AppEnv
from ferdim.profiles import (FILE_NAME, HostProfiles, host_key, open_profiles,
                             rtt_band)
from tests.helpers import tmp_root

DAY = 86400


class FakeTime:
    """jobmod.time yerine: sleep yok, zaman elle ilerletilir."""

    def __init__(self, start=1000.0):
        self.t = start

    def monotonic(self):
        return self.t

    def time(self):
        return 1_790_000_000.0

    def sleep(self, _x):
        self.t += 0.01


def make_store(case, **kw):
    env = AppEnv(root=tmp_root(case, "ferdim-prof-"))
    return env, HostProfiles(env, **kw)


def make_job(case, segments=8):
    """Ag olmadan gozlenen is: profil baslangici bu nesnede dogrulanir."""
    env = AppEnv(root=tmp_root(case, "ferdim-prof-job-"))
    cfg = env.load_config()
    job = DownloadJob(env, cfg, JobSpec(url="http://ornek.test/dosya.bin",
                                        segments=segments))
    job.kind = "http"
    job.state = JobState.DOWNLOADING
    job.total = 100_000_000
    job._host = "ornek.test"
    job._allowed = segments
    job._seg_max = segments
    job._gov["t"] = jobmod.time.monotonic()
    job._gov["done"] = job.done
    return job, env


class HostKeyTests(unittest.TestCase):
    def test_key_is_lowercase_host_without_default_port(self):
        self.assertEqual(host_key("https://Example.COM/a"), "example.com")
        self.assertEqual(host_key("https://ornek.com:443/a"), "ornek.com")
        self.assertEqual(host_key("ftp://ornek.com:21/d"), "ornek.com")
        self.assertEqual(host_key("http://ornek.com:8080/a"), "ornek.com:8080")
        self.assertEqual(host_key("ornek.com"), "ornek.com")
        self.assertEqual(host_key("ornek.com:8080"), "ornek.com:8080")
        self.assertEqual(host_key("http://[::1]:8080/a"), "[::1]:8080")
        self.assertEqual(host_key("http://[::1]/a"), "[::1]")
        self.assertEqual(host_key("sayfa/alt"), "")
        self.assertEqual(host_key(""), "")

    def test_rtt_bands(self):
        self.assertEqual(rtt_band(0), "")
        self.assertEqual(rtt_band(0.01), "low")
        self.assertEqual(rtt_band(0.05), "mid")
        self.assertEqual(rtt_band(0.19), "mid")
        self.assertEqual(rtt_band(0.2), "high")


class StoreTests(unittest.TestCase):
    def test_record_persists_and_reloads(self):
        env, store = make_store(self)
        self.assertTrue(store.record("ornek.test", links=6, rtt=0.01,
                                     per_link_bps=1_000_000))
        path = env.state_dir / FILE_NAME
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        saved = json.loads(path.read_text("utf-8"))
        self.assertEqual(saved["version"], 1)
        fresh = HostProfiles(env)                  # yeniden okuma
        e = fresh.get("https://ornek.test/x")
        self.assertIsNotNone(e)
        self.assertEqual(e["host"], "ornek.test")
        self.assertEqual(e["links"], 6)
        self.assertEqual(e["rtt_band"], "low")
        self.assertEqual(e["per_link_bps"], 1_000_000)
        self.assertEqual(e["samples"], 1)
        self.assertFalse(e["throttled"])
        self.assertGreater(e["last_seen"], 0)

    def test_links_and_speed_are_smoothed(self):
        _env, store = make_store(self)
        store.record("ornek.test", links=8, per_link_bps=1_000_000)
        store.record("ornek.test", links=4, per_link_bps=2_000_000)
        e = store.get("ornek.test")
        self.assertEqual(e["links"], 6)            # (8+4)/2
        self.assertEqual(e["per_link_bps"], 1_500_000)
        self.assertEqual(e["samples"], 2)
        # throttle gorulduyse isaret kalici, samples artmaya devam eder
        store.record("ornek.test", throttled=True)
        e = store.get("ornek.test")
        self.assertTrue(e["throttled"])
        self.assertEqual(e["links"], 6)            # olcum yok: degismez
        self.assertEqual(e["samples"], 3)

    def test_expired_hosts_are_dropped(self):
        now = [1000.0]
        env, store = make_store(self, now_fn=lambda: now[0])
        store.record("eski.test", links=4)
        now[0] += 91 * DAY
        store.record("yeni.test", links=8)
        self.assertIsNone(store.get("eski.test"))
        self.assertEqual([h["host"] for h in store.hosts()], ["yeni.test"])
        # suresi dolan kayit yeni depoda da yok (dosyada temizlendi)
        self.assertIsNone(HostProfiles(env).get("eski.test"))

    def test_expiry_applies_on_read_without_a_write(self):
        """Okuma da suresi dolani gizler: araya record() girmesi gerekmez."""
        now = [1000.0]
        _env, store = make_store(self, now_fn=lambda: now[0])
        store.record("eski.test", links=4)
        self.assertEqual(len(store.hosts()), 1)
        now[0] += 91 * DAY
        self.assertEqual(store.hosts(), [])          # yazim yok, yine de bos
        self.assertIsNone(store.get("eski.test"))

    def test_store_is_bounded_lru(self):
        now = [1000.0]
        _env, store = make_store(self, max_hosts=3, now_fn=lambda: now[0])
        for name in ("a.test", "b.test", "c.test", "d.test"):
            now[0] += 10
            store.record(name, links=2)
        hosts = [h["host"] for h in store.hosts()]
        self.assertEqual(hosts, ["d.test", "c.test", "b.test"])   # en yeni once
        self.assertIsNone(store.get("a.test"))

    def test_corrupt_file_is_ignored(self):
        env, _store = make_store(self)
        (env.state_dir / FILE_NAME).write_text("{bozuk", "utf-8")
        store = HostProfiles(env)
        self.assertIsNone(store.get("ornek.test"))
        self.assertTrue(store.record("ornek.test", links=3))
        self.assertEqual(HostProfiles(env).get("ornek.test")["links"], 3)

    def test_forget_and_clear(self):
        _env, store = make_store(self)
        store.record("a.test", links=2)
        store.record("b.test", links=4)
        self.assertTrue(store.forget("https://a.test/x"))
        self.assertFalse(store.forget("a.test"))
        self.assertEqual(store.clear(), 1)
        self.assertEqual(store.hosts(), [])


class JobSeedTests(unittest.TestCase):
    def setUp(self):
        self.ft = FakeTime()
        self.real_time = jobmod.time
        jobmod.time = self.ft

    def tearDown(self):
        jobmod.time = self.real_time

    def _tick(self, job, bytes_per_sec, dt=2.6):
        self.ft.t += dt
        job.done += int(bytes_per_sec * dt)
        job._governor_tick()

    def test_seed_starts_at_learned_links(self):
        job, env = make_job(self, segments=16)
        open_profiles(env).record("ornek.test", links=5, rtt=0.02,
                                  per_link_bps=500_000)
        job._seed_profile()
        self.assertEqual(job._allowed, 5)
        self.assertEqual(job._server_floor, 1)     # ceza gorulmedi
        self.assertEqual(job._last_limit_ts, 0.0)
        self.assertEqual(job.snapshot()["segments_effective"], 5)
        # azami guc yonu korunur: governor hala yukari yoklar
        self._tick(job, 1_000_000)
        self.assertEqual(job._allowed, 6)

    def test_seed_is_clamped_to_job_segments(self):
        job, env = make_job(self, segments=8)
        open_profiles(env).record("ornek.test", links=30)
        job._seed_profile()
        self.assertEqual(job._allowed, 8)

    def test_throttled_host_seeds_server_floor(self):
        job, env = make_job(self, segments=16)
        open_profiles(env).record("ornek.test", links=4, throttled=True)
        job._seed_profile()
        self.assertEqual(job._allowed, 4)
        self.assertEqual(job._server_floor, 4)     # ceza tabani ogrenildi
        self.assertGreater(job._last_limit_ts, 0.0)
        # ceza penceresi: hemen buyume yok, tabanin altina da inilmez
        self._tick(job, 4_000_000)
        self.assertEqual(job._allowed, 4)
        # pencere dolunca governor yeniden yoklar (profil yalniz baslangic)
        self.ft.t += 61
        self._tick(job, 4_000_000)
        self.assertEqual(job._allowed, 5)

    def test_seed_only_once_per_job(self):
        job, env = make_job(self, segments=16)
        open_profiles(env).record("ornek.test", links=5)
        job._seed_profile()
        self._tick(job, 1_000_000)
        self.assertEqual(job._allowed, 6)
        job._allowed = 3                            # gercek olcum daraltti
        job._seed_profile()                         # ikinci kesif yok
        self.assertEqual(job._allowed, 3)

    def test_seed_respects_external_cap(self):
        """Birlikte yasam tavani tohumdan once kurulduysa asilmaz."""
        job, env = make_job(self, segments=16)
        open_profiles(env).record("ornek.test", links=9)
        job.set_external_cap(3)
        job._seed_profile()
        self.assertLessEqual(job._allowed, 3)
        self.assertEqual(job._allowed, 3)
        self.assertLessEqual(job.snapshot()["segments_effective"], 3)
        # tavan isin mevcut baglanti sayisinin altinda kalmazsa da
        # tohum AYNI kilit altinda tavana kirpilir
        job2, env2 = make_job(self, segments=16)
        job2._allowed = 2                      # ceza isareti kurulmaz
        job2.set_external_cap(3)
        open_profiles(env2).record("ornek.test", links=9)
        job2._seed_profile()
        self.assertEqual(job2._allowed, 3)
        self.assertLessEqual(job2._allowed, job2._ceiling_locked())

    def test_learn_hosts_false_disables_seed(self):
        job, env = make_job(self, segments=16)
        job.cfg["learn_hosts"] = False
        open_profiles(env).record("ornek.test", links=5)
        job._seed_profile()
        self.assertEqual(job._allowed, 16)          # soguk baslangic


class JobReportTests(unittest.TestCase):
    def _observed(self, job, links=6, ema=2_000_000):
        job.done = 4 << 20
        job._allowed = links
        job._gov["ema"] = ema
        job._gov["best"] = ema
        job._gov["best_workers"] = links
        job._probe_rtt = 0.08

    def test_report_writes_observed_profile(self):
        job, env = make_job(self, segments=16)
        self._observed(job, links=6, ema=2_400_000)
        job._report_profile()
        e = open_profiles(env).get("ornek.test")
        self.assertEqual(e["links"], 6)
        self.assertEqual(e["rtt_band"], "mid")
        self.assertEqual(e["per_link_bps"], 400_000)   # 2.4MB/s / 6
        self.assertEqual(e["samples"], 1)
        self.assertFalse(e["throttled"])
        # ayni baytlar tekrar bildirilmez
        job._report_profile()
        self.assertEqual(open_profiles(env).get("ornek.test")["samples"], 1)

    def test_report_records_throttle_cap(self):
        job, env = make_job(self, segments=16)
        self._observed(job, links=8)
        job._throttle_seen = True                  # 429/503 goruldu
        job._safe_links = 3                        # sunucunun kabul ettigi sayi
        job._report_profile()
        e = open_profiles(env).get("ornek.test")
        self.assertTrue(e["throttled"])
        self.assertEqual(e["links"], 3)            # guvenli tavan bildirilir

    def test_report_needs_meaningful_transfer(self):
        job, env = make_job(self, segments=8)
        self._observed(job, links=4)
        job.done = 4096                            # 1 MB alti: ogrenilecek sey yok
        job._report_profile()
        self.assertIsNone(open_profiles(env).get("ornek.test"))

    def test_report_needs_a_governor_measurement(self):
        job, env = make_job(self, segments=8)
        job.done = 4 << 20
        job._report_profile()                      # ema 0: hiz hic olculmedi
        self.assertIsNone(open_profiles(env).get("ornek.test"))

    def test_learn_hosts_false_disables_report(self):
        job, env = make_job(self, segments=8)
        job.cfg["learn_hosts"] = False
        self._observed(job, links=4)
        job._report_profile()
        self.assertIsNone(open_profiles(env).get("ornek.test"))
        self.assertFalse((env.state_dir / FILE_NAME).exists())


class ApiCliTests(unittest.TestCase):
    def setUp(self):
        self.env = AppEnv(root=tmp_root(self, "ferdim-prof-api-"))
        self.cfg = self.env.load_config()
        # gate yalniz env kullanir: gercek kuyruk (thread) gerekmez
        self.ctx = {"qm": None, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}

    def test_learn_hosts_default_and_config_set(self):
        self.assertTrue(self.cfg["learn_hosts"])
        r = api.handle(self.ctx, "config-set",
                       {"config": {"learn_hosts": False}})
        self.assertTrue(r["ok"], r)
        self.assertFalse(self.cfg["learn_hosts"])
        self.assertFalse(self.env.load_config()["learn_hosts"])
        # metin deger de guvenli araliga cekilir
        api.handle(self.ctx, "config-set", {"config": {"learn_hosts": "false"}})
        self.assertFalse(self.cfg["learn_hosts"])
        api.handle(self.ctx, "config-set", {"config": {"learn_hosts": "on"}})
        self.assertTrue(self.cfg["learn_hosts"])

    def test_api_hosts_list_forget_clear(self):
        store = open_profiles(self.env)
        store.record("a.test", links=4, rtt=0.01)
        store.record("b.test", links=8, throttled=True)
        r = api.handle(self.ctx, "hosts", {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["count"], 2)
        self.assertEqual({h["host"] for h in r["hosts"]}, {"a.test", "b.test"})
        bad = api.handle(self.ctx, "hosts", {"forget": "yok.test"})
        self.assertFalse(bad["ok"])
        gone = api.handle(self.ctx, "hosts", {"forget": "https://a.test/x"})
        self.assertTrue(gone["ok"])
        self.assertEqual(api.handle(self.ctx, "hosts", {})["count"], 1)
        cleared = api.handle(self.ctx, "hosts", {"clear": True})
        self.assertEqual(cleared["removed"], 1)
        self.assertEqual(api.handle(self.ctx, "hosts", {})["hosts"], [])

    def test_cli_hosts_parser_and_output(self):
        args = cli.build_parser().parse_args(["hosts"])
        self.assertIs(args.fn, cli.cmd_hosts)
        self.assertFalse(args.clear)
        self.assertIsNone(args.forget)
        rows = [{"host": "ornek.test", "links": 6, "rtt_band": "low",
                 "per_link_bps": 1_048_576, "samples": 2,
                 "last_seen": 1_790_000_000}]
        out, code = self._run_cli([], {"ok": True, "hosts": rows})
        self.assertEqual(code, 0)
        self.assertIn("ornek.test", out)
        self.assertIn("6", out)

    def test_cli_hosts_empty_forget_and_clear(self):
        out, code = self._run_cli([], {"ok": True, "hosts": []})
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())
        out, code = self._run_cli(["--forget", "ornek.test"],
                                  {"ok": True, "forgotten": "ornek.test"})
        self.assertEqual(code, 0)
        self.assertIn("ornek.test", out)
        out, code = self._run_cli(["--forget", "yok.test"],
                                  {"ok": False, "error": "profil yok"})
        self.assertEqual(code, 1)
        out, code = self._run_cli(["--clear"], {"ok": True, "removed": 3})
        self.assertEqual(code, 0)
        self.assertIn("3", out)

    def _run_cli(self, argv, reply):
        """Daemon'siz CLI calistirma: sahte istemci yaniti verir."""
        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def request(self, cmd, **kw):
                return reply

        buf = io.StringIO()
        with mock.patch.object(cli, "_client", lambda env, spawn=True:
                               FakeClient()), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            a = cli.build_parser().parse_args(["hosts"] + argv)
            code = a.fn(a, self.env)
        return buf.getvalue(), code


if __name__ == "__main__":
    unittest.main(verbosity=2)
