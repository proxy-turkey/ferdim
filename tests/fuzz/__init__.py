"""ROADMAP 1.8: guvenilmeyen girdiyi okuyan parser'lar icin fuzz harness'i.

Yalniz stdlib. Iki kullanim yolu:

    python3 -m tests.fuzz --seconds 60 --seed 1     # kampanya (CI)
    python3 -m unittest tests.test_fuzz_smoke -v    # deterministik duman

Hedefler: m3u8 (ferdim.engine.hls), MPD (ferdim.engine.dash),
metalink, .ferdim-merkle bildirimi (ferdim.cdc.Manifest), ZIP/TAR
listeleme (ferdim.archive.describe) ve SOCKS5 sunucu yaniti
(ferdim.engine.socks).

Bulgular `tests/fuzz/known.json` icindeki sinif kimlikleriyle
karsilastirilir; bilinmeyen bir sinif gorulurse CLI sifir disi cikar.
"""
from __future__ import annotations

from .harness import (DEFAULT_TIMEOUT, KNOWN_PATH, Finding, FuzzTimeout,
                      Mutator, Target, TargetResult, exc_id, known_suggestion,
                      load_known, minimize, run_target, time_limit)
from .targets import (SMOKE_ITERATIONS, SMOKE_SEED, TARGETS, seeds_clean,
                      xml_bomb_doc, xml_bomb_mpd)

__all__ = [
    "DEFAULT_TIMEOUT", "KNOWN_PATH", "Finding", "FuzzTimeout", "Mutator",
    "Target", "TargetResult", "exc_id", "known_suggestion", "load_known",
    "minimize", "run_target", "time_limit", "SMOKE_ITERATIONS", "SMOKE_SEED",
    "TARGETS", "seeds_clean", "xml_bomb_doc", "xml_bomb_mpd",
]
