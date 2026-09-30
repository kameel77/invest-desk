"""Walk-forward validation — the honest answer to "does this strategy work?".

The parameter sweep so far ranked configurations on the SAME bars it then
scored them on, which is why its winner (+5.38%) means almost nothing: with
19 variants and ~1200 bars, some configuration will look good by chance.

Walk-forward removes that circularity:

    for each fold:
        1. TRAIN  on bars [start, cut)      -> pick the best configuration
        2. TEST   on bars [cut, cut + span) -> score ONLY that configuration
        3. the test scores are what get reported

The configuration is chosen without ever seeing the test bars, and the test
bars are never re-used for tuning. A strategy that only works in-sample shows
up here as a train result that collapses on test.

Folds advance forward in time (anchored, no shuffling) because a time series
read backwards is a different — and much easier — problem.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Optional

from . import backtest as bt
from . import db, signals


# The candidate grid. Kept deliberately small: every extra variant multiplies
# the chance that the train winner is noise.
def _candidates() -> list[bt.BacktestParams]:
    base = bt.BacktestParams()
    out: list[bt.BacktestParams] = []

    # Geometry of the trade, context disabled — the "old" strategy.
    for stop, target in ((2.0, 3.0), (2.5, 3.0), (3.0, 4.5)):
        out.append(replace(base, stop_atr=stop, target_atr=target))

    # Same geometry + context gate. Each context variant is a separate
    # hypothesis, so each is tuned and tested independently.
    for stop, target in ((2.5, 3.0), (3.0, 4.5)):
        out.append(replace(base, stop_atr=stop, target_atr=target,
                           require_up_regime=True))
        out.append(replace(base, stop_atr=stop, target_atr=target,
                           min_rs=0.0))
        out.append(replace(base, stop_atr=stop, target_atr=target,
                           require_up_regime=True, min_rs=5.0))
    return out


def _score(r: dict[str, Any]) -> float:
    """Rank configurations on TRAIN only.

    Sharpe primary (return-per-unit-of-risk), profit factor as a tie-breaker,
    and a minimum trade count so a variant cannot win on two lucky trades.
    """
    if not r or r.get("trades", 0) < 8:
        return -1e9
    sharpe = r.get("sharpe") or 0.0
    pf = r.get("profit_factor")
    pf_score = 0.0 if pf is None else min(pf, 3.0) - 1.0
    return round(sharpe * 1.0 + pf_score * 0.3, 4)


def _portfolio(symbols: list[str], p: bt.BacktestParams,
               start_ts: Optional[int] = None, end_ts: Optional[int] = None) -> dict[str, Any]:
    """Backtest a basket, optionally clipped to a timestamp window."""
    results = []
    for s in symbols:
        r = bt.backtest_symbol(s, p)
        if "error" in r:
            continue
        if start_ts is not None or end_ts is not None:
            # Keep only trades that fall inside the fold's window.
            r = dict(r)
            r["trades_detail"] = [
                t for t in r["trades_detail"]
                if (start_ts is None or t["entry_ts"] >= start_ts)
                and (end_ts is None or t["entry_ts"] < end_ts)
            ]
            pf = bt._metrics([], r["trades_detail"]) if r["trades_detail"] else {}
            r = {**r, **pf}
        results.append(r)

    trades = [t for r in results for t in r.get("trades_detail", [])]
    trades.sort(key=lambda t: t["entry_ts"])
    equity = [10_000.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl_cash"])
    metrics = bt._metrics(equity, [bt.Trade(**t) for t in trades]) if trades else {}
    metrics["symbols"] = len(results)
    return metrics


def run(symbols: list[str], folds: int = 5, train_years: float = 2.0,
        test_months: float = 6.0) -> dict[str, Any]:
    """Anchored walk-forward over the shared time span of the universe."""
    started = time.time()
    # Anchor to the newest bar we hold.
    ends: list[int] = []
    for s in symbols[:5]:
        rows = db.get_candles(s, limit=3)
        if rows:
            ends.append(int(rows[-1]["ts"]))
    if not ends:
        return {"error": "brak danych"}
    now = min(ends)

    DAY = 86400
    train_span = int(train_years * 365 * DAY)
    test_span = int(test_months * 30.4 * DAY)

    candidates = _candidates()
    results: list[dict[str, Any]] = []
    chosen_counts: dict[str, int] = {}

    for f in range(folds):
        test_end = now - f * (test_span + 0)  # no gap: test ends at its start of next fold
        test_start = test_end - test_span
        train_end = test_start
        train_start = train_end - train_span
        if train_start <= 0:
            break

        # --- 1. tune on train ---
        scores = []
        for p in candidates:
            m = _portfolio(symbols, p, start_ts=train_start, end_ts=train_end)
            scores.append((_score(m), p, m))
        scores.sort(key=lambda x: -x[0])
        best_score, best_p, best_train = scores[0]
        label = bt._label(best_p) + _ctx_label(best_p)
        chosen_counts[label] = chosen_counts.get(label, 0) + 1

        # --- 2. test that ONE configuration, untouched ---
        test_m = _portfolio(symbols, best_p, start_ts=test_start, end_ts=test_end)

        # --- 3. reference: the untuned default on the same test window ---
        default_m = _portfolio(symbols, bt.BacktestParams(),
                              start_ts=test_start, end_ts=test_end)

        results.append({
            "fold": f + 1,
            "train_start": train_start, "train_end": train_end,
            "test_start": test_start, "test_end": test_end,
            "chosen": label,
            "train": {k: best_train.get(k) for k in
                      ("trades", "total_return_pct", "hit_rate_pct", "sharpe",
                       "profit_factor", "max_drawdown_pct")},
            "test": {k: test_m.get(k) for k in
                     ("trades", "total_return_pct", "hit_rate_pct", "sharpe",
                      "profit_factor", "max_drawdown_pct")},
            "test_default": {k: default_m.get(k) for k in
                             ("trades", "total_return_pct", "hit_rate_pct",
                              "sharpe", "profit_factor", "max_drawdown_pct")},
        })

    # --- aggregate the out-of-sample legs ---
    oos_trades = sum(r["test"].get("trades") or 0 for r in results)
    oos_ret = [r["test"].get("total_return_pct") for r in results
               if r["test"].get("total_return_pct") is not None]
    oos_sharpe = [r["test"].get("sharpe") for r in results
                  if r["test"].get("sharpe") is not None]
    df_ret = [r["test_default"].get("total_return_pct") for r in results
              if r["test_default"].get("total_return_pct") is not None]

    return {
        "symbols": len(symbols),
        "folds": len(results),
        "candidates": len(candidates),
        "results": results,
        "chosen_distribution": chosen_counts,
        "oos": {
            "trades": oos_trades,
            "avg_return_pct": round(sum(oos_ret) / len(oos_ret), 2) if oos_ret else 0.0,
            "profitable_folds": sum(1 for r in oos_ret if r > 0),
            "folds": len(oos_ret),
            "avg_sharpe": round(sum(oos_sharpe) / len(oos_sharpe), 2) if oos_sharpe else 0.0,
        },
        "baseline_oos": {
            "avg_return_pct": round(sum(df_ret) / len(df_ret), 2) if df_ret else 0.0,
            "profitable_folds": sum(1 for r in df_ret if r > 0),
        },
        "duration_s": round(time.time() - started, 1),
        "ts": int(time.time()),
    }


def _ctx_label(p: bt.BacktestParams) -> str:
    bits = []
    if p.require_up_regime:
        bits.append("reżim↑")
    if p.min_rs:
        bits.append(f"RS>{p.min_rs:.0f}")
    if p.vix_max:
        bits.append(f"VIX<{p.vix_max:.0f}")
    return (" +" + "+".join(bits)) if bits else ""


_ = signals  # the parameter grid is expressed in signals' units
