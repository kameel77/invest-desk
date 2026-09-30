"""Hypothesis lab: test several strategy families with the same discipline.

The walk-forward result for trend+momentum was -2.02% out-of-sample, so any
new idea has to clear the same bar or be discarded. This module makes that
cheap: describe a strategy as a function, hand it the stored candles, and get
the same metrics + walk-forward verdict the first hypothesis got.

Four families to test:
  A. MA crossover, tuned per symbol (the user's first idea)
  B. Commodity / FX sensitivity — trade the stock when its own driver moves
  C. Short-horizon mean reversion (intraday bars)
  D. Short-horizon momentum on liquid movers

Every family is evaluated with the same anchored walk-forward: tune on the
train window, score on bars the tuner never saw.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from . import backtest as bt
from . import db, signals

DAY = 86400


# --- metrics --------------------------------------------------------------

def _perf(returns_pct: list[float], trades: int, label: str) -> dict[str, Any]:
    if not returns_pct:
        return {"label": label, "trades": 0, "error": "brak transakcji"}
    rets = np.array(returns_pct, dtype=float)
    wins = int((rets > 0).sum())
    curve = 10_000.0 * np.cumprod(1 + rets / 100.0)
    peak = np.maximum.accumulate(np.concatenate([[10_000.0], curve]))
    dd = np.concatenate([[10_000.0], curve]) - peak
    sharpe = (rets.mean() / rets.std() * math.sqrt(252)) if rets.std() > 1e-9 else 0.0
    gross_win = float(rets[rets > 0].sum())
    gross_loss = abs(float(rets[rets < 0].sum()))
    # Compounded return scales with trade count, so a strategy firing 2000
    # times and one firing 100 times are NOT comparable on it: a -0.8%
    # expectancy compounds to ~-100% over 2000 trades and to -55% over 100.
    # expectancy_per_trade is the comparable number; compounded_return_100 is
    # the like-for-like figure against a 100-trade baseline.
    expectancy = float(rets.mean())
    compounded_100 = ((1 + expectancy / 100.0) ** 100 - 1) * 100 if expectancy > -100 else -100.0
    return {
        "label": label,
        "trades": trades,
        "win_rate_pct": round(wins / len(rets) * 100, 1),
        "total_return_pct": round((curve[-1] / 10_000.0 - 1) * 100, 2),
        "expectancy_per_trade": round(expectancy, 3),
        "compounded_100trades_pct": round(compounded_100, 1),
        "sharpe": round(float(sharpe), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 1e-9 else None,
        "max_drawdown_pct": round(float(dd.min() / max(peak.max(), 1e-9) * 100), 2),
        "expectancy_pct": round(float(rets.mean()), 3),
        "avg_win_pct": round(float(rets[rets > 0].mean()), 2) if wins else 0.0,
        "avg_loss_pct": round(float(rets[rets < 0].mean()), 2) if wins < len(rets) else 0.0,
    }


# --- A. per-symbol MA crossover -------------------------------------------

def _ma_crossover(df: pd.DataFrame, fast: int, slow: int, hold: int,
                  stop: float, target: float) -> list[float]:
    """Buy when the fast MA crosses above the slow one; exit on cross-down,
    stop, target or time. Returns per-trade percent, in signal order."""
    if len(df) < slow + hold + 5:
        return []
    close = df["close"].astype(float)
    fast_ma = close.rolling(fast).mean()
    slow_ma = close.rolling(slow).mean()
    atr = (df["high"] - df["low"]).rolling(14).mean()
    vol = (df["volume"].astype(float)).rolling(20).mean()

    out: list[float] = []
    i = slow + 1
    n = len(df)
    while i < n - 1:
        if pd.isna(fast_ma.iloc[i]) or pd.isna(slow_ma.iloc[i]):
            i += 1
            continue
        crossed_up = fast_ma.iloc[i] > slow_ma.iloc[i] and fast_ma.iloc[i - 1] <= slow_ma.iloc[i - 1]
        if not crossed_up:
            i += 1
            continue
        # Require real liquidity — a thin name's "crossover" is noise.
        v = vol.iloc[i]
        if pd.isna(v) or v <= 0:
            i += 1
            continue
        entry = float(df["open"].iloc[i + 1]) * 1.001
        a = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else entry * 0.02
        sl, tp = entry - stop * a, entry + target * a
        exit_px, held = None, 0
        for j in range(i + 1, min(n, i + 1 + hold + 1)):
            if float(df["low"].iloc[j]) <= sl:
                exit_px, held = sl, j - i
                break
            if float(df["high"].iloc[j]) >= tp:
                exit_px, held = tp, j - i
                break
        if exit_px is None:
            j = min(n - 1, i + hold)
            if j <= i:
                i += 1
                continue
            exit_px, held = float(df["close"].iloc[j]), j - i
        cost = 0.35 + 0.05 * 2  # GPW commission + slippage, roughly, in %
        out.append((exit_px / entry - 1) * 100 - cost)
        i = j + 1
    return out


# --- B. commodity / FX sensitivity ----------------------------------------

def _driver_sensitivity(df: pd.DataFrame, drivers: dict[str, pd.Series],
                        lookback: int, min_corr: float,
                        hold: int) -> list[float]:
    """Enter when a driver (gold, oil, USDPLN...) moved in the same direction
    as the stock over the lookback — i.e. the stock is tracking its own input.
    Exit on the opposite driver move, a stop, or time."""
    if len(df) < lookback + hold + 10:
        return []
    stock_ret = df["close"].astype(float).pct_change(lookback)
    # Everything below is indexed by the bar's timestamp. df.index is a
    # RangeIndex, so mixing it with a timestamp-indexed driver makes the
    # concat produce an empty frame and the whole family returns zero trades.
    stamp = pd.Index(df["ts"].values) if "ts" in df.columns else df.index
    stock_ret = pd.Series(stock_ret.values, index=stamp)
    best_corr = pd.Series(0.0, index=stamp)
    for name, drv in drivers.items():
        aligned = drv.reindex(drv.index.union(stamp)).ffill().reindex(stamp)
        if aligned.isna().all():
            continue
        drv_ret = aligned.pct_change(lookback)
        pair = pd.concat([stock_ret, drv_ret], axis=1).dropna()
        if len(pair) < lookback + 10:
            continue
        rolling_corr = pair[0].rolling(lookback, min_periods=max(20, lookback // 2)).corr(pair[1])
        # pair is a dropna() subset, so rolling_corr is indexed by only those
        # timestamps. Reindex onto the full bar index before comparing, or
        # pandas refuses: "Can only compare identically-labeled Series".
        rolling_corr = rolling_corr.reindex(stamp)
        # ``a.where(cond, b)`` keeps ``a`` where cond is True and takes ``b``
        # otherwise. To keep the STRONGEST correlation seen so far, the new
        # value must be the receiver (so it wins where it is stronger) and the
        # running best the fallback. The reverse silently yields all-zeros.
        best_corr = rolling_corr.where(rolling_corr.abs() > best_corr.abs(), best_corr)
    if best_corr.abs().max() < min_corr:
        return []

    out: list[float] = []
    n = len(df)
    atr = (df["high"] - df["low"]).rolling(14).mean()
    i = lookback + 1
    while i < n - 1:
        c = best_corr.iloc[i]
        r = stock_ret.iloc[i]
        if pd.isna(c) or abs(c) < min_corr or pd.isna(r) or abs(r) < 0.01:
            i += 1
            continue
        entry = float(df["open"].iloc[i + 1]) * 1.001
        a = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else entry * 0.02
        sl, tp = entry - 2.5 * a, entry + 3.0 * a
        exit_px, held = None, 0
        for j in range(i + 1, min(n, i + 1 + hold + 1)):
            if float(df["low"].iloc[j]) <= sl:
                exit_px, held = sl, j - i
                break
            if float(df["high"].iloc[j]) >= tp:
                exit_px, held = tp, j - i
                break
        if exit_px is None:
            j = min(n - 1, i + hold)
            if j <= i:
                i += 1
                continue
            exit_px, held = float(df["close"].iloc[j]), j - i
        out.append((exit_px / entry - 1) * 100 - 0.4)
        i = j + 1
    return out


# --- C. mean reversion on a short horizon ---------------------------------

def _mean_reversion(df: pd.DataFrame, lookback: int, z_entry: float,
                    hold: int) -> list[float]:
    """Buy oversold (close far below its own moving average), exit on the mean
    or a time limit. The mirror of the trend system that just failed."""
    if len(df) < lookback + hold + 10:
        return []
    close = df["close"].astype(float)
    ma = close.rolling(lookback).mean()
    sd = close.rolling(lookback).std()
    z = (close - ma) / sd.replace(0, np.nan)
    out: list[float] = []
    n = len(df)
    i = lookback
    while i < n - 1:
        zi = z.iloc[i]
        if pd.isna(zi) or zi > -z_entry:
            i += 1
            continue
        entry = float(df["open"].iloc[i + 1]) * 1.001
        exit_px, j = None, min(n - 1, i + hold)
        for k in range(i + 1, min(n, i + 1 + hold + 1)):
            # Exit when price actually returns to the mean (z >= 0). The
            # previous `or not pd.isna(...)` made the condition true on the
            # very next bar, so every trade lasted one bar.
            if not pd.isna(z.iloc[k]) and z.iloc[k] >= 0:
                exit_px, j = float(df["close"].iloc[k]), k
                break
        if exit_px is None:
            exit_px, j = float(df["close"].iloc[j]), j
        if j <= i:
            i += 1
            continue
        out.append((exit_px / entry - 1) * 100 - 0.4)
        i = j + 1
    return out


# --- D. short-horizon momentum on liquid movers ---------------------------

def _momentum_short(df: pd.DataFrame, lookback: int, hold: int,
                    min_move: float) -> list[float]:
    """Buy names that just moved hard and kept going; exit on the move fading."""
    if len(df) < lookback + hold + 10:
        return []
    close = df["close"].astype(float)
    mom = close.pct_change(lookback) * 100
    out: list[float] = []
    n = len(df)
    atr = (df["high"] - df["low"]).rolling(14).mean()
    i = lookback
    while i < n - 1:
        m = mom.iloc[i]
        if pd.isna(m) or abs(m) < min_move:
            i += 1
            continue
        entry = float(df["open"].iloc[i + 1]) * 1.001
        a = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else entry * 0.02
        sl = entry - 2.0 * a
        exit_px, j = None, min(n - 1, i + hold)
        for k in range(i + 1, min(n, i + 1 + hold + 1)):
            if float(df["low"].iloc[k]) <= sl:
                exit_px, j = sl, k
                break
            if mom.iloc[k] < 0 if k > i else False:
                exit_px, j = float(df["close"].iloc[k]), k
                break
        if exit_px is None:
            exit_px, j = float(df["close"].iloc[j]), j
        if j <= i:
            i += 1
            continue
        out.append((exit_px / entry - 1) * 100 - 0.4)
        i = j + 1
    return out


# --- universe selection ---------------------------------------------------

def liquid_movers(symbols: list[str], min_adv: Optional[float] = None,
                  lookback: int = 60) -> list[tuple[str, float, float]]:
    """The user's first criterion: high liquidity AND a significant move.

    Returns ``(symbol, adv_in_currency, move_pct)`` sorted by move, so the
    "liquidation / eruption" names come first.
    """
    out: list[tuple[str, float, float]] = []
    for s in symbols:
        raw = signals.load_frame(s, limit=1000)
        if raw is None or len(raw) < lookback + 60:
            continue
        df = signals.compute_indicators(raw)
        row = df.iloc[-1]
        adv = float(row["adv"]) if not pd.isna(row["adv"]) else 0.0
        move = float(row["roc_fast"]) if not pd.isna(row["roc_fast"]) else 0.0
        floor = min_adv if min_adv is not None else (
            signals.MIN_ADV_PLN if s.endswith((".WA", ".PL")) else signals.MIN_ADV_USD)
        if adv >= floor and abs(move) >= 3.0:
            out.append((s, adv, move))
    out.sort(key=lambda r: -abs(r[2]))
    return out


def driver_series() -> dict[str, pd.Series]:
    """Commodity and FX close series, indexed by timestamp."""
    out: dict[str, pd.Series] = {}
    for sym in ("GC=F", "CL=F", "HG=F", "NG=F", "EURPLN=X", "USDPLN=X", "GBPPLN=X", "CHFPLN=X"):
        rows = db.get_candles(sym, limit=3000)
        if rows:
            s = pd.Series([float(r["close"]) for r in rows],
                          index=pd.Index([r["ts"] for r in rows]))
            out[sym] = s[~s.index.duplicated(keep="last")].sort_index()
    return out


# --- evaluation -----------------------------------------------------------

def evaluate(name: str, fn: Callable[[pd.DataFrame], list[float]],
             symbols: list[str], params: dict[str, Any],
             start_ts: Optional[int] = None,
             end_ts: Optional[int] = None,
             extra: Optional[dict[str, Any]] = None) -> list[float]:
    """Run ``fn`` per symbol over the (optional) window and pool the returns."""
    call_params = dict(params)
    if extra:
        call_params.update(extra)
    pooled: list[float] = []
    for s in symbols:
        df = bt._cached_frame(s)
        if df is None or len(df) < 80:
            continue
        if start_ts is not None or end_ts is not None:
            ts = df["ts"].values
            mask = np.ones(len(df), dtype=bool)
            if start_ts is not None:
                mask &= ts >= start_ts
            if end_ts is not None:
                mask &= ts < end_ts
            if mask.sum() < 60:
                continue
            df = df[mask].reset_index(drop=True)
        try:
            pooled.extend(fn(df, **call_params))
        except Exception:
            continue
    return pooled


_ = time  # timing is reported by the callers
