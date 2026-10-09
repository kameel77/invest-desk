"""Unit and integration tests for app/portfolio_sim.py."""
from __future__ import annotations

import math
import numpy as np
import pandas as pd
import pytest

from app import costs, reversion
from app.portfolio_sim import PortfolioSimulator, PositionSlot


def _synthetic_symbol_frame(symbol: str, closes: list[float], spread: float = 0.02) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "ts": [1_700_000_000 + i * 86_400 for i in range(n)],
        "symbol": symbol,
        "open": closes,
        "high": [c * (1 + spread) for c in closes],
        "low": [c * (1 - spread) for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    })


def test_portfolio_sim_reconciliation_with_reversion_simulate(monkeypatch):
    """Reconciliation test: with infinite slots and cash, PortfolioSimulator

    produces identical trade outcomes as reversion.simulate().
    """
    # 30 bars of warm wave, then sharp dip to trigger entry
    wave = [100.0 + math.sin(i * 0.5) * 2.0 for i in range(30)] + [92.0]
    # Then recovery above mean (e.g. 102.0)
    wave += [95.0, 98.0, 102.0]
    df = _synthetic_symbol_frame("TEST_SYM", wave)

    # Monkeypatch bt._cached_frame to return df
    monkeypatch.setattr("app.backtest._cached_frame", lambda sym: df if sym == "TEST_SYM" else None)

    # 1. Single asset simulate()
    single_trades = reversion.simulate(df, as_trades=True, broker_profile="IBKR_TIERED_USD")
    assert len(single_trades) >= 1

    # 2. PortfolioSimulator with infinite slots and large cash
    sim = PortfolioSimulator(
        symbols=["TEST_SYM"],
        initial_cash=1_000_000.0,
        max_slots=100,
        broker_profile="IBKR_TIERED_USD",
    )
    res = sim.run()
    assert "error" not in res
    closed = res["closed_trades"]
    assert len(closed) == len(single_trades)

    # Compare trade 0
    t_single = single_trades[0]
    t_port = closed[0]
    assert t_port["symbol"] == "TEST_SYM"
    assert t_port["reason"] == t_single.reason
    assert pytest.approx(t_port["entry_px"], abs=1e-4) == t_single.entry_px
    assert pytest.approx(t_port["exit_px"], abs=1e-4) == t_single.exit_px


def test_deterministic_priority_and_slot_limit(monkeypatch):
    """When more signals occur than available slots, lowest z-score wins,

    with alphabetical ticker as tie-breaker.
    """
    # Create 3 symbols dipping simultaneously:
    # SYM_A: dips to 94 (z ~ -2.1)
    # SYM_B: dips to 90 (z ~ -3.0) -> deepest, must be picked first!
    # SYM_C: dips to 94 (z ~ -2.1, same as SYM_A) -> tie broken by alphabet: SYM_A before SYM_C
    base_wave = [100.0 + math.sin(i * 0.5) * 1.0 for i in range(25)]
    df_a = _synthetic_symbol_frame("SYM_A", base_wave + [94.0, 96.0, 101.0])
    df_b = _synthetic_symbol_frame("SYM_B", base_wave + [90.0, 93.0, 101.0])
    df_c = _synthetic_symbol_frame("SYM_C", base_wave + [94.0, 96.0, 101.0])

    frames = {"SYM_A": df_a, "SYM_B": df_b, "SYM_C": df_c}
    monkeypatch.setattr("app.backtest._cached_frame", lambda sym: frames.get(sym))

    # Test with max_slots = 1 -> SYM_B must win!
    sim_1 = PortfolioSimulator(
        symbols=["SYM_A", "SYM_B", "SYM_C"],
        initial_cash=10_000.0,
        max_slots=1,
    )
    res_1 = sim_1.run()
    assert "error" not in res_1
    trades_1 = res_1["closed_trades"]
    assert len(trades_1) == 1
    assert trades_1[0]["symbol"] == "SYM_B"
    assert res_1["rejected_signals"]["slot_full"] >= 2

    # Test with max_slots = 2 -> SYM_B and SYM_A must win (SYM_C rejected due to alphabet tie-breaker)!
    sim_2 = PortfolioSimulator(
        symbols=["SYM_A", "SYM_B", "SYM_C"],
        initial_cash=10_000.0,
        max_slots=2,
    )
    res_2 = sim_2.run()
    trades_2 = res_2["closed_trades"]
    assert len(trades_2) == 2
    executed_syms = {t["symbol"] for t in trades_2}
    assert executed_syms == {"SYM_B", "SYM_A"}


def test_no_lookahead_entry_day(monkeypatch):
    """Ensure that signal generated at Close(t) NEVER enters at any price from day t,

    and enters strictly on Open(t+1).
    """
    base_wave = [100.0 + math.sin(i * 0.5) * 1.0 for i in range(25)]
    # Day 25: dip to 88.0 triggers signal at Close(day 25)
    # Day 26: Open is 85.0 (gap down), high 87, low 84, close 86
    # Day 27: Open 95, close 102
    ts_day25 = 1_700_000_000 + 25 * 86_400
    ts_day26 = 1_700_000_000 + 26 * 86_400
    df = _synthetic_symbol_frame("NO_LOOKAHEAD", base_wave + [88.0, 86.0, 102.0])
    # Set day 26 open specifically to 85.0
    df.loc[df["ts"] == ts_day26, "open"] = 85.0

    monkeypatch.setattr("app.backtest._cached_frame", lambda sym: df if sym == "NO_LOOKAHEAD" else None)

    sim = PortfolioSimulator(symbols=["NO_LOOKAHEAD"], initial_cash=10_000.0)
    res = sim.run()
    trades = res["closed_trades"]
    assert len(trades) >= 1
    t0 = trades[0]
    # Entry timestamp must strictly be day 26, NOT day 25!
    assert t0["entry_ts"] == ts_day26
    # Entry price must be 85.0 (day 26 open)
    assert t0["entry_px"] == 85.0


def test_gap_down_below_stop_portfolio(monkeypatch):
    """If price gaps down below stop-loss at market open, exit fills at Open, not stop."""
    base_wave = [100.0 + math.sin(i * 0.5) * 1.0 for i in range(25)]
    # Day 25: entry signal
    # Day 26: entry open at 90.0, close 90.0 (stop around ~85)
    # Day 27: crash opening at 75.0 (well below stop ~85)
    ts_day27 = 1_700_000_000 + 27 * 86_400
    df = _synthetic_symbol_frame("GAP_DOWN", base_wave + [90.0, 90.0, 75.0])
    df.loc[df["ts"] == ts_day27, "open"] = 75.0
    df.loc[df["ts"] == ts_day27, "low"] = 74.0
    df.loc[df["ts"] == ts_day27, "close"] = 75.0

    monkeypatch.setattr("app.backtest._cached_frame", lambda sym: df if sym == "GAP_DOWN" else None)

    sim = PortfolioSimulator(symbols=["GAP_DOWN"], initial_cash=10_000.0)
    res = sim.run()
    trades = res["closed_trades"]
    assert len(trades) >= 1
    t0 = trades[0]
    assert t0["reason"] == "stop-loss"
    assert t0["exit_px"] == 75.0  # Filled at the worse gap open, NOT stop_px!



def test_portfolio_cash_accounting_and_metrics():
    """Verify performance metrics calculation."""
    sim = PortfolioSimulator(symbols=["AAPL"], initial_cash=10_000.0)
    # Fake snapshots
    snapshots = [
        {"ts": 100, "equity": 10_000.0, "cash": 10_000.0, "open_positions_count": 0},
        {"ts": 200, "equity": 10_200.0, "cash": 8_000.0, "open_positions_count": 1},
        {"ts": 300, "equity": 10_500.0, "cash": 10_500.0, "open_positions_count": 0},
    ]
    res = sim._compute_performance(snapshots, [], [])
    assert res["total_return_pct"] == 5.0
    assert res["initial_cash"] == 10_000.0
    assert res["final_equity"] == 10_500.0
    assert res["max_drawdown_pct"] == 0.0
