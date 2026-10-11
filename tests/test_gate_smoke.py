"""Smoke tests for validation gates and market data queries."""
from __future__ import annotations

import math
from unittest.mock import AsyncMock, patch
import numpy as np
import pandas as pd
import pytest

from app import market
from tools.run_hypotheses import run_us_reversion_gate


def _synthetic_symbol_frame(symbol: str, prices: list[float]) -> pd.DataFrame:
    base_ts = 1_600_000_000
    rows = []
    for i, p in enumerate(prices):
        rows.append({
            "ts": base_ts + i * 86400,
            "open": float(p),
            "high": float(p) * 1.01,
            "low": float(p) * 0.99,
            "close": float(p),
            "volume": 1_000_000,
        })
    return pd.DataFrame(rows)


import asyncio


def test_market_yahoo_range_20y_mapping():
    """Verify that (rng='max', interval='1d') maps to '20y' to prevent Yahoo monthly downsampling."""
    with patch("app.market._yahoo_chart", new_callable=AsyncMock) as mock_chart, \
         patch("app.market._candle_cache", {}):
        mock_chart.return_value = None  # Stop after chart call

        asyncio.run(market.fetch_candles("SPY", rng="max", interval="1d", force=True))

        mock_chart.assert_called_once_with("SPY", "20y", "1d")


def test_gate_v1_smoke_synthetic(monkeypatch):
    """Verify that run_us_reversion_gate runs without crashing and correctly computes profile returns."""
    # Synthetic series with mean-reversion swings
    base = [100.0 + 5.0 * math.sin(i * 0.2) for i in range(300)]
    df_spy = _synthetic_symbol_frame("SPY", base)
    df_qqq = _synthetic_symbol_frame("QQQ", [p * 0.8 for p in base])

    frames = {"SPY": df_spy, "QQQ": df_qqq}
    monkeypatch.setattr("app.backtest._cached_frame", lambda sym, **kwargs: frames.get(sym))
    monkeypatch.setattr("tools.run_hypotheses._anchor", lambda: int(df_spy["ts"].iloc[-1]))

    report = run_us_reversion_gate(
        symbols=["SPY", "QQQ"],
        folds=3,
        test_months=2.0,
        target_profile="XTB_USD",
        capital=16000.0,
        slots=8,
        n_bootstrap=100,
    )

    assert "gate_decision" in report
    assert report["gate_decision"] in ("GO", "NO-GO")
    assert "criteria" in report
    assert "profiles" in report
    assert "XTB_USD" in report["profiles"]
    assert "ci_90" in report["profiles"]["XTB_USD"]


def test_gate_v2_crit6_runs_from_foreign_cwd(monkeypatch):
    """Kryterium 6 ma działać także wtedy, gdy bramkę uruchomiono spoza katalogu repo."""
    from tools import run_hypotheses

    monkeypatch.chdir(run_hypotheses.ROOT.parent)
    assert run_hypotheses._run_reconciliation_test() is True
