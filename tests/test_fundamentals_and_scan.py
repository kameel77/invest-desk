"""Tests for fundamentals NaN/Inf hardening, error handling, and scanner diagnostic reporting."""
from __future__ import annotations

import math
import pytest
from app import db, fundamentals, scanner


def test_num_filters_nan_and_inf():
    assert fundamentals._num(float("nan")) is None
    assert fundamentals._num(float("inf")) is None
    assert fundamentals._num(float("-inf")) is None
    assert fundamentals._num("NaN") is None
    assert fundamentals._num("Infinity") is None
    assert fundamentals._num({"raw": float("nan")}) is None
    assert fundamentals._num(123.45) == 123.45
    assert fundamentals._num("1.25M") == 1250000.0


def test_cached_fundamentals_cleans_nan_and_recovers_gracefully():
    # Insert a payload with NaN simulated
    with db.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS fundamentals (symbol TEXT PRIMARY KEY, fetched_ts INTEGER, payload TEXT)")
        c.execute("INSERT OR REPLACE INTO fundamentals VALUES ('TEST_SYM', 1000, '{\"symbol\": \"TEST_SYM\", \"val\": NaN, \"nested\": {\"bad\": Infinity, \"ok\": 42}}')")

    cached = fundamentals.get_cached_fundamentals("TEST_SYM")
    assert cached is not None
    assert cached["symbol"] == "TEST_SYM"
    assert cached["val"] is None
    assert cached["nested"]["bad"] is None
    assert cached["nested"]["ok"] == 42


def test_save_fundamentals_does_not_raise_on_error(monkeypatch):
    import asyncio
    # Test that save_fundamentals handles DB exceptions gracefully
    from contextlib import contextmanager
    @contextmanager
    def failing_tx():
        raise RuntimeError("DB locked")
        yield

    monkeypatch.setattr(db, "tx", failing_tx)
    asyncio.run(fundamentals.save_fundamentals({"symbol": "FAIL_SYM", "val": 10}))
    # Should not raise exception


def test_scanner_tracks_skipped_candidates_and_caches_last_scan(monkeypatch):
    # Mock evaluate to produce BUY score without liquidity
    from app import signals
    monkeypatch.setattr(signals, "evaluate", lambda sym: {
        "symbol": sym,
        "score": 40.0,
        "price": 100.0,
        "liquidity_ok": False,
        "adv": 50000.0,
        "reasons": ["test"],
        "components": {"trend": 0.5, "momentum": 0.5, "reversion": 0.0},
        "rsi": 50.0,
        "market": "GPW",
    })

    # Run scan on dummy symbol
    import asyncio
    res = asyncio.run(scanner.scan(["TEST.WA"]))
    assert "skipped" in res
    assert len(res["skipped"]) >= 1
    assert any(s["symbol"] == "TEST.WA" for s in res["skipped"])
    assert any("płynność" in s["reason"].lower() or "deduplikacja" in s["reason"].lower() for s in res["skipped"])
    assert scanner.get_last_scan() is not None
    assert scanner.get_last_scan()["ts"] == res["ts"]


def test_reconcile_gpw_bars_replaces_zero_volume_bars(monkeypatch):
    import asyncio
    from app import market

    bars_1d = [
        (1791183600, 100.0, 105.0, 95.0, 102.0, 1000.0, 102.0),
        (1791270000, 102.0, 102.0, 102.0, 102.0, 0.0, 102.0),
    ]
    mock_60m = {
        "timestamp": [1791270000, 1791273600],
        "indicators": {
            "quote": [{
                "open": [102.0, 106.0],
                "high": [107.0, 108.0],
                "low": [101.0, 105.0],
                "close": [106.0, 107.5],
                "volume": [500.0, 600.0],
            }],
            "adjclose": [{"adjclose": [106.0, 107.5]}]
        }
    }
    async def mock_chart(sym, rng, interval):
        if interval == "60m":
            return mock_60m
        return None

    monkeypatch.setattr(market, "_yahoo_chart", mock_chart)
    reconciled = asyncio.run(market._reconcile_gpw_bars_from_60m("TEST.WA", bars_1d))
    assert len(reconciled) == 2
    assert reconciled[1][0] == 1791270000
    assert reconciled[1][1] == 102.0  # open of first 60m bar
    assert reconciled[1][2] == 108.0  # max high
    assert reconciled[1][3] == 101.0  # min low
    assert reconciled[1][4] == 107.5  # last close
    assert reconciled[1][5] == 1100.0  # total volume

