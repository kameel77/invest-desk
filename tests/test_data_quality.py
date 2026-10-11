"""Tests for daily-granularity validation of candle frames (ensure_daily_bars)."""
from __future__ import annotations

import datetime

import pandas as pd

from app import signals


def _ts(y: int, m: int, d: int, hh: int, mm: int = 0) -> int:
    return int(datetime.datetime(y, m, d, hh, mm, tzinfo=datetime.timezone.utc).timestamp())


def _frame(stamps: list[int]) -> pd.DataFrame:
    n = len(stamps)
    return pd.DataFrame({
        "ts": stamps,
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0] * n,
        "volume": [1_000_000] * n,
    })


def test_ensure_daily_bars_drops_monthly_and_intraday():
    start = datetime.date(2020, 1, 1)
    daily = []
    d = start
    while len(daily) < 300:
        if d.weekday() < 5:
            daily.append(_ts(d.year, d.month, d.day, 13, 30))
        d += datetime.timedelta(days=1)
    first_dates = [datetime.date(2019, m, 1) for m in range(1, 13)]
    monthly = [_ts(x.year, x.month, 1, 5) for x in first_dates]
    intraday = [daily[10] + 6 * 3600 + 30 * 60]  # 20:00 UTC same day as a daily bar

    df = _frame(daily + monthly + intraday)
    out = signals.ensure_daily_bars(df)

    assert len(out) == 300
    assert (pd.to_datetime(out["ts"], unit="s", utc=True).dt.hour == 13).all()
    assert out["ts"].is_monotonic_increasing
    assert list(out.index) == list(range(300))


def test_ensure_daily_bars_keeps_dst_mix_untouched():
    stamps = []
    d = datetime.date(2021, 1, 4)
    while len(stamps) < 300:
        if d.weekday() < 5:
            hh = 14 if (d.month <= 2 or d.month >= 11 or (d.month == 3 and d.day < 14)) else 13
            stamps.append(_ts(d.year, d.month, d.day, hh, 30))
        d += datetime.timedelta(days=1)

    df = _frame(stamps)
    out = signals.ensure_daily_bars(df)

    hours = set(pd.to_datetime(out["ts"], unit="s", utc=True).dt.hour)
    assert hours == {13, 14}
    assert len(out) == 300
    assert out["ts"].tolist() == stamps
