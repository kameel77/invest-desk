"""Portfolio simulation engine for multi-asset trading strategies.

Implements bar-by-bar execution with portfolio slot constraints, deterministic ranking,
exact cash book management, and realistic broker cost profiles.

Event loop sequence per trading day t:
  1. Morning (Open t): Execute queued BUY orders from Close(t-1) at Open(t) * (1 + slippage).
  2. Intraday: Check exits for open slots via reversion.check_bar_exit() in strict priority:
     Stop-loss -> Take-profit -> Mean-reversion -> Time-stop.
     Freed cash from exits is credited and available for allocations at Close(t).
  3. Evening (Close t): Evaluate signals across universe.
     Rank candidates: ascending by z_score (deepest oversold first), tie-breaker by symbol.
     Allocate to available slots up to max_slots.
     Track rejected signals (slot_full vs cash_exhausted).
  4. End of Day: Mark-to-market portfolio equity, cash, and drawdowns.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import backtest as bt
from . import config, costs, reversion, signals


@dataclass
class PositionSlot:
    symbol: str
    entry_ts: int
    entry_bar_idx: int
    entry_px: float
    shares: float
    stop_px: float
    target_px: float
    z_entry: float
    bars_held: int = 0


@dataclass
class SimulatedTrade:
    symbol: str
    entry_ts: int
    exit_ts: int
    entry_px: float
    exit_px: float
    shares: float
    notional: float
    reason: str
    gross_return_pct: float
    net_return_pct: float
    net_pnl: float
    bars_held: int
    z_entry: float


class PortfolioSimulator:
    """Multi-asset portfolio simulator with slot limits, deterministic priority, and cash accounting."""

    def __init__(
        self,
        symbols: list[str],
        initial_cash: float = 10_000.0,
        max_slots: int = 5,
        slot_pct: float | None = None,
        broker_profile: str = "IBKR_TIERED_USD",
        lookback: int = reversion.LOOKBACK,
        z_entry: float = reversion.Z_ENTRY,
        hold_days: int = reversion.HOLD_DAYS,
        stop_atr: float = reversion.STOP_ATR,
        target_atr: float = reversion.TARGET_ATR,
        min_stretch_atr: float = 1.0,
        fractional: bool = True,
    ):
        self.symbols = [s.upper() for s in symbols]
        self.initial_cash = float(initial_cash)
        self.max_slots = int(max_slots)
        self.slot_pct = float(slot_pct) if slot_pct is not None else (1.0 / max(1, self.max_slots))
        self.broker_profile = costs.get_profile(broker_profile)
        self.lookback = lookback
        self.z_entry = z_entry
        self.hold_days = hold_days
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.min_stretch_atr = min_stretch_atr
        self.fractional = fractional

    def _prepare_data(self) -> tuple[list[int], dict[str, pd.DataFrame]]:
        """Load and align daily candles across symbols, precomputing indicators."""
        symbol_frames: dict[str, pd.DataFrame] = {}
        all_timestamps: set[int] = set()

        for s in self.symbols:
            df = bt._cached_frame(s)
            if df is None or len(df) < self.lookback + 2:
                continue
            df = df.sort_values("ts").reset_index(drop=True)
            if "atr" not in df.columns:
                df = signals.compute_indicators(df)
            df = reversion.compute(df)
            symbol_frames[s] = df
            all_timestamps.update(df["ts"].astype(int).tolist())

        sorted_timestamps = sorted(all_timestamps)
        return sorted_timestamps, symbol_frames

    def run(
        self,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        all_trades: bool = False,
    ) -> dict[str, Any]:
        """Execute the bar-by-bar simulation over the requested timestamp range."""
        timestamps, frames = self._prepare_data()
        if not timestamps or not frames:
            return {"error": "no data available for requested universe"}

        # Filter active simulation dates
        active_ts = [
            ts for ts in timestamps
            if (start_ts is None or ts >= start_ts) and (end_ts is None or ts <= end_ts)
        ]
        if len(active_ts) < 2:
            return {"error": "insufficient dates in simulation window"}

        # Pre-index frames by ts for O(1) lookup
        # frame_by_ts[sym][ts] -> dict row
        data_by_sym_ts: dict[str, dict[int, dict[str, Any]]] = {}
        for sym, df in frames.items():
            records = df.to_dict("records")
            data_by_sym_ts[sym] = {int(r["ts"]): r for r in records}

        cash = self.initial_cash
        open_slots: dict[str, PositionSlot] = {}
        queued_buys: list[dict[str, Any]] = []
        closed_trades: list[SimulatedTrade] = []
        rejected_signals: list[dict[str, Any]] = []
        daily_snapshots: list[dict[str, Any]] = []

        last_known_close: dict[str, float] = {}

        for day_idx, t_ts in enumerate(active_ts):
            # -------------------------------------------------------------
            # Step 1: Morning Execution of Queued Orders on Open(t)
            # -------------------------------------------------------------
            for q in queued_buys:
                sym = q["symbol"]
                bar_t = data_by_sym_ts.get(sym, {}).get(t_ts)
                if bar_t is None or pd.isna(bar_t.get("open")) or float(bar_t["open"]) <= 0:
                    rejected_signals.append({
                        "ts": t_ts, "symbol": sym, "reason": "no_open_price"
                    })
                    continue

                open_px = float(bar_t["open"])
                slip_mult = 1.0 + self.broker_profile.slippage_pct
                executed_entry_px = open_px * slip_mult

                # Position sizing: target slot notional
                allocated_budget = q["target_budget"]
                usable_cash = min(cash, allocated_budget)
                if usable_cash < config.MIN_TICKET:
                    rejected_signals.append({
                        "ts": t_ts, "symbol": sym, "reason": "cash_exhausted"
                    })
                    continue

                # Number of shares (fractional or whole)
                if self.fractional:
                    shares = usable_cash / executed_entry_px
                else:
                    shares = float(math.floor(usable_cash / executed_entry_px))

                if shares <= 0:
                    rejected_signals.append({
                        "ts": t_ts, "symbol": sym, "reason": "size_below_min"
                    })
                    continue

                # Trade cash deduction
                costs_res = costs.calculate_trade_costs("BUY", open_px, shares, self.broker_profile)
                if costs_res["total_cash"] > cash:
                    if self.fractional:
                        ratio = cash / costs_res["total_cash"]
                        shares *= (ratio * 0.9999)
                        costs_res = costs.calculate_trade_costs("BUY", open_px, shares, self.broker_profile)
                    else:
                        # Adjust shares if commission pushed over cash
                        shares -= 1
                        if shares <= 0:
                            rejected_signals.append({
                                "ts": t_ts, "symbol": sym, "reason": "cash_exhausted_comm"
                            })
                            continue
                        costs_res = costs.calculate_trade_costs("BUY", open_px, shares, self.broker_profile)

                cash -= costs_res["total_cash"]

                # Anchor risk levels to executed entry price
                atr_val = q["atr"]
                stop_px = max(executed_entry_px - self.stop_atr * atr_val, executed_entry_px * 0.90)
                target_px = executed_entry_px + self.target_atr * atr_val

                open_slots[sym] = PositionSlot(
                    symbol=sym,
                    entry_ts=t_ts,
                    entry_bar_idx=day_idx,
                    entry_px=open_px,
                    shares=shares,
                    stop_px=stop_px,
                    target_px=target_px,
                    z_entry=q["z_entry"],
                    bars_held=0,
                )

            queued_buys.clear()

            # -------------------------------------------------------------
            # Step 2: Intraday Bar Evaluation for Open Positions
            # -------------------------------------------------------------
            symbols_to_close: list[tuple[str, float, str]] = []

            for sym, slot in list(open_slots.items()):
                bar_t = data_by_sym_ts.get(sym, {}).get(t_ts)
                if bar_t is None:
                    # Missing bar on this day: position continues, hold count increases
                    slot.bars_held += 1
                    continue

                last_known_close[sym] = float(bar_t["close"])
                slot.bars_held += 1

                low_k = float(bar_t["low"])
                high_k = float(bar_t["high"])
                open_k = float(bar_t["open"])
                close_k = float(bar_t["close"])
                zk = float(bar_t["rv_z"]) if not pd.isna(bar_t.get("rv_z")) else float("nan")

                exit_px, reason = reversion.check_bar_exit(
                    low_k=low_k,
                    high_k=high_k,
                    open_k=open_k,
                    close_k=close_k,
                    zk=zk,
                    stop_px=slot.stop_px,
                    target_px=slot.target_px,
                    bars_held=slot.bars_held,
                    max_hold=self.hold_days,
                )

                if exit_px is not None and reason is not None:
                    symbols_to_close.append((sym, exit_px, reason))

            # Process exits
            symbols_closed_today: set[str] = set()
            for sym, exit_px, reason in symbols_to_close:
                slot = open_slots.pop(sym)
                symbols_closed_today.add(sym)
                sell_costs = costs.calculate_trade_costs("SELL", exit_px, slot.shares, self.broker_profile)
                cash += sell_costs["total_cash"]

                # Calculate trade return
                buy_costs = costs.calculate_trade_costs("BUY", slot.entry_px, slot.shares, self.broker_profile)
                net_pnl = sell_costs["total_cash"] - buy_costs["total_cash"]
                net_ret = (sell_costs["total_cash"] / buy_costs["total_cash"] - 1.0) * 100.0
                gross_ret = (exit_px / (slot.entry_px * (1 + self.broker_profile.slippage_pct)) - 1.0) * 100.0

                closed_trades.append(SimulatedTrade(
                    symbol=sym,
                    entry_ts=slot.entry_ts,
                    exit_ts=t_ts,
                    entry_px=slot.entry_px,
                    exit_px=exit_px,
                    shares=slot.shares,
                    notional=slot.shares * slot.entry_px,
                    reason=reason,
                    gross_return_pct=round(gross_ret, 4),
                    net_return_pct=round(net_ret, 4),
                    net_pnl=round(net_pnl, 2),
                    bars_held=slot.bars_held,
                    z_entry=slot.z_entry,
                ))

            # -------------------------------------------------------------
            # Step 3: End of Day Signal Generation on Close(t)
            # -------------------------------------------------------------
            # Update last known closes
            for sym in self.symbols:
                bar = data_by_sym_ts.get(sym, {}).get(t_ts)
                if bar is not None and not pd.isna(bar.get("close")):
                    last_known_close[sym] = float(bar["close"])

            # Current equity for position sizing
            open_positions_value = sum(
                slot.shares * last_known_close.get(slot.symbol, slot.entry_px)
                for slot in open_slots.values()
            )
            current_equity = cash + open_positions_value
            available_slots = max(0, self.max_slots - len(open_slots))

            # Evaluate entry signals across universe
            # Cooldown: Symbol zamknięty w dniu t nie może generować sygnału na Close(t)
            candidates: list[dict[str, Any]] = []
            for sym in self.symbols:
                if sym in open_slots or sym in symbols_closed_today:
                    continue
                bar_t = data_by_sym_ts.get(sym, {}).get(t_ts)
                if bar_t is None:
                    continue

                z_val = bar_t.get("rv_z")
                stretch_val = bar_t.get("rv_stretch_atr")
                atr_val = bar_t.get("atr")

                if reversion.evaluate_entry_signal(
                    z_val, stretch_val, atr_val,
                    z_entry=self.z_entry, min_stretch_atr=self.min_stretch_atr
                ):
                    candidates.append({
                        "symbol": sym,
                        "z": float(z_val),
                        "atr": float(atr_val),
                        "close": float(bar_t["close"]),
                    })

            # Deterministic ranking: lowest z_score first, tie-breaker alphabetically by symbol
            candidates.sort(key=lambda c: (c["z"], c["symbol"]))

            target_slot_budget = current_equity * self.slot_pct

            for cand in candidates:
                sym = cand["symbol"]
                if available_slots > 0:
                    if cash >= config.MIN_TICKET:
                        queued_buys.append({
                            "symbol": sym,
                            "signal_ts": t_ts,
                            "z_entry": cand["z"],
                            "atr": cand["atr"],
                            "target_budget": target_slot_budget,
                        })
                        available_slots -= 1
                    else:
                        rejected_signals.append({
                            "ts": t_ts, "symbol": sym, "z": cand["z"], "reason": "cash_exhausted"
                        })
                else:
                    rejected_signals.append({
                        "ts": t_ts, "symbol": sym, "z": cand["z"], "reason": "slot_full"
                    })

            # -------------------------------------------------------------
            # Step 4: Mark to Market Equity Snapshot for Day t
            # -------------------------------------------------------------
            pos_val = sum(
                slot.shares * last_known_close.get(slot.symbol, slot.entry_px)
                for slot in open_slots.values()
            )
            eod_equity = cash + pos_val
            daily_snapshots.append({
                "ts": t_ts,
                "cash": round(cash, 2),
                "open_positions_count": len(open_slots),
                "open_positions_value": round(pos_val, 2),
                "equity": round(eod_equity, 2),
            })

        # Calculate performance statistics
        return self._compute_performance(daily_snapshots, closed_trades, rejected_signals, all_trades=all_trades)

    def _compute_performance(
        self,
        snapshots: list[dict[str, Any]],
        trades: list[SimulatedTrade],
        rejected: list[dict[str, Any]],
        all_trades: bool = False,
    ) -> dict[str, Any]:
        """Compute comprehensive portfolio metrics from daily curve and trades."""
        if not snapshots:
            return {"error": "no simulation snapshots recorded"}

        equities = np.array([s["equity"] for s in snapshots], dtype=float)
        days = len(equities)
        initial = self.initial_cash
        final = float(equities[-1])
        total_return_pct = round(((final / initial) - 1.0) * 100.0, 3)

        # CAGR
        years = days / 252.0
        cagr = round((((final / initial) ** (1.0 / max(years, 0.1))) - 1.0) * 100.0, 2) if final > 0 else -100.0

        # Drawdown
        running_max = np.maximum.accumulate(equities)
        drawdowns = (equities - running_max) / running_max
        max_drawdown_pct = round(float(np.min(drawdowns)) * 100.0, 2)

        # Max Drawdown Duration (bars under water)
        under_water_bars = 0
        max_under_water = 0
        for dd in drawdowns:
            if dd < 0:
                under_water_bars += 1
                if under_water_bars > max_under_water:
                    max_under_water = under_water_bars
            else:
                under_water_bars = 0

        # Daily returns
        daily_returns = np.diff(equities) / equities[:-1] if len(equities) > 1 else np.array([0.0])
        mean_daily = float(np.mean(daily_returns))
        std_daily = float(np.std(daily_returns, ddof=1)) if len(daily_returns) > 1 else 0.0

        # Sharpe Ratio (annualized sqrt(252))
        rf0_sharpe = round(math.sqrt(252) * (mean_daily / std_daily), 3) if std_daily > 1e-8 else 0.0
        # rf = 4% annualized -> daily rf = 0.04 / 252
        daily_rf = 0.04 / 252.0
        rf4_sharpe = round(math.sqrt(252) * ((mean_daily - daily_rf) / std_daily), 3) if std_daily > 1e-8 else 0.0

        # Downside deviation for Sortino
        downside_returns = daily_returns[daily_returns < 0]
        downside_std = float(np.std(downside_returns, ddof=1)) if len(downside_returns) > 1 else 0.0
        sortino = round(math.sqrt(252) * (mean_daily / downside_std), 3) if downside_std > 1e-8 else 0.0

        # Calmar Ratio
        calmar = round(cagr / abs(max_drawdown_pct), 2) if abs(max_drawdown_pct) > 0.01 else 0.0

        # Slot utilization
        slots_used = [s["open_positions_count"] for s in snapshots]
        avg_slots_used = round(float(np.mean(slots_used)), 2)
        pct_slots_80 = round(sum(1 for u in slots_used if u >= (self.max_slots * 0.8)) / max(1, len(slots_used)) * 100.0, 1)

        # Trade stats
        net_rets = [t.net_return_pct for t in trades]
        winning_trades = [r for r in net_rets if r > 0]
        losing_trades = [r for r in net_rets if r < 0]
        win_rate = round(len(winning_trades) / max(1, len(net_rets)) * 100.0, 1)

        gross_profits = sum(t.net_pnl for t in trades if t.net_pnl > 0)
        gross_losses = abs(sum(t.net_pnl for t in trades if t.net_pnl < 0))
        profit_factor = round(gross_profits / gross_losses, 2) if gross_losses > 0 else (999.0 if gross_profits > 0 else 0.0)

        # Pełne powody odrzuceń - zliczanie każdego reason osobno
        rejected_by_reason: dict[str, int] = {
            "slot_full": 0,
            "cash_exhausted": 0,
            "cash_exhausted_comm": 0,
            "size_below_min": 0,
            "no_open_price": 0,
        }
        for r in rejected:
            reason = r.get("reason", "unknown")
            rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + 1

        formatted_trades = [
            {
                "symbol": t.symbol,
                "entry_ts": t.entry_ts,
                "exit_ts": t.exit_ts,
                "entry_px": t.entry_px,
                "exit_px": t.exit_px,
                "shares": t.shares,
                "net_ret": t.net_return_pct,
                "net_pnl": t.net_pnl,
                "reason": t.reason,
                "bars_held": t.bars_held,
            }
            for t in trades
        ]

        return {
            "initial_cash": self.initial_cash,
            "final_equity": round(final, 2),
            "total_return_pct": total_return_pct,
            "cagr": cagr,
            "max_drawdown_pct": max_drawdown_pct,
            "max_drawdown_duration_bars": max_under_water,
            "sharpe_rf0": rf0_sharpe,
            "sharpe_rf4": rf4_sharpe,
            "sortino": sortino,
            "calmar": calmar,
            "days": days,
            "total_trades": len(trades),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "expectancy_per_trade": round(float(np.mean(net_rets)), 3) if net_rets else 0.0,
            "median_trade_ret": round(float(np.median(net_rets)), 3) if net_rets else 0.0,
            "avg_slots_used": avg_slots_used,
            "pct_days_slots_above_80": pct_slots_80,
            "rejected_signals": {
                "total": len(rejected),
                "slot_full": rejected_by_reason.get("slot_full", 0),
                "cash_exhausted": rejected_by_reason.get("cash_exhausted", 0),
                "cash_exhausted_comm": rejected_by_reason.get("cash_exhausted_comm", 0),
                "size_below_min": rejected_by_reason.get("size_below_min", 0),
                "no_open_price": rejected_by_reason.get("no_open_price", 0),
            },
            "closed_trades": formatted_trades if all_trades else formatted_trades[:100],
            "all_closed_trades": formatted_trades,
            "curve": [
                {"ts": s["ts"], "equity": s["equity"], "cash": s["cash"], "pos_count": s["open_positions_count"]}
                for s in snapshots
            ],
        }
