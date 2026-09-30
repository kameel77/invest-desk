"""Backtest the signal engine over stored candles.

The point is not to produce a pretty equity curve — it is to answer "does
this configuration make money, and which knob is responsible", so the module
runs a parameter grid and reports what each variation actually did.

Method
------
* Walks the candles forward bar by bar, computing indicators on the SLICE
  available at each step. No look-ahead: the signal at bar *i* uses only bars
  ``0..i``.
* Fills at the next bar's open (realistic for a daily swing system — you
  cannot trade at the close that generated the signal).
* Applies the same commission, slippage and ATR stop/target as the live
  paper engine, so a backtest result and a paper result mean the same thing.
* Reports per-trade returns, hit rate, expectancy, profit factor, max
  drawdown and a Sharpe-like ratio on daily returns.

Everything is deterministic: same candles + same params = same result.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import config, db, signals
from .context import build_context
from .context import context_at as ctx_at
from .context import score_context


@dataclass
class BacktestParams:
    buy_threshold: float = signals.BUY_THRESHOLD
    sell_threshold: float = signals.SELL_THRESHOLD
    stop_atr: float = 2.0
    target_atr: float = 3.0
    max_hold_days: int = 20
    weights: dict[str, float] = field(default_factory=lambda: dict(signals.WEIGHTS))
    min_adv: Optional[float] = None
    commission_pct: float = config.COMMISSION_PCT
    commission_min: float = config.COMMISSION_MIN
    slippage_pct: float = config.SLIPPAGE_PCT
    use_reversion_tilt: bool = True
    use_liquidity_gate: bool = True
    # --- market context (backfilled by walk-forward tuning) ---
    require_up_regime: bool = False
    min_rs: float = 0.0
    vix_max: Optional[float] = None

    def key(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass
class Trade:
    symbol: str
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    exit_reason: str
    score: float
    bars_held: int
    pnl_pct: float
    pnl_cash: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fees(notional: float, p: BacktestParams) -> float:
    return max(notional * p.commission_pct, p.commission_min) if notional > 0 else 0.0


def _metrics(equity: list[float], trades: list[Trade]) -> dict[str, Any]:
    if not equity:
        return {"error": "brak kapitału"}
    eq = np.array(equity, dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    max_dd = float(dd.min())
    max_dd_pct = float((dd / np.maximum(peak, 1e-9)).min() * 100)

    rets = np.diff(eq) / np.maximum(eq[:-1], 1e-9)
    mean_r = float(rets.mean()) if len(rets) else 0.0
    std_r = float(rets.std()) if len(rets) > 1 else 0.0
    sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 1e-12 else 0.0

    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    gross_win = sum(t.pnl_cash for t in wins)
    gross_loss = abs(sum(t.pnl_cash for t in losses))
    pnls = [t.pnl_pct for t in trades]

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "hit_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "total_return_pct": round((eq[-1] / eq[0] - 1) * 100, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "sharpe": round(sharpe, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy_pct": round(float(np.mean(pnls)), 3) if pnls else 0.0,
        "avg_win_pct": round(float(np.mean([t.pnl_pct for t in wins])), 2) if wins else 0.0,
        "avg_loss_pct": round(float(np.mean([t.pnl_pct for t in losses])), 2) if losses else 0.0,
        "avg_bars_held": round(float(np.mean([t.bars_held for t in trades])), 1) if trades else 0.0,
        "final_equity": round(float(eq[-1]), 2),
        "exit_reasons": _count_by(trades, "exit_reason"),
    }


def _count_by(trades: list[Trade], attr: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in trades:
        out[getattr(t, attr)] = out.get(getattr(t, attr), 0) + 1
    return out


def _score_at(df: pd.DataFrame, i: int, p: BacktestParams) -> Optional[float]:
    """Composite score at bar ``i`` using only bars ``0..i``."""
    if i < signals.SMA_FAST:
        return None
    row = df.iloc[i]
    if pd.isna(row["atr"]) or row["atr"] <= 0:
        return None

    w = dict(p.weights)
    if not p.use_reversion_tilt:
        w["reversion"] = 0.0
    wsum = w["trend"] + w["momentum"] + w["reversion"]
    if wsum <= 0:
        return None

    trend_s, _ = signals._score_trend(row)
    mom_s, _ = signals._score_momentum(df, i)
    rev_s = signals._score_reversion(df, i)[0] if p.use_reversion_tilt else 0.0
    return float((trend_s * w["trend"] + mom_s * w["momentum"] + rev_s * w["reversion"]) / wsum * 100.0)


_FRAME_CACHE: dict[tuple, Any] = {}
_FRAME_CACHE_MAX = 240


def _cached_frame(symbol: str) -> Optional[pd.DataFrame]:
    """Indicator frame for ``symbol``, memoised in-process.

    ``compute_indicators`` is the hot path in a parameter grid: walk-forward
    re-walks the same ~50 symbols nine times per fold, and the frame depends
    only on the symbol (and on the weight-free indicator set), never on the
    parameters being tested. A small bounded cache removes most of the cost.
    """
    hit = _FRAME_CACHE.get(symbol)
    if hit is not None:
        return hit.copy()
    df = signals.load_frame(symbol, limit=1000)
    if df is None:
        return None
    df = signals.compute_indicators(df)
    if len(_FRAME_CACHE) >= _FRAME_CACHE_MAX:
        _FRAME_CACHE.clear()
    _FRAME_CACHE[symbol] = df
    return df.copy()


def clear_frame_cache() -> None:
    _FRAME_CACHE.clear()


def backtest_symbol(symbol: str, p: Optional[BacktestParams] = None,
                    start_cash: float = 10_000.0) -> dict[str, Any]:
    """Walk one symbol's stored candles forward and simulate the strategy."""
    p = p or BacktestParams()
    df = _cached_frame(symbol)
    if df is None:
        return {"symbol": symbol, "error": "brak historii"}
    if len(df) < signals.SMA_FAST + 20:
        return {"symbol": symbol, "error": f"za mało świec ({len(df)})"}
    ctx = build_context(df, symbol)

    cash = float(start_cash)
    position: Optional[dict[str, Any]] = None
    trades: list[Trade] = []
    equity: list[float] = []
    equity_ts: list[int] = []
    entry_log: list[dict[str, Any]] = []

    n = len(df)
    for i in range(n):
        row = df.iloc[i]

        # 1) Manage an open position first: stop, target, or time exit.
        if position is not None:
            reason = None
            if row["low"] <= position["stop"]:
                reason = "stop"
            elif row["high"] >= position["target"]:
                reason = "target"
            elif (i - position["entry_i"]) >= p.max_hold_days:
                reason = "timeout"
            # Exits fill at the level touched, not the close — a stop is not
            # a close, and using the close would flatter the backtest.
            if reason:
                if reason == "stop":
                    px = position["stop"] * (1 - p.slippage_pct)
                elif reason == "target":
                    px = position["target"] * (1 - p.slippage_pct)
                else:
                    px = float(row["close"]) * (1 - p.slippage_pct)
                notional = px * position["qty"]
                fee = _fees(notional, p)
                cash += notional - fee
                pnl_pct = (px / position["entry_price"] - 1) * 100
                trades.append(Trade(
                    symbol=symbol, entry_ts=position["entry_ts"], entry_price=position["entry_price"],
                    exit_ts=int(row["ts"]), exit_price=px, exit_reason=reason,
                    score=position["score"], bars_held=i - position["entry_i"],
                    pnl_pct=round(pnl_pct, 3), pnl_cash=round(px * position["qty"] - fee - position["cost"], 2),
                ))
                position = None

        # 2) Look for an entry, filled at the NEXT bar's open.
        if position is None and i < n - 1:
            score = _score_at(df, i, p)
            size_factor = 1.0
            if score is not None and score < p.buy_threshold:
                score = None
            if score is not None and p.use_liquidity_gate:
                adv = row["adv"]
                floor = p.min_adv if p.min_adv is not None else (
                    signals.MIN_ADV_PLN if symbol.endswith((".WA", ".PL")) else signals.MIN_ADV_USD)
                if pd.isna(adv) or adv < floor:
                    score = None
            if score is not None:
                # Context gate: the stock's own score says nothing about the
                # market it lives in, so the regime/RS/VIX read can block a
                # long entry and can shrink the size of the ones it allows.
                allowed, ctx_reasons, size_factor = score_context(
                    ctx_at(ctx, i), min_rs=p.min_rs,
                    require_up_regime=p.require_up_regime, vix_max=p.vix_max)
                if not allowed:
                    entry_log.append({"ts": int(row["ts"]), "score": round(score, 1),
                                      "blocked": ctx_reasons})
                    score = None
            if score is not None:
                nxt = df.iloc[i + 1]
                px = float(nxt["open"]) * (1 + p.slippage_pct)
                qty = _size(row, score, cash, p, size_factor)
                if qty > 0:
                    notional = px * qty
                    fee = _fees(notional, p)
                    if notional + fee <= cash:
                        cash -= notional + fee
                        atr = float(row["atr"])
                        position = {
                            "entry_i": i + 1, "entry_ts": int(nxt["ts"]),
                            "entry_price": px, "qty": qty, "score": score,
                            "cost": notional + fee,
                            "stop": px - p.stop_atr * atr,
                            "target": px + p.target_atr * atr,
                            }

        equity.append(cash + (position["qty"] * float(row["close"]) if position else 0.0))
        equity_ts.append(int(row["ts"]))

    # Close any position still open at the end so the return is realised.
    if position is not None:
        last = float(df.iloc[-1]["close"])
        notional = last * position["qty"]
        fee = _fees(notional, p)
        cash += notional - fee
        equity[-1] = cash
        trades.append(Trade(
            symbol=symbol, entry_ts=position["entry_ts"], entry_price=position["entry_price"],
            exit_ts=int(df.iloc[-1]["ts"]), exit_price=last, exit_reason="end_of_data",
            score=position["score"], bars_held=len(df) - 1 - position["entry_i"],
            pnl_pct=round((last / position["entry_price"] - 1) * 100, 3),
            pnl_cash=round(notional - fee - position["cost"], 2),
        ))

    out = _metrics(equity, trades)
    blocked_reasons: dict[str, int] = {}
    for e in entry_log:
        for r in e.get("blocked", []):
            key = re.sub(r"[-+]?\d+\.?\d*", "", r).split("(")[0].strip()[:40]
            blocked_reasons[key] = blocked_reasons.get(key, 0) + 1
    out.update({
        "symbol": symbol,
        "bars": n,
        "from_ts": equity_ts[0] if equity_ts else None,
        "to_ts": equity_ts[-1] if equity_ts else None,
        "params": asdict(p),
        "context_available": bool(ctx.get("available")),
        "blocked_entries": len(entry_log),
        "blocked_reasons": blocked_reasons,
        "trades_detail": [t.as_dict() for t in trades],
    })
    return out


def _size(row: pd.Series, score: float, cash: float, p: BacktestParams,
          size_factor: float = 1.0) -> float:
    """Volatility-targeted sizing, same shape as the live engine."""
    price = float(row["close"])
    atr = float(row["atr"]) if not pd.isna(row["atr"]) else price * 0.02
    risk_per_share = max(atr * p.stop_atr, price * 0.005, 0.01)
    # Stronger signal -> larger position, capped at 1.5x the base risk.
    conviction = 1.0 + min(abs(score) / 100.0, 0.5)
    budget = cash * 0.005 * conviction
    # size_factor comes from the macro dial (VIX caution -> smaller position).
    qty = size_factor * min(budget / risk_per_share, (cash * 0.15) / max(price, 0.01))
    notional = qty * price
    return 0.0 if notional < config.MIN_TICKET else qty


def backtest_many(symbols: list[str], p: Optional[BacktestParams] = None,
                  start_cash: float = 10_000.0) -> dict[str, Any]:
    """Per-symbol results plus a portfolio view (equal cash per symbol)."""
    p = p or BacktestParams()
    results = [backtest_symbol(s, p, start_cash) for s in symbols]
    good = [r for r in results if "error" not in r]

    all_trades = [t for r in good for t in r["trades_detail"]]
    equity = [start_cash]
    for t in all_trades:
        equity.append(equity[-1] + t["pnl_cash"])
    portfolio = _metrics(equity, [Trade(**{**t}) for t in all_trades]) if all_trades else {}

    ranked = sorted(good, key=lambda r: r.get("total_return_pct", -999), reverse=True)
    return {
        "params": asdict(p),
        "symbols_tested": len(symbols),
        "symbols_with_trades": len(good),
        "skipped": [{"symbol": r["symbol"], "error": r["error"]}
                    for r in results if "error" in r],
        "portfolio": portfolio,
        "by_symbol": [
            {k: v for k, v in r.items() if k != "trades_detail"} for r in ranked
        ],
        "best": ranked[0]["symbol"] if ranked else None,
        "worst": ranked[-1]["symbol"] if ranked else None,
    }


# --- parameter grid -------------------------------------------------------

def sweep(symbols: list[str], variants: Optional[list[BacktestParams]] = None) -> dict[str, Any]:
    """Run a grid of parameter sets and rank them.

    This is the tool the user actually asked for: "how do I tune the model" —
    the answer is a table, not a hunch.
    """
    variants = variants or _default_variants()
    results = []
    for p in variants:
        r = backtest_many(symbols, p)
        pf = r.get("portfolio") or {}
        results.append({
            "label": _label(p),
            "buy_threshold": p.buy_threshold,
            "sell_threshold": p.sell_threshold,
            "stop_atr": p.stop_atr,
            "target_atr": p.target_atr,
            "max_hold_days": p.max_hold_days,
            "reversion": p.use_reversion_tilt,
            "trades": pf.get("trades", 0),
            "total_return_pct": pf.get("total_return_pct", 0),
            "hit_rate_pct": pf.get("hit_rate_pct", 0),
            "profit_factor": pf.get("profit_factor"),
            "max_drawdown_pct": pf.get("max_drawdown_pct", 0),
            "sharpe": pf.get("sharpe", 0),
            "expectancy_pct": pf.get("expectancy_pct", 0),
        })
    results.sort(key=lambda r: r["total_return_pct"], reverse=True)
    return {"symbols": len(symbols), "variants": results, "ts": int(time.time())}


def _label(p: BacktestParams) -> str:
    return (f"wej≥{p.buy_threshold:.0f} wyj≤{p.sell_threshold:.0f} "
            f"SL{p.stop_atr:.1f} TP{p.target_atr:.1f} H{p.max_hold_days}"
            f"{'' if p.use_reversion_tilt else ' bez-rev'}")


def _default_variants() -> list[BacktestParams]:
    """A focused grid: entry threshold, exit rule, stop/target geometry."""
    out: list[BacktestParams] = []

    # 1) Threshold sweep on the current configuration.
    for buy in (15, 25, 35, 45, 55):
        out.append(BacktestParams(buy_threshold=buy, sell_threshold=-25))

    # 2) Stop / target geometry at the current threshold.
    for stop, target in ((1.5, 2.0), (2.0, 3.0), (2.0, 4.0), (2.5, 3.0), (3.0, 4.5)):
        out.append(BacktestParams(buy_threshold=35, stop_atr=stop, target_atr=target))

    # 3) Holding period.
    for hold in (5, 10, 20, 40):
        out.append(BacktestParams(buy_threshold=35, max_hold_days=hold))

    # 4) Is the reversion tilt helping or hurting?
    out.append(BacktestParams(buy_threshold=35, use_reversion_tilt=False))

    # 5) Weight mixes: how much does each leg actually contribute?
    out.append(BacktestParams(buy_threshold=35, weights={"trend": 1.0, "momentum": 0.0, "reversion": 0.0}))
    out.append(BacktestParams(buy_threshold=35, weights={"trend": 0.0, "momentum": 1.0, "reversion": 0.0}))
    out.append(BacktestParams(buy_threshold=35, weights={"trend": 0.2, "momentum": 0.6, "reversion": 0.2}))
    out.append(BacktestParams(buy_threshold=35, weights={"trend": 0.6, "momentum": 0.2, "reversion": 0.2}))

    return out
