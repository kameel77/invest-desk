"""Testy modułu hypotheses i modelu kosztów."""
from __future__ import annotations

import math
import numpy as np
import pandas as pd
import pytest

from app import config, hypotheses as H
from app.costs import net_return_pct


def test_net_return_pct_multiplicative():
    """Formuła kosztów jest ściśle multiplikatywna i symetryczna dla prowizji i poślizgu."""
    entry_raw = 100.0
    exit_raw = 110.0  # +10% brutto

    comm = config.COMMISSION_PCT   # 0.0035 (0.35%)
    slip = config.SLIPPAGE_PCT     # 0.0010 (0.10%)

    entry_eff = entry_raw * (1.0 + slip) * (1.0 + comm)
    exit_eff = exit_raw * (1.0 - slip) * (1.0 - comm)
    expected_pct = ((exit_eff / entry_eff) - 1.0) * 100.0

    calculated = net_return_pct(entry_raw, exit_raw)
    assert calculated == pytest.approx(expected_pct, abs=1e-6)
    # Zysk netto musi być niższy niż zysk brutto
    assert calculated < 10.0
    assert calculated > 8.5


def test_perf_sharpe_per_trade_without_sqrt252():
    """sharpe_per_trade nie używa annualizacji sqrt(252) i stosuje ddof=1."""
    rets = [2.0, -1.0, 3.0, -0.5, 1.5]
    perf = H._perf(rets, len(rets), "test")
    std = float(np.std(rets, ddof=1))
    expected_sharpe = float(np.mean(rets) / std)

    assert "sharpe_per_trade" in perf
    assert perf["sharpe_per_trade"] == pytest.approx(round(expected_sharpe, 2), abs=1e-2)
    # Upewnij się, że nie został pomnożony przez sqrt(252) (~15.87)
    assert perf["sharpe_per_trade"] < expected_sharpe * 5


def test_driver_sensitivity_timestamp_indexing():
    """_driver_sensitivity wyrównuje serie po ts (timestamp), unikając RangeIndex mismatch."""
    n = 60
    ts_list = [1_700_000_000 + i * 86_400 for i in range(n)]
    stock_df = pd.DataFrame({
        "ts": ts_list,
        "open": [100.0 + i * 0.2 for i in range(n)],
        "high": [101.0 + i * 0.2 for i in range(n)],
        "low": [99.0 + i * 0.2 for i in range(n)],
        "close": [100.0 + i * 0.2 for i in range(n)],
        "volume": [1_000_000] * n,
    })

    # Driver ma ten sam indeks czasowy
    driver_series = pd.Series([50.0 + i * 0.1 for i in range(n)], index=pd.Index(ts_list))
    drivers = {"GOLD": driver_series}

    # Powinno wyliczyć korelację bez błędu concat/reindex
    rets = H._driver_sensitivity(stock_df, drivers, lookback=20, min_corr=0.3, hold=5)
    assert isinstance(rets, list)
