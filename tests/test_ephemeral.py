"""Ozel pencere (ephemeral) isleri: kimlik bilgileri diske yazilmaz.

Eklenti ozel pencereden gelen indirmeyi "ephemeral": true ile isaretler.
Boyle bir is kuyrukta/gecmiste kimlik bilgisi (cerez, ozel baslik, parola,
URL userinfo'su) birakmamalidir; bellekte calisan is ise cereziyle devam
eder. Normal islerin kaliciligi degismez.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import api, nativehost
from ferdim import history as history_mod
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.engine.queue import QUEUE_FILE, QueueManager
from ferdim.paths import DEFAULTS, AppEnv

COOKIE = "oturum=COOKIE-SIRRI-123"
TOKEN = "Bearer TOKEN-SIRRI-456"
PASSWORD = "PAROLA-SIRRI-789"
USER = "kullanici"
URL = f"http://{USER}:{PASSWORD}@127.0.0.1:1/ozel.iso?s=1"
CLEAN_URL = "http://127.0.0.1:1/ozel.iso?s=1"


def _cfg() -> dict:
    cfg = dict(DEFAULTS)
    cfg["retries"] = 0
    cfg["timeout"] = 5
    return cfg


class NativeHostFieldTests(unittest.TestCase):
    """Eklenti -> daemon koprusu 'ephemeral' alanini gecirir (tek + toplu)."""

    def test_download_forwards_ephemeral(self):
        got = nativehost.download_params({"url": URL, "ephemeral": True})
        self.assertIs(got["ephemeral"], True)
        self.assertIs(nativehost.download_params({"url": URL})["ephemeral"],
                      False)
        # JSON'dan metin/sayi gelse de bool'a cevrilir
        self.assertIs(
            nativehost.download_params({"ephemeral": "evet"})["ephemeral"], True)
        self.assertIs(
            nativehost.download_params({"ephemeral": 0})["ephemeral"], False)

    def test_batch_items_carry_ephemeral(self):
        items = nativehost.batch_items({"items": [
            {"url": URL, "ephemeral": True},
            {"url": CLEAN_URL},
        ]})
        self.assertEqual([it.get("ephemeral") for it in items], [True, None])
        self.assertEqual(set(items[0]) - set(nativehost.DOWNLOAD_DEFAULTS),
                         set())


class QueuePersistenceTests(unittest.TestCase):
    """queue.json: ozel pencere isi cerezsiz saklanir, normal is aynen."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ferdim-eph-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.env = AppEnv(root=self.root)
        self.cfg = _cfg()
        self.qm = QueueManager(self.env, self.cfg)
        self.addCleanup(self.qm.shutdown)
        self.ctx = {"qm": self.qm, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}

    def _add(self, **params):
        """Duraklatilmis is ekler: ag trafigi olmadan kalicilik sinanir."""
        r = api.handle(self.ctx, "add", {"paused": True, **params})
        self.assertTrue(r.get("ok"), r)
        job = self.qm.get(r["id"])
        self.assertIsNotNone(job)
        return job

    def _raw(self) -> str:
        return (self.env.state_dir / QUEUE_FILE).read_text("utf-8")

    def _secret_params(self) -> dict:
        return {"url": URL, "cookies": COOKIE, "user": USER,
                "password": PASSWORD, "headers": ["Authorization: " + TOKEN]}

    def test_ephemeral_job_is_stored_without_credentials(self):
        job = self._add(ephemeral=True, **self._secret_params())
        raw = self._raw()
        for secret in (COOKIE, TOKEN, PASSWORD):
            self.assertNotIn(secret, raw, "ozel pencere sirri diske yazildi")
        rec = json.loads(raw)[0]
        self.assertTrue(rec["ephemeral"])
        self.assertEqual(rec["cookies"], "")
        self.assertEqual(rec["headers"], {})
        self.assertEqual(rec["password"], "")
        self.assertEqual(rec["user"], "")
        self.assertEqual(rec["url"], CLEAN_URL)      # userinfo silinir
        # bellekteki is etkilenmez: indirme cereziyle devam eder
        self.assertEqual(job.spec.cookies, COOKIE)
        self.assertEqual(job.spec.headers.get("Authorization"), TOKEN)
        self.assertEqual(job.spec.password, PASSWORD)

    def test_normal_job_still_persists_credentials(self):
        job = self._add(**self._secret_params())
        raw = self._raw()
        self.assertIn(COOKIE, raw)
        self.assertIn(TOKEN, raw)
        self.assertIn(PASSWORD, raw)
        rec = json.loads(raw)[0]
        self.assertFalse(rec["ephemeral"])
        self.assertEqual(rec["cookies"], COOKIE)
        self.assertTrue(job.spec.ephemeral is False)

    def test_ephemeral_job_restores_after_reload(self):
        job = self._add(ephemeral=True, **self._secret_params())
        self.qm.shutdown()               # daemon yeniden basliyor
        qm2 = QueueManager(self.env, self.cfg)
        self.addCleanup(qm2.shutdown)
        again = qm2.get(job.id)
        self.assertIsNotNone(again, "is kuyrukta kalmaliydi")
        self.assertTrue(again.spec.ephemeral)
        self.assertEqual(again.state, JobState.PAUSED)
        self.assertEqual(again.spec.cookies, "")
        self.assertEqual(again.spec.headers, {})
        self.assertEqual(again.url, CLEAN_URL)
        # is gorunumu korunur (dosya adi URL'den turetilir)
        self.assertEqual(again.snapshot()["filename"], "ozel.iso")

    def test_ephemeral_job_keeps_full_url_in_memory(self):
        job = self._add(ephemeral=True, **self._secret_params())
        self.assertEqual(job.url, URL)
        self.assertEqual(job.spec.url, URL)


class HistoryRecordTests(unittest.TestCase):
    """history.db: ozel pencere satiri kimlik tasimaz."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ferdim-eph-h-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.env = AppEnv(root=self.root)
        self.cfg = _cfg()
        self.hist = history_mod.open_history(self.env)
        self.addCleanup(shutil.rmtree, self.root, True)

    def _job(self, jid, *, ephemeral, **spec_kw):
        spec = JobSpec(url=URL, filename="ozel.iso", ephemeral=ephemeral,
                       **spec_kw)
        job = DownloadJob(self.env, self.cfg, spec, job_id=jid)
        job.state = JobState.DONE
        job.total = job.done = 2048
        return job

    def _db_bytes(self) -> bytes:
        return b"".join(p.read_bytes()
                        for p in self.env.state_dir.glob("history.db*"))

    def test_ephemeral_row_has_no_credentials(self):
        job = self._job("e1", ephemeral=True, cookies=COOKIE, user=USER,
                        password=PASSWORD, headers={"Authorization": TOKEN})
        self.assertTrue(self.hist.record(job))
        row = self.hist.get("e1")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["url"], CLEAN_URL)      # userinfo silinir
        self.assertEqual(row["host"], "127.0.0.1")
        for key in ("cookies", "headers", "user", "password"):
            self.assertNotIn(key, row["opts"],
                             f"gecmis opts {key} tasimamali")
        dumped = json.dumps(row, ensure_ascii=False)
        for secret in (COOKIE, TOKEN, PASSWORD):
            self.assertNotIn(secret, dumped)
        blob = self._db_bytes()
        for secret in (COOKIE, TOKEN, PASSWORD):
            self.assertNotIn(secret.encode(), blob, "sir DB dosyasina yazildi")

    def test_ephemeral_row_keeps_metadata(self):
        job = self._job("e2", ephemeral=True, cookies=COOKIE)
        self.hist.record(job)
        row = self.hist.get("e2")
        self.assertEqual(row["filename"], "ozel.iso")
        self.assertEqual(row["bytes"], 2048)
        self.assertEqual(row["size"], 2048)
        self.assertIn("?s=1", row["url"], "sorgu korunur")
        self.assertEqual(row["opts"].get("filename"), "ozel.iso")

    def test_normal_row_keeps_credentials(self):
        job = self._job("n1", ephemeral=False, cookies=COOKIE,
                        password=PASSWORD, headers={"Authorization": TOKEN})
        self.assertTrue(self.hist.record(job))
        row = self.hist.get("n1")
        # gosterilen URL'de sifre yok, kullanici durur (mevcut davranis)
        self.assertEqual(row["url"], f"http://{USER}@127.0.0.1:1/ozel.iso?s=1")
        self.assertEqual(row["opts"]["cookies"], COOKIE)
        self.assertEqual(row["opts"]["password"], PASSWORD)
        self.assertEqual(row["opts"]["headers"], ["Authorization: " + TOKEN])

    def test_redownload_of_ephemeral_row_carries_no_credentials(self):
        job = self._job("e3", ephemeral=True, cookies=COOKIE, user=USER,
                        password=PASSWORD, headers={"Authorization": TOKEN})
        self.hist.record(job)
        opts = self.hist.get("e3")["opts"]
        again = dict(opts)
        again["url"] = self.hist.get("e3")["url"]
        self.assertEqual(again["url"], CLEAN_URL)
        dumped = json.dumps(again, ensure_ascii=False)
        for secret in (COOKIE, TOKEN, PASSWORD):
            self.assertNotIn(secret, dumped)


class ApiRoundTripTests(unittest.TestCase):
    """Ozel pencere isi kuyruk+gecmis uzerinden yeniden indirilebilir ama
    kimlik bilgisi tasimaz."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ferdim-eph-api-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.env = AppEnv(root=self.root)
        self.cfg = _cfg()
        self.qm = QueueManager(self.env, self.cfg)
        self.addCleanup(self.qm.shutdown)
        self.ctx = {"qm": self.qm, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}

    def test_history_redownload_of_ephemeral_job_has_no_cookies(self):
        r = api.handle(self.ctx, "add", {
            "paused": True, "url": URL, "cookies": COOKIE,
            "headers": ["Authorization: " + TOKEN], "ephemeral": True})
        self.assertTrue(r.get("ok"), r)
        job = self.qm.get(r["id"])
        job.state = JobState.DONE          # bitmis gibi gecmise yaz
        self.qm._job_changed(job)
        row = self.qm.history.get(job.id)
        self.assertIsNotNone(row, "biten is gecmise yazilmali")

        again = api.handle(self.ctx, "history-redownload", {"id": job.id})
        self.assertTrue(again.get("ok"), again)
        fresh = self.qm.get(again["id"])
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh.spec.cookies, "")
        self.assertEqual(fresh.spec.headers, {})
        self.assertEqual(fresh.spec.password, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
