"""ETF Trend Following (M1) simulation engine.

Implements Time Series Momentum on a multi-asset ETF basket:
- Monthly rebalance on End-of-Month (EOM) sessions.
- Execution on Open of the first trading day of the following month (zero lookahead).
- 10-month SMA trend filter on dividend-adjusted prices (adjclose).
- Inverse-volatility risk parity sizing with a 30% single-asset cap and no leverage (sum <= 1.0).
- Differential rebalance turnover modeling with broker transaction costs.
- Benchmark comparison against SPY Buy & Hold and 60/40 (SPY/TLT).
"""
from __future__ import annotations

import datetime
import math
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from . import db


DEFAULT_M1_UNIVERSE = ["SPY", "TLT", "GLD", "EEM", "DBC"]


def load_etf_frames(symbols: Sequence[str] = DEFAULT_M1_UNIVERSE) -> dict[str, pd.DataFrame]:
    """Load daily candles for the given symbols from SQLite, keyed by symbol."""
    frames: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        rows = db.get_candles(sym, limit=10000)
        if not rows:
            continue
        data = []
        for r in rows:
            data.append({
                "ts": r["ts"],
                "open": float(r["open"] or 0.0),
                "high": float(r["high"] or 0.0),
                "low": float(r["low"] or 0.0),
                "close": float(r["close"] or 0.0),
                "volume": float(r["volume"] or 0.0),
                "adjclose": float(r["adjclose"] or r["close"] or 0.0),
            })
        df = pd.DataFrame(data)
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["ts"], unit="s").dt.date
        df = df.sort_values("ts").drop_duplicates("date").reset_index(drop=True)
        frames[sym.upper()] = df
    return frames


def get_eom_dates(dates: pd.Series | Sequence[datetime.date]) -> list[datetime.date]:
    """Identify the last trading date of each calendar month in the given series."""
    df_d = pd.DataFrame({"date": sorted(set(dates))})
    df_d["dt"] = pd.to_datetime(df_d["date"])
    df_d["year_month"] = df_d["dt"].dt.to_period("M")
    eom_dates = df_d.groupby("year_month")["date"].max().tolist()
    return sorted(eom_dates)


def compute_monthly_sma(df: pd.DataFrame, window_months: int = 10) -> pd.DataFrame:
    """Compute monthly SMA on adjclose using EOM prices.
    
    Returns DataFrame indexed by date (EOM dates) with columns:
    ['adjclose', 'sma_10m', 'trend_long']
    """
    df_calc = df.copy()
    eom_dates = set(get_eom_dates(df_calc["date"]))
    eom_df = df_calc[df_calc["date"].isin(eom_dates)].copy()
    eom_df = eom_df.sort_values("date").reset_index(drop=True)
    
    eom_df["sma_10m"] = eom_df["adjclose"].rolling(window=window_months).mean()
    # Trend long requires price strictly above 10-month SMA and not NaN
    eom_df["trend_long"] = (eom_df["adjclose"] > eom_df["sma_10m"]) & (~eom_df["sma_10m"].isna())
    return eom_df


def compute_realized_volatility(df: pd.DataFrame, as_of_date: datetime.date, window_days: int = 60) -> float:
    """Compute 60-day annualized realized volatility from daily returns strictly prior to as_of_date."""
    sub = df[df["date"] <= as_of_date].tail(window_days + 1)
    if len(sub) < 15:
        return 0.15  # Default conservative volatility if history is short
    
    # Daily percentage returns on adjclose
    rets = sub["adjclose"].pct_change().dropna()
    if len(rets) < 10:
        return 0.15
    std = float(rets.std())
    ann_vol = std * math.sqrt(252)
    return max(ann_vol, 0.05)  # Floor at 5% volatility to prevent division by zero


def calculate_inverse_vol_weights(
    active_symbols: list[str],
    volatilities: Mapping[str, float],
    max_weight: float = 0.30
) -> dict[str, float]:
    """Calculate inverse-volatility weights with a maximum asset weight cap and sum <= 1.0."""
    if not active_symbols:
        return {}
    
    raw_inv = {s: 1.0 / max(volatilities.get(s, 0.15), 0.01) for s in active_symbols}
    sum_inv = sum(raw_inv.values())
    if sum_inv <= 0:
        return {s: 0.0 for s in active_symbols}
    
    weights = {s: raw_inv[s] / sum_inv for s in active_symbols}
    
    # Cap weights iteratively
    capped = {}
    excess_weight = 0.0
    uncapped = list(active_symbols)
    
    for _ in range(5):  # At most a few passes to distribute
        newly_capped = []
        for s in uncapped:
            if weights[s] > max_weight:
                capped[s] = max_weight
                newly_capped.append(s)
        if not newly_capped:
            break
        for s in newly_capped:
            uncapped.remove(s)
        
        sum_capped = sum(capped.values())
        rem_budget = 1.0 - sum_capped
        if uncapped and rem_budget > 0:
            uncapped_raw = {s: raw_inv[s] for s in uncapped}
            sum_uncapped_raw = sum(uncapped_raw.values())
            for s in uncapped:
                weights[s] = (uncapped_raw[s] / sum_uncapped_raw) * rem_budget
        else:
            break
    
    for s, w in capped.items():
        weights[s] = w
        
    # Ensure no weight exceeds cap and sum <= 1.0
    for s in weights:
        weights[s] = min(weights[s], max_weight)
    
    total_w = sum(weights.values())
    if total_w > 1.0:
        weights = {s: w / total_w for s, w in weights.items()}
        
    return weights


def simulate_m1(
    frames: Mapping[str, pd.DataFrame],
    symbols: Sequence[str] = DEFAULT_M1_UNIVERSE,
    initial_cash: float = 10000.0,
    fee_rate: float = 0.0005,  # 0.05% slippage/commission (XTB_USD profile)
    sma_months: int = 10,
    vol_window_days: int = 60,
    max_asset_weight: float = 0.30,
    risk_free_rate: float = 0.02,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
) -> dict[str, Any]:
    """Run full monthly rebalancing simulation of M1 Time Series Momentum."""
    universe = [s.upper() for s in symbols if s.upper() in frames]
    if not universe:
        raise ValueError("No valid symbol frames provided for M1 simulation.")
    
    # Build unified calendar of all dates
    all_dates_set = set()
    for s in universe:
        all_dates_set.update(frames[s]["date"])
    all_dates = sorted(all_dates_set)
    if start_date:
        all_dates = [d for d in all_dates if d >= start_date]
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]
        
    if len(all_dates) < 250:
        raise ValueError("Insufficient history for M1 simulation (need >= 250 bars).")
    
    # Precompute monthly SMAs and price lookup tables: {date: {symbol: adjclose}}
    eom_signals: dict[str, pd.DataFrame] = {}
    price_by_date: dict[datetime.date, dict[str, float]] = {}
    open_by_date: dict[datetime.date, dict[str, float]] = {}
    
    for s in universe:
        eom_signals[s] = compute_monthly_sma(frames[s], window_months=sma_months)
        for _, row in frames[s].iterrows():
            d = row["date"]
            if d not in price_by_date:
                price_by_date[d] = {}
                open_by_date[d] = {}
            c = float(row["close"] or 0.0)
            adj_c = float(row["adjclose"] or c)
            o = float(row["open"] or c)
            adj_factor = (adj_c / c) if c > 0 else 1.0
            price_by_date[d][s] = adj_c
            open_by_date[d][s] = o * adj_factor
            
    # Map each EOM date to its trend status
    eom_status_by_date: dict[datetime.date, dict[str, bool]] = {}
    all_eom_dates = set()
    for s in universe:
        for _, row in eom_signals[s].iterrows():
            d = row["date"]
            all_eom_dates.add(d)
            if d not in eom_status_by_date:
                eom_status_by_date[d] = {}
            eom_status_by_date[d][s] = bool(row["trend_long"])
            
    sorted_eoms = sorted(d for d in all_eom_dates if d in all_dates)
    eom_to_next_open_date: dict[datetime.date, datetime.date] = {}
    for i, eom_d in enumerate(sorted_eoms):
        # The next available trading session after eom_d
        future_dates = [d for d in all_dates if d > eom_d]
        if future_dates:
            eom_to_next_open_date[eom_d] = future_dates[0]
            
    rebalance_dates_set = set(eom_to_next_open_date.values())
    
    # State tracking
    cash = float(initial_cash)
    shares: dict[str, float] = {s: 0.0 for s in universe}
    current_weights: dict[str, float] = {s: 0.0 for s in universe}
    
    daily_records = []
    rebalance_records = []
    prev_date = None
    
    # Daily stepping
    for d in all_dates:
        # 1. Check if today is a rebalance day (Open of first session of month)
        if d in rebalance_dates_set:
            # Find the corresponding EOM date
            prev_eom = max(eom for eom, next_d in eom_to_next_open_date.items() if next_d == d)
            
            # Determine which assets are in trend as of prev_eom
            active_symbols = [
                s for s in universe 
                if eom_status_by_date.get(prev_eom, {}).get(s, False) and s in price_by_date.get(prev_eom, {})
            ]
            
            # Calculate volatilities as of prev_eom
            vols = {s: compute_realized_volatility(frames[s], prev_eom, window_days=vol_window_days) for s in active_symbols}
            target_weights = calculate_inverse_vol_weights(active_symbols, vols, max_weight=max_asset_weight)
            
            # Portfolio value before rebalance using Open prices
            portfolio_val_pre = cash
            for s, sh in shares.items():
                px_open = open_by_date.get(d, {}).get(s) or price_by_date.get(d, {}).get(s) or 0.0
                portfolio_val_pre += sh * px_open
                
            # Compute dollar adjustments
            target_dollars = {s: portfolio_val_pre * target_weights.get(s, 0.0) for s in universe}
            current_dollars = {s: shares[s] * (open_by_date.get(d, {}).get(s) or price_by_date.get(d, {}).get(s) or 0.0) for s in universe}
            
            turnover_dollars = sum(abs(target_dollars[s] - current_dollars[s]) for s in universe)
            cost = turnover_dollars * fee_rate
            portfolio_val_post = max(portfolio_val_pre - cost, 0.0)
            
            # Execute new share allocation
            new_shares = {}
            for s in universe:
                px_open = open_by_date.get(d, {}).get(s) or price_by_date.get(d, {}).get(s) or 1.0
                target_alloc = portfolio_val_post * target_weights.get(s, 0.0)
                new_shares[s] = target_alloc / px_open if px_open > 0 else 0.0
                
            allocated_dollars = sum(new_shares[s] * (open_by_date.get(d, {}).get(s) or price_by_date.get(d, {}).get(s) or 0.0) for s in universe)
            cash = max(portfolio_val_post - allocated_dollars, 0.0)
            shares = new_shares
            current_weights = {s: target_weights.get(s, 0.0) for s in universe}
            
            rebalance_records.append({
                "date": str(d),
                "eom_signal_date": str(prev_eom),
                "active_symbols": active_symbols,
                "target_weights": {s: round(target_weights.get(s, 0.0), 4) for s in universe},
                "cash_weight": round(1.0 - sum(target_weights.values()), 4),
                "turnover_pct": round(turnover_dollars / portfolio_val_pre * 100, 2) if portfolio_val_pre > 0 else 0.0,
                "cost": round(cost, 2),
                "portfolio_value": round(portfolio_val_post, 2),
            })
            
        # 2. Mark-to-Market at Close of day d
        # Cash accrues interest (risk-free rate)
        if prev_date is not None:
            days_passed = (d - prev_date).days
            if days_passed > 0 and cash > 0 and risk_free_rate > 0:
                cash *= (1.0 + risk_free_rate * (days_passed / 365.25))
                
        positions_val = sum(shares[s] * price_by_date.get(d, {}).get(s, 0.0) for s in universe)
        equity = cash + positions_val
        
        daily_records.append({
            "date": d,
            "equity": equity,
            "cash": cash,
            "positions_val": positions_val,
        })
        prev_date = d

    # Analyze performance metrics
    eq_df = pd.DataFrame(daily_records)
    eq_df["daily_ret"] = eq_df["equity"].pct_change().fillna(0.0)
    
    start_eq = float(eq_df["equity"].iloc[0])
    end_eq = float(eq_df["equity"].iloc[-1])
    total_ret = (end_eq / start_eq) - 1.0
    
    n_days = (eq_df["date"].iloc[-1] - eq_df["date"].iloc[0]).days
    years = max(n_days / 365.25, 0.1)
    cagr = ((end_eq / start_eq) ** (1.0 / years)) - 1.0 if end_eq > 0 else -1.0
    
    # Drawdown
    eq_df["cummax"] = eq_df["equity"].cummax()
    eq_df["drawdown"] = (eq_df["equity"] - eq_df["cummax"]) / eq_df["cummax"]
    max_dd = float(eq_df["drawdown"].min())
    
    # Sharpe & Sortino (annualized, 252 sessions)
    mean_daily = float(eq_df["daily_ret"].mean())
    std_daily = float(eq_df["daily_ret"].std()) if float(eq_df["daily_ret"].std()) > 0 else 1e-6
    sharpe_rf0 = (mean_daily / std_daily) * math.sqrt(252)
    
    downside_rets = eq_df["daily_ret"][eq_df["daily_ret"] < 0]
    std_downside = float(downside_rets.std()) if len(downside_rets) > 0 and float(downside_rets.std()) > 0 else std_daily
    sortino = (mean_daily / std_downside) * math.sqrt(252)
    
    # Annual breakdown
    eq_df["year"] = pd.to_datetime(eq_df["date"]).dt.year
    annual_rets = {}
    prev_end = float(initial_cash)
    for yr, grp in eq_df.groupby("year"):
        yr_end = float(grp["equity"].iloc[-1])
        annual_rets[int(yr)] = round(((yr_end / prev_end) - 1.0) * 100, 2)
        prev_end = yr_end
        
    # Benchmark SPY Buy & Hold comparison
    spy_df = frames.get("SPY")
    spy_cagr = None
    spy_mdd = None
    spy_sharpe = None
    if spy_df is not None:
        sub_spy = spy_df[(spy_df["date"] >= all_dates[0]) & (spy_df["date"] <= all_dates[-1])].copy()
        if not sub_spy.empty:
            spy_start = float(sub_spy["adjclose"].iloc[0])
            spy_end = float(sub_spy["adjclose"].iloc[-1])
            spy_cagr = ((spy_end / spy_start) ** (1.0 / years)) - 1.0
            sub_spy["cummax"] = sub_spy["adjclose"].cummax()
            sub_spy["dd"] = (sub_spy["adjclose"] - sub_spy["cummax"]) / sub_spy["cummax"]
            spy_mdd = float(sub_spy["dd"].min())
            spy_daily_ret = sub_spy["adjclose"].pct_change().dropna()
            spy_sharpe = (float(spy_daily_ret.mean()) / max(float(spy_daily_ret.std()), 1e-6)) * math.sqrt(252)

    return {
        "start_date": str(all_dates[0]),
        "end_date": str(all_dates[-1]),
        "years": round(years, 2),
        "initial_cash": initial_cash,
        "final_equity": round(end_eq, 2),
        "total_return_pct": round(total_ret * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe_rf0": round(sharpe_rf0, 3),
        "sortino": round(sortino, 3),
        "annual_returns": annual_rets,
        "spy_benchmark": {
            "cagr_pct": round(spy_cagr * 100, 2) if spy_cagr is not None else None,
            "max_drawdown_pct": round(spy_mdd * 100, 2) if spy_mdd is not None else None,
            "sharpe": round(spy_sharpe, 3) if spy_sharpe is not None else None,
        },
        "rebalances_count": len(rebalance_records),
        "rebalance_log": rebalance_records,
    }
