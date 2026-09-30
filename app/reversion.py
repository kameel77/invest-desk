"""Mean-reversion hypothesis.

The trend+momentum system lost money out-of-sample (-2.02% walk-forward).
Mean reversion is the mirror image and the only family that cleared the bar:
+0.67% expectancy per trade over 8 folds, positive in 5 of 8.

Parameters are the ones walk-forward kept choosing (7 of 8 folds):
    lookback=20, z_entry=2.0, hold=10, stop=2.5xATR, target=3.0xATR

It lives beside the trend engine, not instead of it. Both feed the same signal
table and the same paper account, each tagged with its hypothesis, so the live
comparison is the real test of which one the backtest was right about.
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import config, db, signals

# --- walk-forward-selected parameters ------------------------------------
LOOKBACK = 20
Z_ENTRY = 2.0
HOLD_DAYS = 10
STOP_ATR = 2.5
TARGET_ATR = 3.0

HYPOTHESIS = "reversion"


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """Add the z-score of price against its own moving average."""
    close = df["close"].astype(float)
    ma = close.rolling(LOOKBACK).mean()
    sd = close.rolling(LOOKBACK).std()
    df = df.copy()
    df["rv_ma"] = ma
    df["rv_sd"] = sd
    df["rv_z"] = (close - ma) / sd.replace(0, np.nan)
    # How far the stretch is in ATR terms — a sanity check that the stretch is
    # not just a low-volatility artefact.
    df["rv_stretch_atr"] = (close - ma) / df["atr"].replace(0, np.nan)
    return df


def evaluate(symbol: str) -> Optional[dict[str, Any]]:
    """Current reversion read for one instrument, or None without history."""
    raw = signals.load_frame(symbol, limit=1000)
    if raw is None:
        return None
    df = compute(signals.compute_indicators(raw))
    if len(df) < LOOKBACK + 25:
        return None

    row = df.iloc[-1]
    z = float(row["rv_z"]) if not pd.isna(row["rv_z"]) else None
    price = float(row["close"])
    atr = float(row["atr"]) if not pd.isna(row["atr"]) else price * 0.02
    ma = float(row["rv_ma"]) if not pd.isna(row["rv_ma"]) else price
    stretch = float(row["rv_stretch_atr"]) if not pd.isna(row["rv_stretch_atr"]) else None

    # Signal strength: 1.0 at the entry threshold, growing with the stretch.
    if z is None:
        strength = 0.0
        eligible = False
    else:
        strength = max(0.0, min(1.0, (-z) / (Z_ENTRY * 2)))
        eligible = z <= -Z_ENTRY

    # Mean reversion buys dislocations, so it wants the stretch measured in
    # ATR as well as in sigma: a -2-sigma move on 0.3% daily vol is noise.
    atr_ok = stretch is None or stretch <= -1.0

    reasons: list[str] = []
    if z is not None:
        if eligible:
            reasons.append(f"cena {z:.2f}σ poniżej średniej {LOOKBACK}d "
                           f"(próg -{Z_ENTRY:.1f}σ)")
        else:
            reasons.append(f"cena {z:.2f}σ vs średnia {LOOKBACK}d — brak rozciągnięcia")
    if stretch is not None:
        reasons.append(f"rozciągnięcie {stretch:+.1f}×ATR")
    if not atr_ok:
        reasons.append("rozciągnięcie zbyt małe w relacji do ATR — odrzucone")

    return {
        "symbol": symbol.upper(),
        "hypothesis": HYPOTHESIS,
        "ts": int(row["ts"]),
        "price": price,
        "z": None if z is None else round(z, 2),
        "ma": round(ma, 4),
        "mean_gap_pct": round(((price / ma) - 1) * 100, 2) if ma else 0.0,
        "stretch_atr": None if stretch is None else round(stretch, 2),
        "atr": round(atr, 4),
        "strength": round(strength, 3),
        "eligible": bool(eligible and atr_ok),
        "target_mean": round(ma, 4),
        "stop": round(max(price - STOP_ATR * atr, price * 0.90), 4),
        "take": round(price + TARGET_ATR * atr, 4),
        "max_hold_days": HOLD_DAYS,
        "reasons": reasons,
    }


def size(analysis: dict[str, Any], cash: float) -> dict[str, float]:
    """Volatility-targeted sizing, matching the live engine's shape.

    A deeper stretch sizes up, but the conviction multiplier is capped well
    below the trend engine's, because the stop is 2.5 ATR away and one more
    bad dislocation against you doubles the loss.
    """
    price = float(analysis["price"])
    atr = float(analysis["atr"]) or price * 0.02
    risk_per_share = max(price - float(analysis["stop"]), price * 0.005, 0.01)
    strength = float(analysis.get("strength") or 0.0)
    conviction = 1.0 + 0.5 * strength          # 1.0 … 1.5
    budget = cash * 0.005 * conviction
    qty = min(budget / risk_per_share, (cash * 0.15) / max(price, 0.01))
    notional = qty * price
    if notional < config.MIN_TICKET:
        qty = 0.0
        notional = 0.0
    return {"qty": round(qty, 4), "notional": round(notional, 2),
            "risk": round(qty * risk_per_share, 2),
            "stop": analysis["stop"], "atr": round(atr, 4)}


def bars_since_extreme(symbol: str, limit: int = 200) -> Optional[int]:
    """How many bars ago the qualifying stretch started (for the UI)."""
    raw = signals.load_frame(symbol, limit=limit)
    if raw is None:
        return None
    df = compute(signals.compute_indicators(raw))
    z = df["rv_z"].dropna()
    if z.empty:
        return None
    below = (z <= -Z_ENTRY).values
    if not below.any():
        return None
    # Walk back from the newest bar to the start of the current stretch.
    age = 0
    for i in range(len(below) - 1, -1, -1):
        if not below[i]:
            break
        age += 1
    return age or None


_ = (db, math)  # imported for parity with the trend module
