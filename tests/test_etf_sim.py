"""Unit tests for M1 ETF Trend Following Simulation Engine."""
from __future__ import annotations

import datetime
import math
import numpy as np
import pandas as pd
import pytest

from app import etf_sim


def test_get_eom_dates():
    dates = [
        datetime.date(2023, 1, 3),
        datetime.date(2023, 1, 15),
        datetime.date(2023, 1, 31),
        datetime.date(2023, 2, 1),
        datetime.date(2023, 2, 28),
        datetime.date(2023, 3, 1),
    ]
    eoms = etf_sim.get_eom_dates(dates)
    assert eoms == [datetime.date(2023, 1, 31), datetime.date(2023, 2, 28), datetime.date(2023, 3, 1)]


def test_weight_constraints_and_capping():
    # 4 active assets with distinct volatilities
    active = ["SPY", "TLT", "GLD", "EEM"]
    vols = {"SPY": 0.15, "TLT": 0.10, "GLD": 0.18, "EEM": 0.25}
    
    weights = etf_sim.calculate_inverse_vol_weights(active, vols, max_weight=0.30)
    assert len(weights) == 4
    # All weights <= 0.30
    for s, w in weights.items():
        assert w <= 0.300001
        assert w > 0.0
    # Sum of weights <= 1.0
    assert sum(weights.values()) <= 1.000001
    # Lowest volatility asset (TLT @ 10%) gets the highest weight (capped at 0.30)
    assert weights["TLT"] == 0.30


def test_all_cash_when_no_trend():
    weights = etf_sim.calculate_inverse_vol_weights([], {}, max_weight=0.30)
    assert weights == {}


def test_differential_turnover_costs():
    # Synthetic trending universe
    dates = pd.date_range("2020-01-01", "2021-12-31", freq="B").date
    data = []
    # 10 months flat at 100, then rises to 120 and stays at 120
    for i, d in enumerate(dates):
        px = 100.0 if i < 220 else 120.0
        data.append({
            "date": d,
            "ts": int(pd.Timestamp(d).timestamp()),
            "open": px,
            "high": px + 1.0,
            "low": px - 1.0,
            "close": px,
            "volume": 100000.0,
            "adjclose": px,
        })
    df_flat = pd.DataFrame(data)
    frames = {"SPY": df_flat}
    
    # Run simulation
    res = etf_sim.simulate_m1(frames, symbols=["SPY"], initial_cash=10000.0, fee_rate=0.001)
    assert res["final_equity"] > 0
    assert len(res["rebalance_log"]) > 0
    # Active rebalances after initial entry with zero drift should have minimal turnover (< 0.1%)
    active_rebalances = [r for r in res["rebalance_log"] if "SPY" in r["active_symbols"]]
    assert len(active_rebalances) >= 2
    # First month: initial position entry (30% weight)
    assert active_rebalances[0]["turnover_pct"] == 30.0
    assert active_rebalances[0]["cost"] > 0.0
    # Subsequent months: differential turnover is near zero (< 0.1% due only to cash interest accrual)
    for r in active_rebalances[1:]:
        assert r["turnover_pct"] < 0.1, f"Expected minimal turnover, got {r['turnover_pct']}%"
        assert r["cost"] < 0.05, f"Expected negligible cost, got {r['cost']}"


def test_crisis_2008_protection_and_positive_return(monkeypatch):
    """Verify that in the real 2008 crisis, M1 trend-following delivered positive return."""
    from pathlib import Path
    from app import config, db
    real_db = Path("data/trading.db")
    if not real_db.exists():
        pytest.skip("Historical ETF database data/trading.db not found.")
    
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn
    monkeypatch.setattr(config, "DB_PATH", real_db)

    frames = etf_sim.load_etf_frames(["SPY", "TLT", "GLD", "EEM", "DBC"])
    if not frames or len(frames) < 3:
        pytest.skip("Historical ETF database not populated in this environment.")
        
    res = etf_sim.simulate_m1(
        frames=frames,
        symbols=["SPY", "TLT", "GLD", "EEM", "DBC"],
        initial_cash=10000.0,
        fee_rate=0.0005,
        start_date=datetime.date(2006, 10, 10),
        end_date=datetime.date(2009, 1, 1),
    )
    
    # 2008 return must be positive (TLT +33.9% and GLD +5% protected capital while SPY crashed -38.4%)
    assert 2008 in res["annual_returns"]
    ret_2008 = res["annual_returns"][2008]
    assert ret_2008 >= 0.0, f"Expected 2008 return to be >= 0%, got {ret_2008}%"
    # Max drawdown must stay well within safe limits
    assert res["max_drawdown_pct"] > -20.0, f"Expected MDD > -20%, got {res['max_drawdown_pct']}%"

