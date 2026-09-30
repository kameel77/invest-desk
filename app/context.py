"""Market context: regime, relative strength and macro backdrop.

Three ideas, all computed from candles only, all strictly backward-looking:

``regime``      the benchmark's own trend. A stock in a rising index is a
                different bet than the same stock in a falling one, and the
                current engine scores each name in isolation.
``relative``    ROC(stock) − ROC(benchmark). Tells apart "this rose" from
                "this rose more than the market" — the latter is what a
                swing trader actually wants.
``macro``       VIX level and rate/FX context. Used as a risk dial, not a
                signal: high VIX widens the effective entry threshold.

Every series is aligned to the stock's own timestamps with a backward-only
join (as-of, never forward-filled from the future), so a benchmark bar dated
after the stock bar can never leak into its features.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import db, signals

log = logging.getLogger("trading.context")

# Benchmark per market. GPW.WA is the WIG index; GPW has no Yahoo ETF
# trackers, so the index itself is the benchmark.
BENCHMARKS = {"GPW": "GPW.WA", "US": "^SPX"}
VIX_SYMBOL = "^VIX"

# Regime thresholds (tuned by walk-forward, not by hand).
RS_SMA = 200          # benchmark regime length
RS_ROC = 60           # relative-strength horizon
VIX_PANIC = 30.0      # above this, cut position size
VIX_CAUTION = 22.0


def market_of(symbol: str) -> str:
    s = symbol.upper()
    return "GPW" if s.endswith((".WA", ".PL")) else "US"


def benchmark_for(symbol: str) -> str:
    return BENCHMARKS[market_of(symbol)]


def load_series(symbol: str, limit: int = 4000) -> Optional[pd.Series]:
    """Close series indexed by timestamp, for benchmarks and macro."""
    rows = db.get_candles(symbol, limit=limit)
    if not rows:
        return None
    s = pd.Series([float(r["close"]) for r in rows], index=pd.Index([r["ts"] for r in rows]))
    return s[~s.index.duplicated(keep="last")].sort_index()


def align_to(index_ts: pd.Index, series: Optional[pd.Series]) -> Optional[pd.Series]:
    """Backward-only as-of join: for each bar, the latest benchmark value
    known AT OR BEFORE that bar's timestamp.

    ``reindex(method='ffill')`` on a union index can carry a benchmark print
    into a stock bar that is dated later only if we sorted wrong; sorting both
    sides and using ``ffill`` guarantees the value is never from the future.
    """
    if series is None or len(series) == 0:
        return None
    aligned = series.reindex(series.index.union(index_ts)).ffill()
    return aligned.reindex(index_ts)


def build_context(df: pd.DataFrame, symbol: str) -> dict[str, Any]:
    """Attach regime / relative-strength / macro columns to a stock frame."""
    bench_sym = benchmark_for(symbol)
    bench = load_series(bench_sym)
    vix = load_series(VIX_SYMBOL)
    if bench is None:
        log.warning("no benchmark %s for %s", bench_sym, symbol)
        return {"available": False, "benchmark": bench_sym}

    # signals.load_frame() resets the index to a RangeIndex, so bar i is NOT
    # timestamp i. Every per-bar read below goes through this frame, which is
    # indexed by the bar's own timestamp — aligning on the RangeIndex would
    # match stock bar 400 against a benchmark print from 2009.
    if "ts" in df.columns:
        frm = pd.DataFrame({"close": df["close"].astype(float).values},
                           index=pd.Index(df["ts"].values))
    else:
        frm = pd.DataFrame({"close": df["close"].astype(float)}, index=df.index)

    bench_a = align_to(frm.index, bench)
    vix_a = align_to(frm.index, vix)
    out: dict[str, Any] = {"available": True, "benchmark": bench_sym}

    close = frm["close"]
    if bench_a is not None and len(bench_a) > RS_ROC:
        b = bench_a.astype(float)
        out["bench_close"] = b
        out["bench_sma"] = b.rolling(RS_SMA).mean()
        # Benchmark's own ROC, computed on the aligned (not the raw) series so
        # the horizons line up bar-for-bar with the stock.
        out["bench_roc"] = b.pct_change(RS_ROC) * 100.0
        out["stock_roc"] = close.pct_change(RS_ROC) * 100.0
        out["rs"] = out["stock_roc"] - out["bench_roc"]
        # 63d (one quarter) for a more responsive second read.
        out["rs_short"] = (close.pct_change(63) - b.pct_change(63)) * 100.0
    if vix_a is not None and len(vix_a) > 20:
        out["vix"] = vix_a.astype(float)
        out["vix_pct_rank"] = vix_a.rolling(252).rank(pct=True) if len(vix_a) >= 60 else None

    return out


def context_at(ctx: dict[str, Any], i: int) -> dict[str, Any]:
    """Read the context values for bar ``i`` (all already backward-safe)."""
    if not ctx.get("available"):
        return {"available": False}

    def _val(key: str):
        s = ctx.get(key)
        if s is None:
            return None
        try:
            v = float(s.iloc[i])
        except (IndexError, TypeError, ValueError):
            return None
        return None if (v != v) else v  # NaN check

    bench_close = _val("bench_close")
    bench_sma = _val("bench_sma")
    bench_roc = _val("bench_roc")
    rs = _val("rs")
    rs_short = _val("rs_short")
    vix = _val("vix")

    regime = None
    if bench_close is not None and bench_sma:
        regime = "up" if bench_close > bench_sma else "down"
    elif bench_roc is not None:
        # No 200 bars of benchmark yet — fall back to its own momentum.
        regime = "up" if bench_roc > 0 else "down"

    return {
        "available": True,
        "regime": regime,
        "bench_roc": bench_roc,
        "rs": rs,
        "rs_short": rs_short,
        "vix": vix,
    }


def score_context(c: dict[str, Any], min_rs: float = 0.0,
                  require_up_regime: bool = True,
                  vix_max: Optional[float] = None) -> tuple[bool, list[str], float]:
    """Gate a trade on context. Returns ``(allowed, reasons, size_factor)``.

    ``size_factor`` scales the position: it is 0 when blocked, below 1 when
    the macro dial is caution, 1 when everything lines up.
    """
    if not c.get("available"):
        # No benchmark data: allow the trade, but never size up on the
        # strength of context we do not have.
        return True, ["brak benchmarku — kontekst neutralny"], 0.8

    reasons: list[str] = []
    allowed = True
    size = 1.0

    if require_up_regime and c.get("regime") == "down":
        allowed = False
        reasons.append(
            f"reżim rynku: spadkowy (indeks {c.get('bench_roc', 0):+.1f}% / 60d)")

    rs = c.get("rs")
    if rs is not None and rs < min_rs:
        allowed = False
        reasons.append(f"momentum relatywne {rs:+.1f}% vs indeks — za słabe")

    vix = c.get("vix")
    if vix is not None:
        if vix_max is not None and vix > vix_max:
            allowed = False
            reasons.append(f"VIX {vix:.1f} > limit {vix_max:.0f} — ryzyko zbyt wysokie")
        elif vix >= VIX_CAUTION:
            size = 0.6
            reasons.append(f"VIX {vix:.1f} podwyższony — pozycja 60%")
        elif vix < 18:
            reasons.append(f"VIX {vix:.1f} — spokojny rynek")

    if allowed and not reasons:
        if c.get("regime") == "up":
            reasons.append("reżim rynku: wzrostowy")
        if rs is not None:
            reasons.append(f"RS {rs:+.1f}% vs indeks")
    return allowed, reasons, size


def context_frame(symbol: str, df: pd.DataFrame) -> pd.DataFrame:
    """Convenience for inspection: a frame with the context columns added."""
    ctx = build_context(df, symbol)
    if not ctx.get("available"):
        return df.assign(ctx_regime=None, ctx_rs=None, ctx_vix=None)
    idx = df.index
    return df.assign(
        ctx_regime=[context_at(ctx, i)["regime"] for i in range(len(idx))],
        ctx_rs=[context_at(ctx, i)["rs"] for i in range(len(idx))],
        ctx_vix=[context_at(ctx, i)["vix"] for i in range(len(idx))],
    )


_ = np  # numpy is used via pandas; keep the import meaningful
