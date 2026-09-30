"""Signal engine.

The default book is a swing-trend + momentum system sized for small tickets:

  * ``trend``     — price vs SMA50/SMA200 regime and SMA50 slope
  * ``momentum``  — 20/60-day ROC, RSI(14), distance from SMA20 in ATR units
  * ``liquidity`` — 20-day average dollar volume, hard gate for small tickets
  * ``reversion`` — Bollinger %B and z-score, used as a tilt, never as the trigger

Each component scores in [-1, +1]; the composite is their weighted sum scaled
to [-100, +100]. A signal fires when the composite crosses a threshold in the
right direction, which keeps the desk from re-signalling the same tape bar
after bar.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import config, db

log = logging.getLogger("trading.signals")

# Tunables — deliberately plain module constants so they're greppable.
SMA_FAST = 50
SMA_SLOW = 200
MOM_FAST = 20
MOM_SLOW = 60
RSI_LEN = 14
ATR_LEN = 14
BB_LEN = 20
BB_STD = 2.0
VOL_LOOKBACK = 20

WEIGHTS = {"trend": 0.40, "momentum": 0.35, "reversion": 0.25}
# Minimum 20-day average dollar volume — below this a small ticket moves the price.
MIN_ADV_USD = float(1_000_000)      # US equities
MIN_ADV_PLN = float(300_000)        # GPW (converted below)
BUY_THRESHOLD = 35.0
SELL_THRESHOLD = -25.0             # exit earlier than entry: cut losers faster
MIN_BARS = SMA_SLOW + 10


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, x)))


def load_frame(symbol: str, limit: int = 500) -> Optional[pd.DataFrame]:
    rows = db.get_candles(symbol, limit=limit)
    if len(rows) < 30:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    df = df.sort_values("ts").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    return df if len(df) >= 30 else None


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close, high, low = df["close"], df["high"], df["low"]

    df["sma_fast"] = close.rolling(SMA_FAST).mean()
    df["sma_slow"] = close.rolling(SMA_SLOW).mean()
    df["sma20"] = close.rolling(20).mean()
    df["ema12"] = close.ewm(span=12, adjust=False).mean()
    df["ema26"] = close.ewm(span=26, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(RSI_LEN).mean()
    loss = (-delta.clip(upper=0)).rolling(RSI_LEN).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_LEN).mean()

    mid = df["sma20"]
    std = close.rolling(BB_LEN).std(ddof=0)
    df["bb_upper"] = mid + BB_STD * std
    df["bb_lower"] = mid - BB_STD * std
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / mid.replace(0, np.nan)

    df["roc_fast"] = close.pct_change(MOM_FAST) * 100.0
    df["roc_slow"] = close.pct_change(MOM_SLOW) * 100.0
    df["adv"] = (close * df["volume"]).rolling(VOL_LOOKBACK).mean()
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(VOL_LOOKBACK).mean().replace(0, np.nan)
    df["ret"] = close.pct_change()
    df["vol_ann"] = df["ret"].rolling(20).std() * math.sqrt(252) * 100.0
    return df


def _score_trend(row: pd.Series) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0
    price, fast, slow = float(row["close"]), row["sma_fast"], row["sma_slow"]

    if pd.isna(slow):
        # Not enough history for the slow regime filter — use the fast one only.
        if not pd.isna(fast) and price > fast:
            score += 0.35
            reasons.append("price above SMA50 (long regime not yet confirmable)")
        elif not pd.isna(fast):
            score -= 0.35
            reasons.append("price below SMA50")
        return _clip(score), reasons

    if price > slow and fast > slow:
        score += 0.6
        reasons.append("price > SMA200 and SMA50 > SMA200 — confirmed uptrend")
    elif price < slow and fast < slow:
        score -= 0.6
        reasons.append("price < SMA200 and SMA50 < SMA200 — confirmed downtrend")
    elif price > slow:
        score += 0.2
        reasons.append("price above SMA200, MA stack not yet bullish")
    else:
        score -= 0.2
        reasons.append("price below SMA200")

    # SMA50 slope over the last 5 bars, normalised by ATR.
    return _clip(score), reasons


def _trend_slope(df: pd.DataFrame, i: int) -> Optional[float]:
    if i < 5 or df["sma_fast"].iloc[i - 5] is None:
        return None
    a, b = df["sma_fast"].iloc[i - 5], df["sma_fast"].iloc[i]
    if pd.isna(a) or pd.isna(b):
        return None
    return float(b - a)


def _score_momentum(df: pd.DataFrame, i: int) -> tuple[float, list[str]]:
    row = df.iloc[i]
    reasons: list[str] = []
    score = 0.0

    roc_f, roc_s = row["roc_fast"], row["roc_slow"]
    if not pd.isna(roc_f):
        # ±15% over 20 sessions is a strong swing move; scale to ±1.
        score += _clip(roc_f / 15.0) * 0.6
        if abs(roc_f) > 5:
            reasons.append(f"20d ROC {roc_f:+.1f}%")
    if not pd.isna(roc_s):
        score += _clip(roc_s / 30.0) * 0.4
        if abs(roc_s) > 10:
            reasons.append(f"60d ROC {roc_s:+.1f}%")

    rsi = row["rsi"]
    if not pd.isna(rsi):
        if rsi >= 70:
            score -= 0.25
            reasons.append(f"RSI {rsi:.0f} overbought")
        elif rsi <= 30:
            score += 0.25
            reasons.append(f"RSI {rsi:.0f} oversold")
        else:
            # 50 is neutral; above it is mildly bullish.
            score += _clip((rsi - 50) / 25.0) * 0.2

    slope = _trend_slope(df, i)
    atr = row["atr"]
    if slope is not None and not pd.isna(atr) and atr > 0:
        norm = _clip(slope / (atr * 1.5))
        score += norm * 0.2
        if abs(norm) > 0.5:
            reasons.append("SMA50 slope " + ("rising" if norm > 0 else "falling"))

    return _clip(score), reasons


def _score_reversion(df: pd.DataFrame, i: int) -> tuple[float, list[str]]:
    row = df.iloc[i]
    price = row["close"]
    reasons: list[str] = []
    up, lo, mid = row["bb_upper"], row["bb_lower"], row["sma20"]

    if not (pd.isna(up) or pd.isna(lo) or pd.isna(mid) or up == lo):
        pct_b = (price - lo) / (up - lo)
        if pct_b > 1.0:
            score = -0.5
            reasons.append(f"close above upper band (%B {pct_b:.2f}) — stretched")
        elif pct_b < 0.0:
            score = 0.5
            reasons.append(f"close below lower band (%B {pct_b:.2f}) — washed out")
        else:
            score = (0.5 - pct_b) * 0.8
    else:
        score = 0.0

    std = (row["bb_upper"] - row["bb_lower"]) / (2 * BB_STD) if not pd.isna(mid) and mid else np.nan
    if not pd.isna(std) and std > 0 and not pd.isna(mid):
        z = (price - mid) / std
        # Within ±1σ is neutral; beyond that is a genuine stretch.
        score += _clip((2.0 - abs(z)) / 2.0 * 0.5 - 0.25)
    return _clip(score), reasons


def _liquidity_gate(df: pd.DataFrame, i: int, market: str) -> tuple[bool, str, float]:
    adv = df["adv"].iloc[i]
    if pd.isna(adv) or adv <= 0:
        return False, "no volume history", 0.0
    floor = MIN_ADV_PLN if market == "GPW" else MIN_ADV_USD
    # bool(): the comparison yields numpy.bool_, which json cannot serialise.
    ok = bool(adv >= floor)
    return ok, f"ADV {adv:,.0f} vs floor {floor:,.0f}", float(adv)


def evaluate(symbol: str) -> Optional[dict[str, Any]]:
    """Full analysis for one symbol. Returns ``None`` when history is too thin."""
    df = load_frame(symbol)
    if df is None:
        return None
    df = compute_indicators(df)
    if len(df) < max(60, min(MIN_BARS, len(df))):
        return None

    i = len(df) - 1
    row = df.iloc[i]
    quote = db.get_quotes([symbol])
    market = (quote[0]["exchange"] and "Warsaw" in str(quote[0]["exchange"]) and "GPW") \
        or ("GPW" if symbol.upper().endswith((".WA", ".PL")) else "US")

    trend_s, trend_r = _score_trend(row)
    mom_s, mom_r = _score_momentum(df, i)
    rev_s, rev_r = _score_reversion(df, i)
    composite = (trend_s * WEIGHTS["trend"] + mom_s * WEIGHTS["momentum"]
                 + rev_s * WEIGHTS["reversion"]) * 100.0

    liq_ok, liq_text, adv = _liquidity_gate(df, i, market)
    reasons = trend_r + mom_r + rev_r + [liq_text]
    if row["vol_ratio"] is not None and not pd.isna(row["vol_ratio"]) and row["vol_ratio"] > 1.8:
        reasons.append(f"volume {row['vol_ratio']:.1f}x the 20d average")

    price = float(row["close"])
    atr = float(row["atr"]) if not pd.isna(row["atr"]) else price * 0.02
    # Volatility-targeted position sizing: risk 0.5% of equity per 2-ATR stop.
    stop = max(price - 2.0 * atr, price * 0.90)
    take = price + 3.0 * atr

    return {
        "symbol": symbol.upper(),
        "ts": int(row["ts"]),
        "price": price,
        "market": market,
        "score": round(float(composite), 1),
        "components": {
            "trend": round(trend_s, 3),
            "momentum": round(mom_s, 3),
            "reversion": round(rev_s, 3),
        },
        "rsi": None if pd.isna(row["rsi"]) else round(float(row["rsi"]), 1),
        "roc_fast": None if pd.isna(row["roc_fast"]) else round(float(row["roc_fast"]), 2),
        "roc_slow": None if pd.isna(row["roc_slow"]) else round(float(row["roc_slow"]), 2),
        "vol_ann": None if pd.isna(row["vol_ann"]) else round(float(row["vol_ann"]), 1),
        "atr": round(atr, 4),
        "adv": adv,
        "liquidity_ok": liq_ok,
        "sma20": None if pd.isna(row["sma20"]) else round(float(row["sma20"]), 4),
        "sma50": None if pd.isna(row["sma_fast"]) else round(float(row["sma_fast"]), 4),
        "sma200": None if pd.isna(row["sma_slow"]) else round(float(row["sma_slow"]), 4),
        "suggested_stop": round(stop, 4),
        "suggested_target": round(take, 4),
        "reasons": reasons,
        "bars": len(df),
    }


def size_position(analysis: dict[str, Any], cash: float) -> dict[str, float]:
    """Volatility-targeted sizing for a small ticket.

    Risk budget is 0.5% of equity across a 2-ATR stop, then capped so a single
    name never exceeds 15% of the account and never below ``config.MIN_TICKET``.
    """
    price = float(analysis["price"])
    atr = float(analysis["atr"]) or price * 0.02
    stop = float(analysis["suggested_stop"])
    risk_per_share = max(price - stop, price * 0.005, 0.01)
    budget = max(cash * 0.005, 1.0)
    qty_risk = budget / risk_per_share
    qty_cap = (cash * 0.15) / max(price, 0.01)
    qty = min(qty_risk, qty_cap)
    notional = qty * price
    if notional < config.MIN_TICKET:
        qty = 0.0
        notional = 0.0
    return {
        "qty": round(qty, 4),
        "notional": round(notional, 2),
        "risk": round(qty * risk_per_share, 2),
        "stop": stop,
        "atr": round(atr, 4),
    }
