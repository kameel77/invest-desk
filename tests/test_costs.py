"""Tests for transaction cost calculations and broker profiles."""
from __future__ import annotations

import pytest

from app import costs


def test_broker_profiles_exist():
    assert "GPW_RETAIL" in costs.PROFILES
    assert "IBKR_TIERED_USD" in costs.PROFILES
    assert "XTB_PLN" in costs.PROFILES
    assert "CONSERVATIVE_US" in costs.PROFILES


def test_get_profile_by_symbol():
    p_gpw = costs.get_profile("KGH.WA")
    assert p_gpw.name == "GPW_RETAIL"
    assert p_gpw.commission_pct == 0.0035

    p_us = costs.get_profile("AAPL")
    assert p_us.name == "IBKR_TIERED_USD"
    assert p_us.commission_per_share > 0


def test_calculate_trade_costs_ibkr_tiered():
    # 10 shares of AAPL at $200 = $2000 notional
    # base comm: 10 * 0.0035 = $0.035 -> clipped to min $0.35
    # reg fee: 10 * 0.0010 = $0.010
    # total comm = 0.35 + 0.010 = $0.36
    # slippage: 2000 * 0.0005 = $1.00
    res = costs.calculate_trade_costs("BUY", price=200.0, shares=10.0, profile="IBKR_TIERED_USD")
    assert res["notional"] == 2000.0
    assert res["slippage"] == 1.00
    assert abs(res["commission"] - 0.36) < 1e-4
    assert res["fx_cost"] == 0.0
    assert abs(res["total_cash"] - (2000.0 + 1.00 + 0.36)) < 1e-4


def test_calculate_trade_costs_xtb_pln():
    # 5 shares of MSFT at $400 = $2000 notional
    # commission: 0%
    # slippage: 2000 * 0.0005 = $1.00
    # fx markup: 2000 * 0.0050 = $10.00
    res = costs.calculate_trade_costs("BUY", price=400.0, shares=5.0, profile="XTB_PLN")
    assert res["commission"] == 0.0
    assert res["slippage"] == 1.00
    assert res["fx_cost"] == 10.00
    assert res["total_cash"] == 2011.00


def test_net_return_pct_with_profile():
    # Round-trip trade on XTB: buying at 100, selling at 105 (gross +5%)
    # With 0.5% FX entry + 0.5% FX exit + 0.05% slippage each way (~1.1% total cost)
    # Net return should be ~ +3.85%
    ret = costs.net_return_pct(100.0, 105.0, profile="XTB_PLN")
    assert 3.7 < ret < 4.0

    # Same trade on IBKR: 0% FX, minimal comm + slippage
    # Net return should be ~ +4.8%
    ret_ibkr = costs.net_return_pct(100.0, 105.0, profile="IBKR_TIERED_USD")
    assert 4.7 < ret_ibkr < 4.95


def test_net_return_pct_backward_compatibility():
    # Standard legacy call without profile
    ret = costs.net_return_pct(100.0, 110.0, commission_pct=0.0035, slippage_pct=0.001)
    assert 8.0 < ret < 10.0
