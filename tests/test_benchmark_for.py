"""Tests for the exchange-aware benchmark resolver."""

import unittest

import pytest

from tradingagents.dataflows.utils import benchmark_for, DEFAULT_BENCHMARK


@pytest.mark.unit
class TestBenchmarkFor(unittest.TestCase):
    def test_us_ticker_falls_back_to_spy(self):
        for ticker in ("AAPL", "MSFT", "BRK-B", "BRK.A"):
            self.assertEqual(benchmark_for(ticker), DEFAULT_BENCHMARK)

    def test_indian_suffixes_map_to_indian_indices(self):
        self.assertEqual(benchmark_for("RELIANCE.NS"), "^NSEI")
        self.assertEqual(benchmark_for("TCS.NS"), "^NSEI")
        self.assertEqual(benchmark_for("RELIANCE.BO"), "^BSESN")

    def test_known_global_suffixes(self):
        self.assertEqual(benchmark_for("CNC.TO"), "^GSPTSE")
        self.assertEqual(benchmark_for("7203.T"), "^N225")
        self.assertEqual(benchmark_for("0700.HK"), "^HSI")
        self.assertEqual(benchmark_for("VOD.L"), "^FTSE")
        self.assertEqual(benchmark_for("BHP.AX"), "^AXJO")

    def test_unknown_suffix_falls_back_to_default(self):
        self.assertEqual(benchmark_for("FOO.XYZ"), DEFAULT_BENCHMARK)

    def test_case_insensitive_suffix(self):
        self.assertEqual(benchmark_for("reliance.ns"), "^NSEI")
        self.assertEqual(benchmark_for("Reliance.Ns"), "^NSEI")

    def test_non_string_or_empty_returns_default(self):
        self.assertEqual(benchmark_for(""), DEFAULT_BENCHMARK)
        self.assertEqual(benchmark_for(None), DEFAULT_BENCHMARK)  # type: ignore[arg-type]
