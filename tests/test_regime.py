import pandas as pd
import pytest

from app.regime import spy_above_sma


def _make_spy_frame(prices: list[float], start_ts: int = 1_000_000, step: int = 86400) -> pd.DataFrame:
    timestamps = [start_ts + i * step for i in range(len(prices))]
    return pd.DataFrame({
        "ts": timestamps,
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [1_000_000] * len(prices),
    })


def test_spy_above_sma_warmup():
    """First window - 1 bars must return False."""
    prices = [100.0] * 250
    df = _make_spy_frame(prices)
    res = spy_above_sma(df, window=200)

    # First 199 bars should be False
    for i in range(199):
        ts = int(df["ts"].iloc[i])
        assert res[ts] is False, f"Bar {i} should be False during warmup"

    # Bar 200 (index 199): close=100.0, sma=100.0 -> close > sma is False (equal)
    ts_200 = int(df["ts"].iloc[199])
    assert res[ts_200] is False


def test_spy_above_sma_crossing():
    """Verify True when close > SMA and False when close <= SMA."""
    # 200 bars at 100.0, then bar 201 at 105.0, then bar 202 at 95.0
    prices = [100.0] * 200 + [105.0, 95.0]
    df = _make_spy_frame(prices)
    res = spy_above_sma(df, window=200)

    # Bar 201: close 105.0 > SMA (approx 100.025) -> True
    ts_201 = int(df["ts"].iloc[200])
    assert res[ts_201] is True

    # Bar 202: close 95.0 < SMA -> False
    ts_202 = int(df["ts"].iloc[201])
    assert res[ts_202] is False


def test_spy_above_sma_lookahead_free():
    """Modifying future prices does not affect earlier signals."""
    prices_base = [100.0 + i * 0.1 for i in range(210)]
    df_base = _make_spy_frame(prices_base)
    res_base = spy_above_sma(df_base, window=200)

    # Change bar 209 from 120.9 to 10.0 (crash on last day)
    prices_alt = list(prices_base)
    prices_alt[-1] = 10.0
    df_alt = _make_spy_frame(prices_alt)
    res_alt = spy_above_sma(df_alt, window=200)

    # All decisions before the last bar must remain completely identical
    for i in range(209):
        ts = int(df_base["ts"].iloc[i])
        assert res_base[ts] == res_alt[ts], f"Bar {i} leaked future information"


def test_spy_above_sma_empty_and_edge_cases():
    assert spy_above_sma(pd.DataFrame(), window=200) == {}
    assert spy_above_sma(None, window=200) == {}
    df = _make_spy_frame([100.0] * 10)
    assert spy_above_sma(df, window=-1) == {}
