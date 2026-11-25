"""ROADMAP 1.12 (lane D): dosya adlari, DST penceresi, VPN coexist, pano.

Tum testler deterministiktir: ag yok, uyku yok; saat/timer/okuyucu enjekte
edilir. Berlin saat dilimi yoksa DST testleri temizce atlanir.
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import notify as notify_mod
from ferdim import util
from ferdim.clipboard import ClipboardWatcher
from ferdim.coexist import (LineMonitor, default_route_interface,
                            is_line_iface, parse_net_dev, parse_route,
                            parse_route6, read_net_dev,
                            read_net_dev_default, resolve_interfaces)
from ferdim.i18n import LANGS, S
from tests.helpers import tmp_root

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover
    ZoneInfo = None


def _berlin():
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo("Europe/Berlin")
    except Exception:
        return None


BERLIN = _berlin()


class FilenameTests(unittest.TestCase):
    def test_01_long_name_is_clipped_to_255_bytes_keeping_extension(self):
        name = util.sanitize_filename("a" * 400 + ".tar.gz")
        self.assertLessEqual(len(name.encode("utf-8")), util.NAME_MAX_BYTES)
        self.assertTrue(name.endswith(".gz"), name[-20:])

    def test_02_multibyte_stem_never_splits_a_character(self):
        # 'i' + 200 x U+0131 (2 bayt): ham ad 400+ bayt
        raw = "i" + "\u0131" * 200 + ".txt"
        name = util.sanitize_filename(raw)
        data = name.encode("utf-8")
        self.assertLessEqual(len(data), util.NAME_MAX_BYTES)
        self.assertEqual(data.decode("utf-8"), name)   # bolunme yok
        self.assertNotIn("\ufffd", name)
        self.assertTrue(name.endswith(".txt"))

    def test_03_huge_extension_falls_back_to_whole_name_clip(self):
        name = util.sanitize_filename("x." + "\u00e9" * 200)
        self.assertLessEqual(len(name.encode("utf-8")), util.NAME_MAX_BYTES)
        self.assertEqual(name.encode("utf-8").decode("utf-8"), name)

    def test_04_plain_short_name_and_invalid_chars(self):
        self.assertEqual(util.sanitize_filename(" normal.txt "), "normal.txt")
        self.assertEqual(util.sanitize_filename("a/b:c?.txt"), "a_b_c_.txt")
        self.assertEqual(util.sanitize_filename(""), "download.bin")
        self.assertEqual(util.sanitize_filename(". ."), "download.bin")


class ContentDispositionTests(unittest.TestCase):
    def test_01_plain_filename(self):
        self.assertEqual(util.parse_content_disposition(
            'attachment; filename="simple.txt"'), "simple.txt")

    def test_02_rfc5987_any_charset(self):
        self.assertEqual(util.parse_content_disposition(
            "attachment; filename*=UTF-8''%C3%A7%C3%B6k.txt"), "çök.txt")
        self.assertEqual(util.parse_content_disposition(
            "attachment; filename*=windows-1254''%E7al%FD%FE.txt"),
            "çalış.txt")
        self.assertEqual(util.parse_content_disposition(
            "attachment; filename*=ISO-8859-1''%E9t%E9.txt"), "été.txt")

    def test_03_star_preferred_over_plain(self):
        self.assertEqual(util.parse_content_disposition(
            "attachment; filename=\"fallback.txt\"; "
            "filename*=UTF-8''%C3%BCst%C3%BCn.txt"), "üstün.txt")

    def test_04_continuations_and_absence(self):
        self.assertEqual(util.parse_content_disposition(
            "attachment; filename*0*=UTF-8''%C3%A7%C3%B6k; "
            "filename*1=.txt"), "çök.txt")
        self.assertIsNone(util.parse_content_disposition("inline"))
        self.assertIsNone(util.parse_content_disposition(None))
        self.assertIsNone(util.parse_content_disposition(""))

    def test_05_regex_fallback_when_email_parser_fails(self):
        value = "attachment; filename*=UTF-8''%C3%A7%C3%B6k.txt"
        with mock.patch.object(util, "_cd_from_email", return_value=None):
            self.assertEqual(util.parse_content_disposition(value), "çök.txt")


@unittest.skipUnless(BERLIN is not None, "zoneinfo/Europe/Berlin yok")
class DstWindowTests(unittest.TestCase):
    def _at(self, month, day, hour, minute, fold=0):
        return dt.datetime(2026, month, day, hour, minute,
                           tzinfo=BERLIN, fold=fold)

    def test_01_spring_forward_start_moves_past_the_gap(self):
        # Berlin 2026-03-29: 02:00 -> 03:00; 02:30 var olmayan bir saat.
        self.assertFalse(util.window_open(
            "02:30", "03:30", self._at(3, 29, 1, 59)))
        self.assertTrue(util.window_open(
            "02:30", "03:30", self._at(3, 29, 3, 0)),
            "bosluktan sonraki ilk gecerli an baslangic olmali")
        self.assertFalse(util.window_open(
            "02:30", "03:30", self._at(3, 29, 3, 30)))

    def test_02_window_inside_the_gap_is_empty_that_day(self):
        self.assertFalse(util.window_open(
            "02:10", "02:20", self._at(3, 29, 3, 0)))

    def test_03_fall_back_uses_first_occurrence_and_opens_once(self):
        # Berlin 2026-10-25: 03:00 -> 02:00; 02:15-02:45 iki kez yasanir.
        first = self._at(10, 25, 2, 30, fold=0)
        second = self._at(10, 25, 2, 30, fold=1)
        self.assertNotEqual(first.utcoffset(), second.utcoffset())
        self.assertTrue(util.window_open("02:15", "02:45", first))
        self.assertFalse(util.window_open("02:15", "02:45", second),
                         "ikinci tekrar pencereyi yeniden acmamali")

    def test_04_naive_datetimes_keep_legacy_semantics(self):
        noon = dt.datetime(2026, 5, 1, 12, 0)
        self.assertTrue(util.window_open("10:00", "13:00", noon))
        self.assertFalse(util.window_open("02:00", "08:00", noon))


ROUTE4 = """\
Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
wg0\t00000000\t00000000\t0003\t0\t0\t600\t00000000\t0\t0\t0
tun0\t00000000\t00000000\t0001\t0\t0\t700\t00000000\t0\t0\t0
"""

ROUTE6 = """\
00000000000000000000000000000000 00 00000000000000000000000000000000 00 00000000000000000000000000000000 00000400 00000001 00000000 00000001 wg0
00000000000000000000000000000000 00 00000000000000000000000000000000 00 00000000000000000000000000000000 ffffffff 00000001 00000000 00200200 lo
fd7a115ca1e00000000000000000000053 80 00000000000000000000000000000000 00 00000000000000000000000000000000 00000400 00000001 00000000 00000001 wg0
"""

NET_DEV = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:    1000      10    0    0    0     0          0         0     2000      20    0    0    0     0       0          0
  eth1:    5000      50    0    0    0     0          0         0     7000      70    0    0    0     0       0          0
   wg0:     300       3    0    0    0     0          0         0      400       4    0    0    0     0       0          0
"""


class CoexistVpnTests(unittest.TestCase):
    def test_01_parse_route_lowest_metric_default(self):
        self.assertEqual(parse_route(ROUTE4), "wg0")

    def test_02_parse_route6_default_only(self):
        self.assertEqual(parse_route6(ROUTE6), "wg0")

    def test_03_no_default_route_returns_none(self):
        text = ROUTE4.splitlines()[0] + "\neth0\t0001A8C0\t0\t1\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
        self.assertIsNone(parse_route(text))
        self.assertIsNone(parse_route6(""))
        self.assertIsNone(parse_route6("bozuk satir\n"))

    def test_04_default_route_interface_takes_injectable_paths(self):
        root = tmp_root(self, "ferdim-p112-route-")
        r4, r6 = root / "route", root / "route6"
        r4.write_text(ROUTE4, "utf-8")
        r6.write_text(ROUTE6, "utf-8")
        self.assertEqual(default_route_interface(str(r4), str(r6)), "wg0")

    def test_05_include_overrides_virtual_skip(self):
        self.assertEqual(parse_net_dev(NET_DEV), 12_000)        # yalniz eth1
        self.assertEqual(parse_net_dev(NET_DEV, include=("wg0",)), 700)
        self.assertFalse(is_line_iface("wg0"))
        self.assertTrue(is_line_iface("wg0", include=("wg0",)))

    def test_06_read_net_dev_default_follows_route_and_falls_back(self):
        root = tmp_root(self, "ferdim-p112-dev-")
        nd, r4 = root / "dev", root / "route"
        nd.write_text(NET_DEV, "utf-8")
        r4.write_text(ROUTE4, "utf-8")
        self.assertEqual(read_net_dev_default(str(nd), str(r4),
                                              str(root / "yok")), 700)
        self.assertEqual(read_net_dev(str(nd), include=("wg0",)), 700)
        empty = root / "bos"
        empty.write_text(ROUTE4.splitlines()[0] + "\n", "utf-8")
        # varsayilan rota yok: eski davranis (eth1 = 12_000)
        self.assertEqual(read_net_dev_default(str(nd), str(empty),
                                              str(root / "yok")), 12_000)

    def test_07_resolve_interfaces_config_override(self):
        root = tmp_root(self, "ferdim-p112-res-")
        r4, r6 = root / "route", root / "route6"
        r4.write_text(ROUTE4, "utf-8")
        r6.write_text(ROUTE6, "utf-8")
        self.assertEqual(resolve_interfaces(
            {"coexist_interfaces": ["wg0", "eth1"]}, str(r4), str(r6)),
            ("wg0", "eth1"))
        self.assertEqual(resolve_interfaces(
            {"coexist_interfaces": "wg0, eth1"}, str(r4), str(r6)),
            ("wg0", "eth1"))
        self.assertEqual(resolve_interfaces({}, str(r4), str(r6)), ("wg0",))
        missing = str(root / "yok")
        self.assertEqual(resolve_interfaces({}, missing, missing), ())

    def test_08_monitor_reads_the_configured_interfaces(self):
        mon = LineMonitor(interfaces=("wg0",))
        with mock.patch("ferdim.coexist.read_net_dev", return_value=42) as rd:
            self.assertEqual(mon._read(), 42)
        rd.assert_called_once_with(include=("wg0",))


class FakeTimer:
    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled = delay, fn, False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn()


class FakeQM:
    def __init__(self):
        self.jobs: dict = {}
        self.calls: list = []

    def add(self, spec, job_id=None, paused=False):
        jid = "j%d" % (len(self.jobs) + 1)
        job = SimpleNamespace(id=jid, url=spec.url,
                              state="paused" if paused else "queued")
        self.jobs[jid] = job
        self.calls.append(("add", jid, job.state))
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def control(self, job_id, action, delete_files=False):
        job = self.jobs.get(job_id)
        self.calls.append((action, job_id))
        if job is None:
            return False
        if action == "resume":
            job.state = "queued"
        return True

    def remove(self, job_id):
        self.calls.append(("remove", job_id))
        return self.jobs.pop(job_id, None) is not None


URL = "http://example.test/film.mp4"


class ClipboardConfirmTests(unittest.TestCase):
    def _watcher(self, action_result=True, confirm=True, grace=10.0):
        self.qm = FakeQM()
        self.events: list = []
        self.timers: list = []

        def action(title, body, label, cb):
            self.events.append(("action", title, body, label, cb))
            return action_result

        def plain(title, body):
            self.events.append(("plain", title, body))
            return True

        def timer(delay, fn):
            t = FakeTimer(delay, fn)
            self.timers.append(t)
            return t

        watcher = ClipboardWatcher(
            self.qm, {"clipboard_confirm": confirm, "language": "en",
                      "clipboard_extensions": ""},
            notify_action=action, notify_plain=plain, timer=timer, grace=grace)
        return watcher

    def test_01_action_capable_backend_queues_paused_then_resumes(self):
        w = self._watcher()
        w._queue(URL)
        self.assertEqual(len(self.qm.jobs), 1)
        job = next(iter(self.qm.jobs.values()))
        self.assertEqual(job.state, "paused")
        self.assertEqual(self.events[0][0], "action")
        self.assertEqual(self.events[0][3], "Undo")
        self.assertEqual(self.timers[0].delay, 10.0)
        self.assertEqual(job.state, "paused", "pencere dolmadan baslamamali")
        self.timers[0].fire()
        self.assertEqual(job.state, "queued")

    def test_02_undo_removes_the_paused_job(self):
        w = self._watcher()
        w._queue(URL)
        cb = self.events[0][4]
        self.assertTrue(cb())
        self.assertEqual(self.qm.jobs, {})
        self.assertIn(("remove", "j1"), self.qm.calls)

    def test_03_undo_does_not_touch_a_started_job(self):
        w = self._watcher()
        w._queue(URL)
        cb = self.events[0][4]
        self.timers[0].fire()
        self.assertFalse(cb())
        self.assertEqual(len(self.qm.jobs), 1)

    def test_04_backend_without_actions_queues_immediately(self):
        w = self._watcher(action_result=False)
        w._queue(URL)
        job = next(iter(self.qm.jobs.values()))
        self.assertEqual(job.state, "queued")
        self.assertIn(("resume", "j1"), self.qm.calls)
        self.assertEqual(self.events[-1][0], "plain")
        self.assertEqual(self.timers, [])

    def test_05_confirm_disabled_keeps_today_behaviour(self):
        w = self._watcher(confirm=False)
        w._queue(URL)
        job = next(iter(self.qm.jobs.values()))
        self.assertEqual(job.state, "queued")
        self.assertEqual([e[0] for e in self.events], ["plain"])
        self.assertEqual(self.timers, [])

    def test_06_i18n_keys_present_in_every_language(self):
        for key in ("clipboard_confirm_title", "clipboard_confirm_body",
                    "clipboard_undo", "clipboard_queued_title",
                    "clipboard_queued_body"):
            self.assertEqual(set(S[key]), set(LANGS), key)


class NotifyActionTests(unittest.TestCase):
    def test_01_action_event_matches_only_our_key(self):
        line = ("/org/freedesktop/Notifications: org.freedesktop."
                "Notifications.ActionInvoked (uint32 7, 'undo_1_2')")
        self.assertTrue(notify_mod._action_event(line, "undo_1_2"))
        self.assertFalse(notify_mod._action_event(line, "undo_1_3"))
        self.assertFalse(notify_mod._action_event(
            "(uint32 7, 'undo_1_2')", "undo_1_2"))

    def test_02_scan_invokes_undo_once(self):
        hits = []
        lines = ["giris", "ActionInvoked (uint32 7, 'undo_9_9')",
                 "ActionInvoked (uint32 8, 'baska')"]
        self.assertTrue(notify_mod._scan_actions(
            lines, "undo_9_9", lambda: hits.append(1)))
        self.assertEqual(hits, [1])
        self.assertFalse(notify_mod._scan_actions(
            ["yok"], "undo_9_9", lambda: hits.append(2)))
        self.assertEqual(hits, [1])

    def test_03_freedesktop_command_carries_actions_and_timeout(self):
        cmd = notify_mod._freedesktop_action_cmd(
            "Baslik", "Govde", "undo_1_1", "Geri al", 10_000)
        self.assertIn("@as ['undo_1_1', 'Geri al']", cmd)
        self.assertEqual(cmd[-1], "int32 10000")
        idx = cmd.index("org.freedesktop.Notifications.Notify")
        self.assertEqual(cmd[idx + 1:idx + 5],
                         ["'FerDim'", "uint32 0", "'ferdim'", "'Baslik'"])

    def test_04_no_bus_or_rejected_call_is_unsupported(self):
        self.assertFalse(notify_mod.notify_action(
            "t", "b", "Undo", bus_available=lambda: False))
        self.assertFalse(notify_mod.notify_action(
            "t", "b", "Undo", bus_available=lambda: True,
            run=lambda cmd: (False, "")))

    def test_05_successful_call_uses_freedesktop(self):
        seen = []

        def call(cmd):
            seen.append(cmd)
            return True, "(uint32 1,)"

        self.assertTrue(notify_mod.notify_action(
            "t", "b", "Undo", bus_available=lambda: True, run=call))
        self.assertTrue(seen and any("Notify" in part for part in seen[0]))
        self.assertTrue(any("@as [" in part for part in seen[0]))

    def test_06_send_plain_needs_a_bus(self):
        self.assertFalse(notify_mod.send_plain(
            "t", "b", bus_available=lambda: False))
        self.assertTrue(notify_mod.send_plain(
            "t", "b", bus_available=lambda: True, run=lambda cmd: True))


if __name__ == "__main__":
    unittest.main()
