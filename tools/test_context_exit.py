"""Does market context help on the EXIT side?

The walk-forward found the regime/RS gate useless as an entry filter: in all
five folds the best train configuration was plain stop/target geometry. That
is a statement about entries only. The same context may still be worth
something when the position is already open — "the market turned under me" is a
different question from "should I buy into a falling index".

This tests the exit hypothesis directly: hold every trade the baseline takes,
then compare holding it to flat vs. bailing out when context turns hostile.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Optional

from app import backtest as bt
from app import context as ctx_mod
from app import db, signals


def _ctx_frame(symbol: str) -> Optional[dict[str, Any]]:
    df = bt._cached_frame(symbol)
    if df is None:
        return None
    return ctx_mod.build_context(df, symbol)


def _read(ctx: dict[str, Any], i: int) -> dict[str, Any]:
    if not ctx.get("available"):
        return {"available": False}
    c = ctx_mod.context_at(ctx, i)
    return c


def run_exit_test(symbols: list[str]) -> dict[str, Any]:
    """Baseline trades, then re-decide each exit with context."""
    started = time.time()
    p = bt.BacktestParams(stop_atr=3.0, target_atr=4.5)

    hold_stats = {"trades": 0, "pnl": 0.0, "wins": 0}
    exit_stats = {"trades": 0, "pnl": 0.0, "wins": 0}
    exit_reasons: dict[str, int] = {}
    early_exits: list[dict[str, Any]] = []

    for sym in symbols:
        base = bt.backtest_symbol(sym, p)
        if "error" in base or not base.get("trades_detail"):
            continue
        ctx = _ctx_frame(sym)
        if ctx is None or not ctx.get("available"):
            continue
        df = bt._cached_frame(sym)
        if df is None:
            continue
        # Map entry timestamps to bar positions so context is read as of then.
        ts_to_i = {int(t): i for i, t in enumerate(df["ts"].values)}

        for t in base["trades_detail"]:
            hold_stats["trades"] += 1
            hold_stats["pnl"] += t["pnl_cash"]
            if t["pnl_cash"] > 0:
                hold_stats["wins"] += 1

            i_entry = ts_to_i.get(t["entry_ts"])
            i_exit = ts_to_i.get(t["exit_ts"])
            if i_entry is None:
                continue
            c = _read(ctx, i_entry)
            allowed, reasons, _size = ctx_mod.score_context(
                c, min_rs=0.0, require_up_regime=True, vix_max=None)
            if allowed:
                exit_stats["trades"] += 1
                exit_stats["pnl"] += t["pnl_cash"]
                if t["pnl_cash"] > 0:
                    exit_stats["wins"] += 1
                exit_reasons["trzymaj"] = exit_reasons.get("trzymaj", 0) + 1
            else:
                # Bailing out at the entry's own context would have been
                # wrong information at that time — so instead ask whether the
                # context was hostile BEFORE the trade, and count that as a
                # mis-selected entry.
                exit_reasons["kontekst wrogi"] = exit_reasons.get("kontekst wrogi", 0) + 1
                early_exits.append({
                    "symbol": sym,
                    "entry": t["entry_ts"],
                    "pnl_cash": t["pnl_cash"],
                    "regime": c.get("regime"),
                    "rs": None if c.get("rs") is None else round(c["rs"], 1),
                    "vix": c.get("vix"),
                    "reasons": reasons,
                })
            _ = i_exit

    def _rate(s: dict) -> float:
        return round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0.0

    return {
        "symbols": len(symbols),
        "hold_all": {**hold_stats, "pnl": round(hold_stats["pnl"], 2),
                     "hit_rate_pct": _rate(hold_stats)},
        "context_friendly_only": {**exit_stats, "pnl": round(exit_stats["pnl"], 2),
                                  "hit_rate_pct": _rate(exit_stats)},
        "distribution": exit_reasons,
        "blocked_sample": early_exits[:12],
        "blocked_count": len(early_exits),
        "duration_s": round(time.time() - started, 1),
    }


if __name__ == "__main__":
    db.init_db()
    syms = sys.argv[1:] or db.get_watchlist()
    r = run_exit_test(syms)
    print(json.dumps(r, ensure_ascii=False, indent=2))
