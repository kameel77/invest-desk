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


def _synthetic_student_t_frame(symbol: str, n_bars: int = 1500, seed: int = 42, df_t: float = 4.0) -> pd.DataFrame:
    """Generate synthetic OHLCV random walk with Student-t fat tails."""
    rng = np.random.default_rng(seed)
    # Scaled to ~1.5% daily volatility
    t_innovations = rng.standard_t(df=df_t, size=n_bars) * 0.015 / math.sqrt(df_t / (df_t - 2.0))
    closes = 100.0 * np.exp(np.cumsum(t_innovations))
    highs = closes * (1.0 + rng.uniform(0.005, 0.02, size=n_bars))
    lows = closes * (1.0 - rng.uniform(0.005, 0.02, size=n_bars))
    opens = np.empty(n_bars)
    opens[0] = closes[0]
    for i in range(1, n_bars):
        opens[i] = np.clip(closes[i - 1] * (1.0 + rng.normal(0, 0.003)), lows[i] * 1.001, highs[i] * 0.999)
    highs = np.maximum(highs, np.maximum(opens, closes) * 1.001)
    lows = np.minimum(lows, np.minimum(opens, closes) * 0.999)

    timestamps = [1_700_000_000 + i * 86_400 for i in range(n_bars)]
    return pd.DataFrame({
        "ts": timestamps,
        "symbol": symbol,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1_000_000.0] * n_bars,
    })


@pytest.mark.parametrize("seed", [42, 123, 999])
def test_portfolio_sim_reconciliation_with_reversion_simulate(monkeypatch, seed):
    """Reconciliation test on 1500 bars synthetic Student-t series (seed parameterised):

    With infinite slots and cash, PortfolioSimulator produces 1:1 identical trades
    and net returns (tol 1e-3 pp) to reversion.simulate().
    """
    df = _synthetic_student_t_frame("SYNTH_TEST", n_bars=1500, seed=seed)
    monkeypatch.setattr("app.backtest._cached_frame", lambda sym: df if sym == "SYNTH_TEST" else None)

    # 1. Single asset simulate() with target budget notional ($1e7 = $1e9 / 100)
    target_notional = 1e7
    single_trades = reversion.simulate(
        df,
        as_trades=True,
        broker_profile="IBKR_TIERED_USD",
        notional=target_notional,
    )
    assert len(single_trades) >= 10, f"Seed {seed} generated too few trades: {len(single_trades)}"

    # 2. PortfolioSimulator with 100 slots and 1e9 cash
    sim = PortfolioSimulator(
        symbols=["SYNTH_TEST"],
        initial_cash=1e9,
        max_slots=100,
        broker_profile="IBKR_TIERED_USD",
    )
    res = sim.run(all_trades=True)
    assert "error" not in res
    port_trades = res["all_closed_trades"]

    assert len(port_trades) == len(single_trades), (
        f"Trade count mismatch for seed {seed}: port={len(port_trades)} vs single={len(single_trades)}"
    )

    # 3. Assert 1:1 identical trades and net_ret within 1e-3 pp
    for i, (t_single, t_port) in enumerate(zip(single_trades, port_trades)):
        assert t_port["symbol"] == "SYNTH_TEST"
        assert t_port["entry_ts"] == t_single.entry_ts, f"Trade {i} entry_ts mismatch"
        assert t_port["exit_ts"] == t_single.exit_ts, f"Trade {i} exit_ts mismatch"
        assert t_port["reason"] == t_single.reason, f"Trade {i} reason mismatch"
        assert pytest.approx(t_port["exit_px"], abs=1e-4) == t_single.exit_px, f"Trade {i} exit_px mismatch"
        assert abs(t_port["net_ret"] - t_single.ret_pct) < 1e-3, (
            f"Trade {i} net_ret mismatch: port={t_port['net_ret']} vs single={t_single.ret_pct}"
        )


@pytest.mark.parametrize("seed", [7, 21, 314])
def test_portfolio_sim_reconciliation_two_calendars(monkeypatch, seed):
    """Reconciliation on two symbols with different calendars.

    Symbol B starts 40 bars later than A and has ~5% of its days removed (gaps).
    With infinite slots and cash, the portfolio must reproduce reversion.simulate()
    1:1 on each symbol separately: a missing bar neither advances the hold counter
    nor triggers an exit or any other state change.
    """
    df_a = _synthetic_student_t_frame("SYM_A", n_bars=1500, seed=seed)
    df_b = _synthetic_student_t_frame("SYM_B", n_bars=1460, seed=seed + 1000)
    df_b["ts"] = df_b["ts"] + 40 * 86_400
    rng = np.random.default_rng(seed)
    gap_mask = rng.random(len(df_b)) < 0.05
    gap_mask[:25] = False  # keep the warmup intact
    df_b = df_b[~gap_mask].reset_index(drop=True)
    assert len(df_b) < 1460 - 20

    frames = {"SYM_A": df_a, "SYM_B": df_b}
    monkeypatch.setattr("app.backtest._cached_frame", lambda sym: frames.get(sym))

    target_notional = 1e7
    sim = PortfolioSimulator(
        symbols=["SYM_A", "SYM_B"],
        initial_cash=1e9,
        max_slots=100,
        broker_profile="IBKR_TIERED_USD",
    )
    res = sim.run(all_trades=True)
    assert "error" not in res

    for sym, df in frames.items():
        single = reversion.simulate(
            df, as_trades=True, broker_profile="IBKR_TIERED_USD", notional=target_notional,
        )
        # simulate() force-closes a position still open on the last bar ("eod"); the
        # portfolio keeps it open (marked to market), so that trailing trade is not comparable.
        single = [t for t in single if t.reason != "eod"]
        port = [t for t in res["closed_trades"] if t["symbol"] == sym]
        assert len(single) >= 10, f"{sym}: too few trades ({len(single)})"
        assert len(port) == len(single), f"{sym}: port={len(port)} vs single={len(single)}"
        for i, (ts_, tp) in enumerate(zip(single, port)):
            assert tp["entry_ts"] == ts_.entry_ts, f"{sym} trade {i} entry_ts"
            assert tp["exit_ts"] == ts_.exit_ts, f"{sym} trade {i} exit_ts"
            assert tp["reason"] == ts_.reason, f"{sym} trade {i} reason"
            assert pytest.approx(tp["exit_px"], abs=1e-4) == ts_.exit_px, f"{sym} trade {i} exit_px"
            assert abs(tp["net_ret"] - ts_.ret_pct) < 1e-3, f"{sym} trade {i} net_ret"


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
