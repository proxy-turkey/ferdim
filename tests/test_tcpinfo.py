"""TCP_INFO okuma ve tikaniklik sinyali testleri.

Parca ayristirma yapay tamponla (eski/yeni cekirdek boylari), sinyal ise
enjekte edilen ornekler + sahte saat ile calisir: uyku yok. Gercek soket
testi yalnizca TCP_INFO destekleniyorsa kosar (Linux), degilse atlanir.
"""
from __future__ import annotations

import socket
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import tcpinfo
from ferdim.engine import httpcli
from tests.helpers import start_server


def build(**fields) -> bytes:
    """Adlandirilmis alanlarla yapay struct tcp_info (232 bayt)."""
    raw = bytearray(tcpinfo.SIZE)
    for name, off, fmt in tcpinfo._FIELDS:
        if name in fields:
            struct.pack_into(fmt, raw, off, fields[name])
    return bytes(raw)


class ParseTests(unittest.TestCase):
    def test_full_buffer(self):
        raw = build(rtt_us=25_000, rttvar_us=1_000, cwnd=10,
                    total_retrans=7, bytes_sent=1_000_000,
                    bytes_retrans=20_000, segs_out=900)
        info = tcpinfo.parse(raw)
        self.assertEqual(info["rtt_us"], 25_000)
        self.assertEqual(info["rttvar_us"], 1_000)
        self.assertEqual(info["cwnd"], 10)
        self.assertEqual(info["total_retrans"], 7)
        self.assertEqual(info["bytes_sent"], 1_000_000)
        self.assertEqual(info["bytes_retrans"], 20_000)
        self.assertEqual(info["segs_out"], 900)

    def test_short_old_kernel_buffer(self):
        """Eski cekirdek kisa tampon dondurur: RTT var, yeni alanlar yok."""
        raw = build(rtt_us=9_000, total_retrans=2)[:128]
        info = tcpinfo.parse(raw)
        self.assertEqual(info["rtt_us"], 9_000)
        self.assertEqual(info["total_retrans"], 2)
        self.assertNotIn("bytes_sent", info)
        self.assertNotIn("segs_out", info)

    def test_no_core_fields(self):
        self.assertIsNone(tcpinfo.parse(b""))
        self.assertIsNone(tcpinfo.parse(bytes(64)))   # ne RTT ne retrans

    def test_read_unsupported_socket(self):
        class Fake:
            def getsockopt(self, *_a):
                raise OSError("yok")
        self.assertIsNone(tcpinfo.read(Fake()))
        self.assertIsNone(tcpinfo.read(None))

    def test_read_without_tcp_info(self):
        with mock.patch.object(socket, "TCP_INFO", None):
            sock = mock.Mock()
            self.assertIsNone(tcpinfo.read(sock))
            sock.getsockopt.assert_not_called()

    def test_read_parses_socket_option(self):
        sock = mock.Mock()
        sock.getsockopt.return_value = build(rtt_us=12_000, total_retrans=1)
        info = tcpinfo.read(sock)
        self.assertEqual(info["rtt_us"], 12_000)


@unittest.skipUnless(hasattr(socket, "TCP_INFO"), "TCP_INFO yok (Linux degil)")
class RealSocketTests(unittest.TestCase):
    def test_loopback_tcp_info(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            client = socket.create_connection(srv.getsockname(), timeout=5)
            try:
                peer, _ = srv.accept()
                try:
                    info = tcpinfo.read(peer)
                    self.assertIsNotNone(info)
                    self.assertIn("rtt_us", info)
                    self.assertGreaterEqual(info["rtt_us"], 0)
                finally:
                    peer.close()
            finally:
                client.close()
        finally:
            srv.close()


class ResponseSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_urllib_response_exposes_socket(self):
        opt = httpcli.DlOptions(timeout=5)
        resp = httpcli.open_range(f"{self.base}/file.bin", opt, 0, 1023)
        try:
            sock = httpcli.response_socket(resp)
            self.assertIsNotNone(sock)
            self.assertTrue(callable(sock.getsockopt))
            if hasattr(socket, "TCP_INFO"):
                info = tcpinfo.read(sock)
                self.assertIsNotNone(info)
        finally:
            resp.close()

    def test_h3_or_synthetic_response_has_no_socket(self):
        class FakeH3:
            _session = object()
        self.assertIsNone(httpcli.response_socket(FakeH3()))
        self.assertIsNone(httpcli.response_socket(None))


class CongestionSignalTests(unittest.TestCase):
    def test_flat_link_is_not_congested(self):
        sig = tcpinfo.CongestionSignal()
        for i in range(4):
            sig.add("a", {"rtt_us": 20_000, "total_retrans": 0,
                          "segs_out": 100 * (i + 1),
                          "bytes_sent": 1_000_000 * (i + 1),
                          "bytes_retrans": 0}, now=float(i * 2))
        self.assertAlmostEqual(sig.retrans_ratio(), 0.0)
        self.assertFalse(sig.congested())

    def test_retransmits_and_rtt_inflation_are_congestion(self):
        sig = tcpinfo.CongestionSignal()
        sig.add("a", {"rtt_us": 20_000, "total_retrans": 0,
                      "segs_out": 100, "bytes_sent": 1_000_000,
                      "bytes_retrans": 0}, now=0.0)
        # iki ardisik ornek: %5 yeniden iletim + 2x RTT
        for i, t in enumerate((2.0, 4.0), start=1):
            sig.add("a", {"rtt_us": 40_000, "total_retrans": 50 * i,
                          "segs_out": 100 * (i + 1),
                          "bytes_sent": 1_000_000 * (i + 1),
                          "bytes_retrans": 50_000 * i}, now=t)
        self.assertGreaterEqual(sig.retrans_ratio(), 0.04)
        self.assertGreaterEqual(sig.rtt_inflation(), 1.5)
        self.assertTrue(sig.congested())

    def test_retrans_ratio_alone_is_not_enough(self):
        """Yeniden iletim var ama RTT sismemis: tikaniklik denmez."""
        sig = tcpinfo.CongestionSignal()
        sig.add("a", {"rtt_us": 20_000, "total_retrans": 0,
                      "segs_out": 100, "bytes_sent": 1_000_000,
                      "bytes_retrans": 0}, now=0.0)
        for i, t in enumerate((2.0, 4.0), start=1):
            sig.add("a", {"rtt_us": 20_000, "total_retrans": 50 * i,
                          "segs_out": 100 * (i + 1),
                          "bytes_sent": 1_000_000 * (i + 1),
                          "bytes_retrans": 50_000 * i}, now=t)
        self.assertFalse(sig.congested())

    def test_old_kernel_retrans_rate_fallback(self):
        sig = tcpinfo.CongestionSignal()
        sig.add("a", {"rtt_us": 20_000, "total_retrans": 0}, now=0.0)
        for i, t in enumerate((2.0, 4.0), start=1):
            sig.add("a", {"rtt_us": 40_000,
                          "total_retrans": 20 * i}, now=t)
        self.assertGreater(sig.retrans_rate(), 0)
        self.assertTrue(sig.congested())

    def test_stale_links_are_forgotten_and_reset(self):
        sig = tcpinfo.CongestionSignal()
        sig.add("a", {"rtt_us": 40_000, "total_retrans": 10,
                      "bytes_sent": 100, "bytes_retrans": 10}, now=0.0)
        sig.add("a", {"rtt_us": 40_000, "total_retrans": 20,
                      "bytes_sent": 200, "bytes_retrans": 20}, now=2.0)
        sig.add("b", {"rtt_us": 5_000}, now=2.0 + sig.STALE + 1.0)
        self.assertEqual(sig.retrans_ratio(), 0.0)
        self.assertEqual(sig.rtt_inflation(), 1.0)
        self.assertFalse(sig.congested())
        sig.reset()
        self.assertEqual(sig.retrans_rate(), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
