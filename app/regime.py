"""Market regime filter: detects macro market trends using index moving averages (e.g. SPY > SMA200).

Used to gate new long mean-reversion entries during bear markets.
"""
from __future__ import annotations

from typing import Mapping
import pandas as pd


def spy_above_sma(spy_df: pd.DataFrame, window: int = 200) -> dict[int, bool]:
    """Compute whether SPY close(t) > SMA_window(close)(t) for each session timestamp.

    Rules:
      - First window - 1 sessions (e.g. 199 bars for window=200) return False (warmup period).
      - Evaluated at bar Close(t) so execution at Open(t+1) is strictly lookahead-free.
      - Returns a mapping of {int(ts): bool}.
      - Missing bars or dates outside this mapping should be treated as False (no entry).
    """
    if spy_df is None or len(spy_df) == 0 or window <= 0:
        return {}

    df = spy_df.sort_values("ts").copy()
    close = df["close"].astype(float)
    sma = close.rolling(window=window).mean()

    # close > sma is False whenever sma is NaN (warmup: first window-1 bars)
    is_above = (close > sma).fillna(False)

    return {int(ts): bool(val) for ts, val in zip(df["ts"], is_above)}
