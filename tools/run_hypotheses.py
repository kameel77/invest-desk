"""Run every hypothesis family through the same anchored walk-forward.

The point is not to find a winner — the trend system already lost out-of-sample
at -2.02%, and a family that only looks good in-sample is worthless. Each
family gets tuned on the train window and scored on bars it never saw, exactly
like the original strategy did.

Run:  .venv/bin/python -m tools.run_hypotheses
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import backtest as bt
from app import config, costs, db, hypotheses as H, market, reversion, signals
from app.portfolio_sim import PortfolioSimulator

DAY = 86400
GATE_V2_DEFAULT_PROFILE = "XTB_USD"  # --gate-v2 targets the XTB account; v1 keeps config.DEFAULT_BROKER_PROFILE_US


def _now_ts() -> int:
    rows = db.get_candles(db.get_watchlist()[0], limit=2)
    return int(rows[-1]["ts"]) if rows else int(time.time())


def _anchor() -> int:
    ends = []
    for s in db.get_watchlist()[:5]:
        r = db.get_candles(s, limit=2)
        if r:
            ends.append(int(r[-1]["ts"]))
    return min(ends) if ends else int(time.time())


def _grid(family: str) -> list[dict[str, Any]]:
    """Candidate parameter sets per family. Small on purpose: every extra
    candidate multiplies the chance the train winner is noise."""
    if family == "ma":
        return [
            {"fast": 10, "slow": 30, "hold": 10, "stop": 2.0, "target": 3.0},
            {"fast": 20, "slow": 50, "hold": 10, "stop": 2.0, "target": 3.0},
            {"fast": 20, "slow": 60, "hold": 15, "stop": 2.5, "target": 4.0},
            {"fast": 50, "slow": 200, "hold": 20, "stop": 3.0, "target": 4.5},
        ]
    if family == "driver":
        return [
            {"lookback": 20, "min_corr": 0.4, "hold": 10},
            {"lookback": 40, "min_corr": 0.5, "hold": 10},
            {"lookback": 40, "min_corr": 0.6, "hold": 15},
            {"lookback": 60, "min_corr": 0.5, "hold": 20},
        ]
    if family == "reversion":
        out = []
        for lb in (10, 20):
            for z in (1.5, 2.0, 2.5):
                for h in (5, 10):
                    out.append({"lookback": lb, "z_entry": z, "hold": h,
                                "stop_atr": 2.5, "target_atr": 3.0})
        return out
    if family == "momentum_short":
        return [
            {"lookback": 5, "hold": 3, "min_move": 3.0},
            {"lookback": 10, "hold": 5, "min_move": 4.0},
            {"lookback": 10, "hold": 5, "min_move": 6.0},
            {"lookback": 20, "hold": 10, "min_move": 5.0},
        ]
    return []


FAMILIES: dict[str, Callable] = {
    "ma": H._ma_crossover,
    "driver": H._driver_sensitivity,
    "reversion": H._mean_reversion,
    "momentum_short": H._momentum_short,
}


def run_family(family: str, symbols: list[str], folds: int = 8,
               train_years: float = 2.0, test_months: float = 5.0,
               drivers: dict | None = None) -> dict[str, Any]:
    fn = FAMILIES[family]
    grid = _grid(family)
    now = _anchor()
    train_span = int(train_years * 365 * DAY)
    test_span = int(test_months * 30.4 * DAY)

    results = []
    picks: dict[str, int] = {}
    all_oos_returns: list[float] = []

    for f in range(folds):
        test_end = now - f * test_span
        test_start = test_end - test_span
        train_end, train_start = test_start, test_start - train_span
        if train_start <= 0:
            break

        best = None
        for params in grid:
            rets = H.evaluate(family, lambda d, **kw: fn(d, **kw), symbols, params,
                              train_start, train_end, extra={"drivers": drivers}
                              if family == "driver" else None)
            if len(rets) < 6:
                continue
            m = H._perf(rets, len(rets), f"train{params}")
            score = m.get("sharpe_per_trade", m.get("sharpe", 0.0))
            if best is None or score > best[0]:
                best = (score, params, m, rets)
        if best is None:
            continue

        _, params, train_m, _ = best
        key = str(params)
        picks[key] = picks.get(key, 0) + 1
        test_rets = H.evaluate(family, lambda d, **kw: fn(d, **kw), symbols, params,
                               test_start, test_end, extra={"drivers": drivers}
                               if family == "driver" else None)
        test_m = H._perf(test_rets, len(test_rets), f"test{params}")
        if test_rets:
            all_oos_returns.extend(test_rets)
        results.append({
            "fold": f + 1, "params": params,
            "train": train_m, "test": test_m,
        })

    oos = [r["test"] for r in results if r["test"].get("trades")]
    oos_trades = sum(m.get("trades", 0) for m in oos)
    oos_ret = [m["total_return_pct"] for m in oos if "total_return_pct" in m]
    oos_sh = [m.get("sharpe_per_trade", m.get("sharpe", 0.0)) for m in oos]
    in_sample = [r["train"] for r in results if r["train"].get("trades")]
    is_ret = [m["total_return_pct"] for m in in_sample if "total_return_pct" in m]

    fold_exps = [m["expectancy_per_trade"] for m in oos if "expectancy_per_trade" in m]
    trade_weighted = round(float(np.mean(all_oos_returns)), 3) if all_oos_returns else None
    fold_mean = round(float(np.mean(fold_exps)), 3) if fold_exps else None
    fold_median = round(float(np.median(fold_exps)), 3) if fold_exps else None
    profitable_folds = sum(1 for exp in fold_exps if exp > 0)

    return {
        "family": family,
        "symbols": len(symbols),
        "folds": len(results),
        "grid_size": len(grid),
        "results": results,
        "param_picks": picks,
        "in_sample_avg": round(sum(is_ret) / len(is_ret), 2) if is_ret else None,
        "oos": {
            "trades": oos_trades,
            "expectancy_trade_weighted": trade_weighted,
            "expectancy_fold_mean": fold_mean,
            "expectancy_fold_median": fold_median,
            "profitable_folds": profitable_folds,
            "folds_with_data": len(fold_exps),
            "avg_return_pct": round(sum(oos_ret) / len(oos_ret), 2) if oos_ret else None,
            "avg_sharpe": round(sum(oos_sh) / len(oos_sh), 2) if oos_sh else None,
        },
    }


def run_fixed_sweep(symbols: list[str], folds: int = 8,
                    test_months: float = 5.0) -> list[dict[str, Any]]:
    """Etap B: wrażliwość przy stałych parametrach (bez selekcji) na tych samych oknach OOS."""
    now = _anchor()
    test_span = int(test_months * 30.4 * DAY)

    variants = [
        {"label": "baza (20, z2.0, h10, SL2.5, TP3.0)", "params": {"lookback": 20, "z_entry": 2.0, "hold": 10, "stop_atr": 2.5, "target_atr": 3.0}},
        {"label": "lookback=15", "params": {"lookback": 15, "z_entry": 2.0, "hold": 10, "stop_atr": 2.5, "target_atr": 3.0}},
        {"label": "lookback=30", "params": {"lookback": 30, "z_entry": 2.0, "hold": 10, "stop_atr": 2.5, "target_atr": 3.0}},
        {"label": "z_entry=1.75", "params": {"lookback": 20, "z_entry": 1.75, "hold": 10, "stop_atr": 2.5, "target_atr": 3.0}},
        {"label": "z_entry=2.25", "params": {"lookback": 20, "z_entry": 2.25, "hold": 10, "stop_atr": 2.5, "target_atr": 3.0}},
        {"label": "hold=5", "params": {"lookback": 20, "z_entry": 2.0, "hold": 5, "stop_atr": 2.5, "target_atr": 3.0}},
        {"label": "hold=15", "params": {"lookback": 20, "z_entry": 2.0, "hold": 15, "stop_atr": 2.5, "target_atr": 3.0}},
        {"label": "stop_atr=2.0", "params": {"lookback": 20, "z_entry": 2.0, "hold": 10, "stop_atr": 2.0, "target_atr": 3.0}},
        {"label": "stop_atr=3.0", "params": {"lookback": 20, "z_entry": 2.0, "hold": 10, "stop_atr": 3.0, "target_atr": 3.0}},
        {"label": "target_atr=2.0", "params": {"lookback": 20, "z_entry": 2.0, "hold": 10, "stop_atr": 2.5, "target_atr": 2.0}},
        {"label": "target_atr=4.0", "params": {"lookback": 20, "z_entry": 2.0, "hold": 10, "stop_atr": 2.5, "target_atr": 4.0}},
        {"label": "koszty 1.5x (prowizja 0.52%, slip 0.15%)", "params": {"lookback": 20, "z_entry": 2.0, "hold": 10, "stop_atr": 2.5, "target_atr": 3.0, "commission_pct": config.COMMISSION_PCT * 1.5, "slippage_pct": config.SLIPPAGE_PCT * 1.5}},
    ]

    sweep_results = []
    gpw_symbols = [s for s in symbols if s.endswith(".WA")]
    us_symbols = [s for s in symbols if not s.endswith(".WA")]

    print("\n=== ETAP B: Badanie wrażliwości bazy (Fixed Sweep OOS) ===")
    print(f"{'Wariant':<42} | {'Trans':>5} | {'Exp OOS':>8} | {'Mediana':>7} | {'Fałdy >0':>8} | {'Najgorszy':>9} | {'GPW':>7} | {'US':>7}")
    print("-" * 115)

    for v in variants:
        all_rets: list[float] = []
        gpw_rets: list[float] = []
        us_rets: list[float] = []
        fold_exps: list[float] = []

        for f in range(folds):
            test_end = now - f * test_span
            test_start = test_end - test_span
            if test_start <= 0:
                break
            rets = H.evaluate("reversion", lambda d, **kw: H._mean_reversion(d, **kw),
                              symbols, v["params"], test_start, test_end)
            if rets:
                all_rets.extend(rets)
                fold_exps.append(float(np.mean(rets)))
            else:
                fold_exps.append(0.0)

            if gpw_symbols:
                g_rets = H.evaluate("reversion", lambda d, **kw: H._mean_reversion(d, **kw),
                                    gpw_symbols, v["params"], test_start, test_end)
                gpw_rets.extend(g_rets)
            if us_symbols:
                u_rets = H.evaluate("reversion", lambda d, **kw: H._mean_reversion(d, **kw),
                                    us_symbols, v["params"], test_start, test_end)
                us_rets.extend(u_rets)

        tw = round(float(np.mean(all_rets)), 3) if all_rets else 0.0
        med = round(float(np.median(fold_exps)), 3) if fold_exps else 0.0
        prof = sum(1 for e in fold_exps if e > 0)
        worst = round(float(min(fold_exps)), 3) if fold_exps else 0.0
        gpw_tw = round(float(np.mean(gpw_rets)), 3) if gpw_rets else 0.0
        us_tw = round(float(np.mean(us_rets)), 3) if us_rets else 0.0

        print(f"{v['label']:<42} | {len(all_rets):>5} | {tw:>7.3f}% | {med:>6.3f}% | "
              f"{f'{prof}/{len(fold_exps)}':>8} | {worst:>8.3f}% | {gpw_tw:>6.3f}% | {us_tw:>6.3f}%")

        sweep_results.append({
            "label": v["label"],
            "params": v["params"],
            "trades": len(all_rets),
            "expectancy_trade_weighted": tw,
            "expectancy_median": med,
            "profitable_folds": f"{prof}/{len(fold_exps)}",
            "worst_fold": worst,
            "gpw_expectancy": gpw_tw,
            "us_expectancy": us_tw,
        })
    return sweep_results


def run_us_reversion_gate(symbols: list[str], folds: int = 8,
                          test_months: float = 5.0,
                          target_profile: str = "IBKR_TIERED_USD",
                          capital: float = 1350.0,  # as in gate v2: notional per slot = capital / slots
                          slots: int = 8,
                          n_bootstrap: int = 2000) -> dict[str, Any]:
    """Faza 0: Rygorystyczna bramka walidacyjna OOS dla US Reversion z block bootstrapem."""
    now = _anchor()
    test_span = int(test_months * 30.4 * DAY)
    us_symbols = [s for s in symbols if not s.endswith(".WA")]
    real_notional = float(capital) / float(slots)

    print("\n" + "=" * 85)
    print("=== FAZA 0: BRAMKA WALIDACYJNA DLA US REVERSION (WALK-FORWARD OOS) ===")
    print("=" * 85)
    print(f"Uniwersum US: {len(us_symbols)} instrumentów | Fałdy: {folds} | Okno OOS: {test_months} mies.")
    print(f"Docelowy profil brokera: {target_profile} | Realny bilet: ${real_notional:,.2f} (${capital:,.2f} / {slots} slotów)")
    print("Parametry bazy: lookback=20, z_entry=2.0, hold=10, stop_atr=2.5, target_atr=3.0, min_stretch_atr=1.0")
    print("-" * 85)

    raw_trades = []
    fold_trade_counts = {f: 0 for f in range(1, folds + 1)}

    for f in range(folds):
        fold_idx = f + 1
        test_end = now - f * test_span
        test_start = test_end - test_span
        if test_start <= 0:
            break

        for sym in us_symbols:
            df = bt._cached_frame(sym)
            if df is None or len(df) < 60:
                continue
            # Utnij historię na test_end, aby nie było lookahead biasu
            df_slice = df[df["ts"] <= test_end].reset_index(drop=True)
            if len(df_slice) < 40:
                continue
            trades = reversion.simulate(
                df_slice,
                lookback=20,
                z_entry=2.0,
                hold=10,
                stop_atr=2.5,
                target_atr=3.0,
                min_stretch_atr=1.0,
                broker_profile=target_profile,
                notional=real_notional,
                symbol=sym,
                as_trades=True,
            )
            for t in trades:
                if test_start <= t.entry_ts < test_end:
                    raw_trades.append({
                        "symbol": sym,
                        "entry_ts": t.entry_ts,
                        "exit_ts": t.exit_ts,
                        "entry_px": t.entry_px,
                        "exit_px": t.exit_px,
                        "reason": t.reason,
                        "fold": fold_idx,
                    })
                    fold_trade_counts[fold_idx] += 1

    print(f"Łącznie zebrano transakcji OOS na US: {len(raw_trades)}")
    for f in range(1, folds + 1):
        print(f"  Fałd {f}: {fold_trade_counts[f]} transakcji")

    profiles_to_test = [target_profile, "XTB_PLN", "CONSERVATIVE_US"]
    seen = set()
    test_profiles = []
    for p in profiles_to_test:
        if p not in seen:
            seen.add(p)
            test_profiles.append(p)

    profile_results = {}

    for prof_name in test_profiles:
        prof = costs.get_profile(prof_name)
        trade_returns = []
        weeks_map: dict[int, list[float]] = {}
        fold_returns: dict[int, list[float]] = {f: [] for f in range(1, folds + 1)}

        for rt in raw_trades:
            ret = costs.net_return_pct(
                rt["entry_px"],
                rt["exit_px"],
                profile=prof,
                notional=real_notional,
                symbol=rt["symbol"],
            )
            trade_returns.append(ret)
            w = rt["entry_ts"] // (7 * DAY)
            weeks_map.setdefault(w, []).append(ret)
            fold_returns[rt["fold"]].append(ret)

        unique_weeks = list(weeks_map.keys())
        boot_means = []
        if unique_weeks and trade_returns:
            np.random.seed(42)
            for _ in range(n_bootstrap):
                sampled_weeks = np.random.choice(unique_weeks, size=len(unique_weeks), replace=True)
                sample_rets = []
                for sw in sampled_weeks:
                    sample_rets.extend(weeks_map[sw])
                if sample_rets:
                    boot_means.append(float(np.mean(sample_rets)))

        ci_lower = round(float(np.percentile(boot_means, 5.0)), 3) if boot_means else 0.0
        ci_upper = round(float(np.percentile(boot_means, 95.0)), 3) if boot_means else 0.0
        boot_std = round(float(np.std(boot_means)), 3) if boot_means else 0.0
        pt_mean = round(float(np.mean(trade_returns)), 3) if trade_returns else 0.0
        pt_median = round(float(np.median(trade_returns)), 3) if trade_returns else 0.0

        profitable_folds = 0
        fold_details = []
        for f in range(1, folds + 1):
            f_rets = fold_returns[f]
            n_t = len(f_rets)
            f_exp = round(float(np.mean(f_rets)), 3) if f_rets else 0.0
            is_prof = (n_t >= 25) and (f_exp > 0.0)
            if is_prof:
                profitable_folds += 1
            fold_details.append({
                "fold": f,
                "trades": n_t,
                "expectancy": f_exp,
                "profitable": is_prof,
            })

        profile_results[prof_name] = {
            "profile": prof_name,
            "trades": len(trade_returns),
            "expectancy": pt_mean,
            "median_return": pt_median,
            "ci_90": [ci_lower, ci_upper],
            "bootstrap_se": boot_std,
            "profitable_folds": profitable_folds,
            "total_folds": folds,
            "fold_details": fold_details,
        }

    target_res = profile_results[target_profile]
    conserv_res = profile_results.get("CONSERVATIVE_US", target_res)

    crit_ci = target_res["ci_90"][0] > 0.10
    crit_folds = target_res["profitable_folds"] >= 5
    crit_conserv = conserv_res["expectancy"] >= 0.0

    gate_pass = crit_ci and crit_folds and crit_conserv
    gate_decision = "GO" if gate_pass else "NO-GO"

    print("\n" + "=" * 85)
    print(f"{'Profil Brokera':<22} | {'Trans':>5} | {'Exp OOS':>8} | {'90% CI (Block)':>22} | {'Fałdy (N≥25 & >0)':>18}")
    print("-" * 85)
    for p_name, r in profile_results.items():
        ci_str = f"[{r['ci_90'][0]:+.3f}%, {r['ci_90'][1]:+.3f}%]"
        print(f"{p_name:<22} | {r['trades']:>5} | {r['expectancy']:>7.3f}% | {ci_str:>22} | {r['profitable_folds']:>7}/{r['total_folds']}")

    print("\n=== KRYTERIA BRAMKI WALIDACYJNEJ ===")
    print(f"1. 90% CI Dolna granica > +0.10%:   {target_res['ci_90'][0]:+.3f}%  -> {'PASSED' if crit_ci else 'FAILED'}")
    print(f"2. Zyskowne fałdy (N≥25) >= 5/8:    {target_res['profitable_folds']}/{target_res['total_folds']}       -> {'PASSED' if crit_folds else 'FAILED'}")
    print(f"3. Stress test (CONSERVATIVE_US) >= 0.0%: {conserv_res['expectancy']:+.3f}% -> {'PASSED' if crit_conserv else 'FAILED'}")
    print(f"\n>>> WERDYKT BRAMKI: {gate_decision} <<<")
    print("=" * 85)

    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "target_profile": target_profile,
        "gate_decision": gate_decision,
        "criteria": {
            "ci_lower_above_010": {"value": target_res["ci_90"][0], "passed": crit_ci},
            "profitable_folds_min_5": {"value": target_res["profitable_folds"], "passed": crit_folds},
            "conservative_exp_above_0": {"value": conserv_res["expectancy"], "passed": crit_conserv},
        },
        "profiles": profile_results,
    }

    reports_dir = config.DATA_DIR.parent / "reports" if hasattr(config, "DATA_DIR") else Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / f"gate_us_reversion_{time.strftime('%Y%m%d_%H%M%S')}_{target_profile}.json"
    with open(report_file, "w") as f:
        json.dump(report, f, default=str, indent=2)
    print(f"Zapisano raport bramki: {report_file}\n")

    return report


def _calc_spy_benchmark(start_ts: int, end_ts: int) -> dict[str, float]:
    """Calculate SPY Buy & Hold benchmark return for interval."""
    df = bt._cached_frame("SPY", limit=6000)
    if df is None or len(df) < 10:
        return {"return_pct": 0.0, "cagr": 0.0, "mdd_pct": 0.0}
    df = df[(df["ts"] >= start_ts) & (df["ts"] <= end_ts)].sort_values("ts")
    if len(df) < 2:
        return {"return_pct": 0.0, "cagr": 0.0, "mdd_pct": 0.0}
    c_start = float(df["close"].iloc[0])
    c_end = float(df["close"].iloc[-1])
    ret = ((c_end / c_start) - 1.0) * 100.0
    days = len(df)
    cagr = (((c_end / c_start) ** (252.0 / max(days, 1))) - 1.0) * 100.0 if c_end > 0 else 0.0

    closes = df["close"].values.astype(float)
    peak = np.maximum.accumulate(closes)
    mdd = float(np.min((closes - peak) / peak)) * 100.0
    return {"return_pct": round(ret, 2), "cagr": round(cagr, 2), "mdd_pct": round(mdd, 2)}


def _run_reconciliation_test() -> bool:
    """Kryterium 6: uruchamia test uzgodnienia silników (niezależnie od bieżącego katalogu)."""
    res = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests" / "test_portfolio_sim.py"),
         "-k", "reconciliation", "-q"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if res.returncode != 0:
        print(f"OSTRZEŻENIE: Test uzgodnienia silników (pytest reconciliation) zwrócił błąd (kod {res.returncode}):")
        print(res.stdout)
        print(res.stderr)
    return res.returncode == 0


def run_us_reversion_gate_v2(
    symbols: list[str],
    target_profile: str = GATE_V2_DEFAULT_PROFILE,
    capital: float = 1350.0,
    slots: int = 8,
    n_bootstrap: int = 2000,
    seed: int = 42,
) -> dict[str, Any]:
    """Bramka walidacyjna v2 na pełnej historii (15+ lat).

    Kryteria:
    1. Pełny okres: dolna granica 90% CI (bloki miesięczne) > +0,10%.
    2. Holdout: wejścia przed 2021-10-01 — expectancy > 0 i dolna granica 90% CI > -0,25%.
    3. >= 60% fałdów rocznych zyskownych (fałd z N < 25 = niezyskowny).
    4. CONSERVATIVE_US na pełnym okresie: expectancy >= 0.
    5. Symulacja portfela (8 slotów, $1 350, XTB_USD, ułamkowe): żaden pełny rok kalendarzowy ze stratą > 15%.
    6. Test uzgodnienia z pkt 4 zielony.
    """
    us_symbols = [s for s in symbols if not s.endswith(".WA")]
    real_notional = float(capital) / float(slots)

    print("\n" + "=" * 90)
    print("=== BRAMKA WALIDACYJNA V2: US REVERSION (DŁUGA HISTORIA 15+ LAT) ===")
    print("=" * 90)
    print(f"Liczba instrumentów US: {len(us_symbols)}")
    print(f"Docelowy profil: {target_profile} | Realny bilet (notional): ${real_notional:,.2f} (${capital:,.2f} / {slots} slotów)")
    print(f"Ustawienia bootstrapu: {n_bootstrap} iteracji, seed {seed}, bloki miesięczne (+ tygodniowe)")
    print("-" * 90)

    # 1. Daty pierwszego bara per symbol
    print("Daty i zasięg historii per symbol:")
    symbol_data: dict[str, pd.DataFrame] = {}
    first_bars_info: list[dict[str, Any]] = []

    for sym in sorted(us_symbols):
        df = bt._cached_frame(sym, limit=6000)
        if df is None or len(df) < 60:
            print(f"  {sym:6s}: BRAK DANYCH (wymagane min 60 barów)")
            continue
        ts_min = int(df["ts"].min())
        ts_max = int(df["ts"].max())
        dt_min = datetime.datetime.fromtimestamp(ts_min, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        dt_max = datetime.datetime.fromtimestamp(ts_max, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        # Bary odrzucone przez ensure_daily_bars (miesięczne / śróddzienne / duplikaty dat)
        raw_df = signals.load_frame(sym, limit=6000)
        dropped_bars = max(0, len(raw_df) - len(signals.ensure_daily_bars(raw_df))) if raw_df is not None else 0
        first_bars_info.append({
            "symbol": sym, "bars": len(df), "min_ts": ts_min, "max_ts": ts_max,
            "min_date": dt_min, "max_date": dt_max, "dropped_bars": dropped_bars,
        })
        symbol_data[sym] = df
        print(f"  {sym:6s}: {len(df):4d} barów | od {dt_min} do {dt_max} | odrzucone bary: {dropped_bars}")

    current_year = datetime.datetime.now(datetime.timezone.utc).year
    # Reguła roku startowego: fałdy roczne obejmują każdy rok od pierwszego, w którym co najmniej
    # jeden symbol ma >= 60 barów przed 1 stycznia, do roku bieżącego. Transakcje symbolu liczą się
    # dopiero od roku, w którym ten symbol sam spełnia warunek >= 60 barów przed 1 stycznia.
    sym_start_year: dict[str, int] = {}
    for sym, df in symbol_data.items():
        for y in range(1995, current_year + 1):
            jan1_ts = int(datetime.datetime(y, 1, 1, tzinfo=datetime.timezone.utc).timestamp())
            if len(df[df["ts"] < jan1_ts]) >= 60:
                sym_start_year[sym] = y
                break

    start_year = min(sym_start_year.values()) if sym_start_year else current_year
    annual_folds = list(range(start_year, current_year + 1))
    print(f"\nWyznaczone fałdy roczne ({len(annual_folds)} fałdów, pierwszy rok z ≥1 symbolem o ≥60 barów przed 1 stycznia): {annual_folds[0]}..{annual_folds[-1]}")

    # 2. Transakcje na pełnej historii (bez sztucznego ucinania df), generowane osobno dla
    # każdego profilu: stop/target są kotwiczone na poślizgu danego profilu, więc zestaw
    # transakcji różni się między profilami.
    etf_set = {"SPY", "QQQ", "IWM", "EEM", "EFA", "EWZ", "VGK"}

    profiles_to_test = [target_profile, "XTB_PLN", "CONSERVATIVE_US"]
    seen = set()
    test_profiles = [p for p in profiles_to_test if not (p in seen or seen.add(p))]

    def _profile_trades(prof_name: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sym, df in symbol_data.items():
            trades = reversion.simulate(
                df,
                lookback=20,
                z_entry=2.0,
                hold=10,
                stop_atr=2.5,
                target_atr=3.0,
                min_stretch_atr=1.0,
                broker_profile=prof_name,
                notional=real_notional,
                symbol=sym,
                as_trades=True,
            )
            for t in trades:
                e_year = datetime.datetime.fromtimestamp(t.entry_ts, tz=datetime.timezone.utc).year
                if e_year >= sym_start_year.get(sym, current_year + 1):
                    out.append({
                        "symbol": sym,
                        "entry_ts": t.entry_ts,
                        "exit_ts": t.exit_ts,
                        "entry_px": t.entry_px,
                        "exit_px": t.exit_px,
                        "reason": t.reason,
                        "year": e_year,
                        "is_etf": sym in etf_set,
                    })
        out.sort(key=lambda t: t["entry_ts"])
        return out

    # 3. Ewaluacja profili kosztowych
    profile_results = {}
    holdout_cutoff_ts = int(datetime.datetime(2021, 10, 1, tzinfo=datetime.timezone.utc).timestamp())

    for prof_name in test_profiles:
        prof = costs.get_profile(prof_name)
        raw_trades = _profile_trades(prof_name)
        print(f"Profil {prof_name}: transakcji na US w latach {start_year}..{current_year}: {len(raw_trades)}")
        trade_returns = []
        months_map: dict[str, list[float]] = {}
        weeks_map: dict[int, list[float]] = {}
        year_returns: dict[int, list[float]] = {y: [] for y in annual_folds}
        etf_year_returns: dict[int, list[float]] = {y: [] for y in annual_folds}
        stock_year_returns: dict[int, list[float]] = {y: [] for y in annual_folds}
        holdout_returns: list[float] = []
        holdout_months_map: dict[str, list[float]] = {}

        for rt in raw_trades:
            ret = costs.net_return_pct(
                rt["entry_px"],
                rt["exit_px"],
                profile=prof,
                notional=real_notional,
                symbol=rt["symbol"],
            )
            trade_returns.append(ret)
            m_key = datetime.datetime.fromtimestamp(rt["entry_ts"], tz=datetime.timezone.utc).strftime("%Y-%m")
            w_key = rt["entry_ts"] // (7 * DAY)
            months_map.setdefault(m_key, []).append(ret)
            weeks_map.setdefault(w_key, []).append(ret)

            year_returns[rt["year"]].append(ret)
            if rt["is_etf"]:
                etf_year_returns[rt["year"]].append(ret)
            else:
                stock_year_returns[rt["year"]].append(ret)

            if rt["entry_ts"] < holdout_cutoff_ts:
                holdout_returns.append(ret)
                holdout_months_map.setdefault(m_key, []).append(ret)

        # Monthly Block Bootstrap (2000 iteracji, seed 42)
        unique_months = list(months_map.keys())
        boot_means_m = []
        if unique_months and trade_returns:
            np.random.seed(seed)
            for _ in range(n_bootstrap):
                sampled_months = np.random.choice(unique_months, size=len(unique_months), replace=True)
                sample_rets = [r for m in sampled_months for r in months_map[m]]
                if sample_rets:
                    boot_means_m.append(float(np.mean(sample_rets)))

        ci_m_lower = round(float(np.percentile(boot_means_m, 5.0)), 3) if boot_means_m else 0.0
        ci_m_upper = round(float(np.percentile(boot_means_m, 95.0)), 3) if boot_means_m else 0.0

        # Weekly Block Bootstrap (seed 42)
        unique_weeks = list(weeks_map.keys())
        boot_means_w = []
        if unique_weeks and trade_returns:
            np.random.seed(seed)
            for _ in range(n_bootstrap):
                sampled_weeks = np.random.choice(unique_weeks, size=len(unique_weeks), replace=True)
                sample_rets = [r for w in sampled_weeks for r in weeks_map[w]]
                if sample_rets:
                    boot_means_w.append(float(np.mean(sample_rets)))

        ci_w_lower = round(float(np.percentile(boot_means_w, 5.0)), 3) if boot_means_w else 0.0
        ci_w_upper = round(float(np.percentile(boot_means_w, 95.0)), 3) if boot_means_w else 0.0

        # Holdout monthly block bootstrap
        unique_h_months = list(holdout_months_map.keys())
        boot_h_means = []
        if unique_h_months and holdout_returns:
            np.random.seed(seed)
            for _ in range(n_bootstrap):
                sampled_months = np.random.choice(unique_h_months, size=len(unique_h_months), replace=True)
                sample_rets = [r for m in sampled_months for r in holdout_months_map[m]]
                if sample_rets:
                    boot_h_means.append(float(np.mean(sample_rets)))

        holdout_exp = round(float(np.mean(holdout_returns)), 3) if holdout_returns else 0.0
        holdout_ci_lower = round(float(np.percentile(boot_h_means, 5.0)), 3) if boot_h_means else 0.0
        holdout_ci_upper = round(float(np.percentile(boot_h_means, 95.0)), 3) if boot_h_means else 0.0

        # Annual folds stats
        annual_stats = []
        profitable_folds = 0
        for y in annual_folds:
            y_rets = year_returns[y]
            n_y = len(y_rets)
            exp_y = round(float(np.mean(y_rets)), 3) if y_rets else 0.0
            win_rate_y = round(sum(1 for r in y_rets if r > 0) / n_y * 100.0, 1) if n_y else 0.0
            is_prof = (n_y >= 25) and (exp_y > 0.0)
            if is_prof:
                profitable_folds += 1

            etf_rets = etf_year_returns[y]
            stock_rets = stock_year_returns[y]
            etf_exp = round(float(np.mean(etf_rets)), 3) if etf_rets else 0.0
            stock_exp = round(float(np.mean(stock_rets)), 3) if stock_rets else 0.0

            annual_stats.append({
                "year": y,
                "trades": n_y,
                "expectancy": exp_y,
                "win_rate": win_rate_y,
                "profitable": is_prof,
                "etf_trades": len(etf_rets),
                "etf_expectancy": etf_exp,
                "stock_trades": len(stock_rets),
                "stock_expectancy": stock_exp,
            })

        profile_results[prof_name] = {
            "profile": prof_name,
            "trades": len(trade_returns),
            "expectancy": round(float(np.mean(trade_returns)), 3) if trade_returns else 0.0,
            "median_return": round(float(np.median(trade_returns)), 3) if trade_returns else 0.0,
            "ci_90_monthly": [ci_m_lower, ci_m_upper],
            "ci_90_weekly": [ci_w_lower, ci_w_upper],
            "holdout": {
                "trades": len(holdout_returns),
                "expectancy": holdout_exp,
                "ci_90_monthly": [holdout_ci_lower, holdout_ci_upper],
            },
            "profitable_annual_folds": profitable_folds,
            "total_annual_folds": len(annual_folds),
            "annual_stats": annual_stats,
        }

    # 4. Symulacja portfela dla kryterium 5
    print("\nUruchamiam symulację portfela dla kryterium 5 (8 slotów, $1 350, XTB_USD, ułamkowe)...")
    sim = PortfolioSimulator(
        symbols=us_symbols,
        initial_cash=capital,
        max_slots=slots,
        broker_profile=target_profile,
        fractional=True,
    )
    port_res = sim.run()
    curve = port_res.get("curve", [])
    curve_by_year: dict[int, list[dict]] = {}
    for pt in curve:
        y = datetime.datetime.fromtimestamp(pt["ts"], tz=datetime.timezone.utc).year
        curve_by_year.setdefault(y, []).append(pt)

    full_years = [y for y in annual_folds if y < current_year and y in curve_by_year]
    portfolio_annual_returns: dict[int, dict[str, Any]] = {}
    worst_full_year_ret = 0.0

    prev_eod_equity = capital
    for y in sorted(curve_by_year.keys()):
        pts = curve_by_year[y]
        start_eq = prev_eod_equity
        end_eq = float(pts[-1]["equity"])
        y_ret = ((end_eq / start_eq) - 1.0) * 100.0
        prev_eod_equity = end_eq

        first_ts = pts[0]["ts"]
        last_ts = pts[-1]["ts"]
        spy_bm = _calc_spy_benchmark(first_ts, last_ts)

        portfolio_annual_returns[y] = {
            "year": y,
            "start_equity": round(start_eq, 2),
            "end_equity": round(end_eq, 2),
            "return_pct": round(y_ret, 2),
            "spy_return_pct": spy_bm["return_pct"],
            "is_full_year": y < current_year,
        }
        if y < current_year and y_ret < worst_full_year_ret:
            worst_full_year_ret = y_ret

    # 5. Ewaluacja 6 kryteriów
    target_res = profile_results[target_profile]
    conserv_res = profile_results.get("CONSERVATIVE_US", target_res)

    crit_1 = target_res["ci_90_monthly"][0] > 0.10
    crit_2 = (target_res["holdout"]["expectancy"] > 0.0) and (target_res["holdout"]["ci_90_monthly"][0] > -0.25)
    pct_prof_folds = (target_res["profitable_annual_folds"] / max(1, target_res["total_annual_folds"])) * 100.0
    crit_3 = pct_prof_folds >= 60.0
    crit_4 = conserv_res["expectancy"] >= 0.0
    crit_5 = all(portfolio_annual_returns[y]["return_pct"] >= -15.0 for y in full_years)
    crit_6 = _run_reconciliation_test()

    gate_v2_pass = crit_1 and crit_2 and crit_3 and crit_4 and crit_5 and crit_6
    gate_decision = "GO" if gate_v2_pass else "NO-GO"

    # 6. Wypisanie tabel wyników
    print("\n" + "=" * 90)
    print("=== WYNIKI WEDŁUG PROFILI KOSZTOWYCH (PEŁNY OKRES) ===")
    print(f"{'Profil':<18} | {'Trans':>5} | {'Exp':>8} | {'90% CI (Mies.)':>22} | {'90% CI (Tyg.)':>22}")
    print("-" * 90)
    for p_name, r in profile_results.items():
        ci_m = f"[{r['ci_90_monthly'][0]:+.3f}%, {r['ci_90_monthly'][1]:+.3f}%]"
        ci_w = f"[{r['ci_90_weekly'][0]:+.3f}%, {r['ci_90_weekly'][1]:+.3f}%]"
        print(f"{p_name:<18} | {r['trades']:>5} | {r['expectancy']:>7.3f}% | {ci_m:>22} | {ci_w:>22}")

    print("\n" + "=" * 90)
    print(f"=== WYNIKI ROK PO ROKU ({target_profile}) — ROZBICIE ETF VS AKCJE ORAZ PORTFEL VS SPY ===")
    print(f"{'Rok':<5} | {'Trans':>5} | {'Exp All':>8} | {'ETF(Tr/Exp)':>15} | {'Akcje(Tr/Exp)':>16} | {'Portfel':>9} | {'SPY B&H':>9}")
    print("-" * 90)
    for st in target_res["annual_stats"]:
        y = st["year"]
        p_row = portfolio_annual_returns.get(y, {"return_pct": 0.0, "spy_return_pct": 0.0})
        etf_str = f"{st['etf_trades']:>2}/{st['etf_expectancy']:>+6.2f}%"
        stock_str = f"{st['stock_trades']:>3}/{st['stock_expectancy']:>+6.2f}%"
        print(f"{y:<5} | {st['trades']:>5} | {st['expectancy']:>+7.3f}% | {etf_str:>15} | {stock_str:>16} | {p_row['return_pct']:>+8.2f}% | {p_row['spy_return_pct']:>+8.2f}%")

    print("\n" + "=" * 90)
    print("=== WERDYKT BRAMKI V2 (6 KRYTERIÓW) ===")
    print("=" * 90)
    ci_m_str = f"{target_res['ci_90_monthly'][0]:+.3f}% (próg > +0.10%)"
    print(f"1. Pełny okres 90% CI dolna (mies.):    {ci_m_str:<32} -> {'PASSED' if crit_1 else 'FAILED'}")

    h_exp_str = f"exp={target_res['holdout']['expectancy']:+.3f}%, CI_min={target_res['holdout']['ci_90_monthly'][0]:+.3f}%"
    print(f"2. Holdout (<2021-10):                 {h_exp_str:<32} -> {'PASSED' if crit_2 else 'FAILED'}")

    folds_str = f"{target_res['profitable_annual_folds']}/{target_res['total_annual_folds']} ({pct_prof_folds:.1f}%, próg >= 60%)"
    print(f"3. Zyskowne fałdy roczne (N>=25):      {folds_str:<32} -> {'PASSED' if crit_3 else 'FAILED'}")

    conserv_str = f"{conserv_res['expectancy']:+.3f}% (próg >= 0.0%)"
    print(f"4. Stress test (CONSERVATIVE_US):      {conserv_str:<32} -> {'PASSED' if crit_4 else 'FAILED'}")

    port_loss_str = f"najgorszy pełny rok: {worst_full_year_ret:+.2f}% (próg >= -15.0%)"
    print(f"5. Portfel (max strata roku <= 15%):   {port_loss_str:<32} -> {'PASSED' if crit_5 else 'FAILED'}")

    print(f"6. Test uzgodnienia silnika (pkt 4):   identyczna lista transakcji 1:1 -> {'PASSED' if crit_6 else 'FAILED'}")

    print("\n" + "-" * 90)
    print(f">>> OSTATECZNY WERDYKT BRAMKI V2: {gate_decision} <<<")
    print("=" * 90 + "\n")

    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "target_profile": target_profile,
        "capital": capital,
        "slots": slots,
        "notional": real_notional,
        "gate_decision": gate_decision,
        "criteria": {
            "1_full_period_ci_lower_above_010": {"value": target_res["ci_90_monthly"][0], "passed": crit_1},
            "2_holdout_exp_positive_and_ci_lower_above_minus025": {
                "expectancy": target_res["holdout"]["expectancy"],
                "ci_lower": target_res["holdout"]["ci_90_monthly"][0],
                "passed": crit_2,
            },
            "3_profitable_annual_folds_pct": {
                "profitable": target_res["profitable_annual_folds"],
                "total": target_res["total_annual_folds"],
                "pct": pct_prof_folds,
                "passed": crit_3,
            },
            "4_conservative_exp_above_0": {"value": conserv_res["expectancy"], "passed": crit_4},
            "5_portfolio_worst_full_year_above_minus15": {"worst_full_year_return": worst_full_year_ret, "passed": crit_5},
            "6_reconciliation_test_passed": {"passed": crit_6},
        },
        "profiles": profile_results,
        "portfolio_annual_returns": portfolio_annual_returns,
        "portfolio_summary": {
            "initial_cash": port_res["initial_cash"],
            "final_equity": port_res["final_equity"],
            "total_return_pct": port_res["total_return_pct"],
            "cagr": port_res["cagr"],
            "max_drawdown_pct": port_res["max_drawdown_pct"],
            "sharpe_rf0": port_res["sharpe_rf0"],
            "win_rate": port_res["win_rate"],
            "profit_factor": port_res["profit_factor"],
            "expectancy_per_trade": port_res["expectancy_per_trade"],
            "rejected_signals": port_res["rejected_signals"],
        },
        "first_bars_info": first_bars_info,
    }

    reports_dir = config.DATA_DIR.parent / "reports" if hasattr(config, "DATA_DIR") else Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / f"gate_v2_{time.strftime('%Y%m%d_%H%M%S')}_{target_profile}_{slots}slots.json"
    with open(report_file, "w") as f:
        json.dump(report, f, default=str, indent=2)
    print(f"Zapisano raport bramki v2: {report_file}\n")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward analysis of strategy families.")
    parser.add_argument("--folds", type=int, default=8, help="Number of walk-forward folds (default: 8)")
    parser.add_argument("--train-years", type=float, default=2.0, help="Years in train window (default: 2.0)")
    parser.add_argument("--test-months", type=float, default=5.0, help="Months in test window (default: 5.0)")
    parser.add_argument("--families", nargs="*", default=["ma", "driver", "reversion", "momentum_short"],
                        help="Families to test (default: all)")
    parser.add_argument("--fetch", action="store_true", help="Pobierz świece z Yahoo Finance przed analizą")
    parser.add_argument("--range", default="10y", help="Zakres danych do pobrania (np. 5y, 10y, 20y, max)")
    parser.add_argument("--fixed-sweep", action="store_true", help="Uruchom badanie wrażliwości (Etap B) na sąsiadach bazy")
    parser.add_argument("--gate", action="store_true", help="Uruchom bramkę walidacyjną Fazy 0 dla US Reversion")
    parser.add_argument("--gate-v2", action="store_true", help="Uruchom bramkę walidacyjną v2 dla US Reversion na 15+ latach")
    parser.add_argument("--profile", default=None,
                        help=f"Docelowy profil brokera dla bramki (domyślnie: --gate -> {config.DEFAULT_BROKER_PROFILE_US}, "
                             f"--gate-v2 -> {GATE_V2_DEFAULT_PROFILE})")
    parser.add_argument("--capital", type=float, default=None, help="Kapitał początkowy portfela/bramki (USD)")
    parser.add_argument("--slots", type=int, default=None, help="Liczba slotów w portfelu")
    args = parser.parse_args()


    db.init_db()
    started = time.time()
    symbols = db.get_watchlist()

    # Automatyczne pobranie danych historycznych, jeśli baza świec jest pusta lub wymuszono flagą --fetch
    candle_count = db.get_conn().execute("SELECT COUNT(*) c FROM candles").fetchone()["c"]
    if args.fetch or candle_count < 1000:
        print(f"Pobieram świece ({args.range}) dla {len(symbols)} spółek...")
        asyncio.run(market.refresh_candles(symbols, rng=args.range))

    if args.gate_v2:
        gate_v2_kwargs: dict[str, Any] = {"target_profile": args.profile or GATE_V2_DEFAULT_PROFILE}
        if args.capital is not None:
            gate_v2_kwargs["capital"] = args.capital
        if args.slots is not None:
            gate_v2_kwargs["slots"] = args.slots
        run_us_reversion_gate_v2(symbols, **gate_v2_kwargs)
        return

    drivers = H.driver_series()
    print(f"Uniwersum: {len(symbols)} · sterowniki makro: {len(drivers)} "
          f"({', '.join(drivers)})")
    movers = H.liquid_movers(symbols)
    print(f"Liquid + istotny ruch (ADV≥progi, |20d ROC|≥3%): {len(movers)}")
    for s, adv, mv in movers[:8]:
        print(f"   {s:9} ADV {adv:>14,.0f}  20d {mv:+.1f}%")

    if args.gate:
        gate_kwargs: dict[str, Any] = {
            "folds": args.folds,
            "test_months": args.test_months,
            "target_profile": args.profile or config.DEFAULT_BROKER_PROFILE_US,
        }
        if args.capital is not None:
            gate_kwargs["capital"] = args.capital
        if args.slots is not None:
            gate_kwargs["slots"] = args.slots
        run_us_reversion_gate(symbols, **gate_kwargs)
        return

    out = []
    for fam in args.families:
        if fam not in FAMILIES:
            print(f"Nieznana rodzina: {fam}, pomijam.")
            continue
        print(f"\n=== {fam} ===")
        r = run_family(fam, symbols, folds=args.folds, train_years=args.train_years,
                       test_months=args.test_months, drivers=drivers)
        out.append(r)
        oos = r["oos"]
        print(f"  OOS transakcje: {oos['trades']}  zyskowne fałdy: "
              f"{oos['profitable_folds']}/{oos['folds_with_data']}  "
              f"Sharpe {oos['avg_sharpe']}")
        print(f"  expectancy trade-weighted: {oos['expectancy_trade_weighted']}%  "
              f"średnia fałdów: {oos['expectancy_fold_mean']}%  "
              f"mediana: {oos['expectancy_fold_median']}%")
        for fold in r["results"][:3]:
            t, te = fold["train"], fold["test"]
            print(f"    F{fold['fold']} train exp={t.get('expectancy_per_trade')}% "
                  f"sh={t.get('sharpe_per_trade', t.get('sharpe'))} | "
                  f"test exp={te.get('expectancy_per_trade')}% "
                  f"sh={te.get('sharpe_per_trade', te.get('sharpe'))} n={te.get('trades')}")

    fixed_sweep_results = []
    if args.fixed_sweep or "reversion" in args.families:
        fixed_sweep_results = run_fixed_sweep(symbols, folds=args.folds, test_months=args.test_months)

    print(f"\nŁączny czas: {round(time.time() - started, 1)}s")

    report_payload = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "folds": args.folds,
            "train_years": args.train_years,
            "test_months": args.test_months,
            "families": args.families,
        },
        "costs": {
            "commission_pct": config.COMMISSION_PCT,
            "slippage_pct": config.SLIPPAGE_PCT,
        },
        "results": out,
        "fixed_sweep": fixed_sweep_results,
    }

    reports_dir = config.DATA_DIR.parent / "reports" if hasattr(config, "DATA_DIR") else Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / f"hypotheses_{time.strftime('%Y%m%d_%H%M')}.json"
    with open(report_file, "w") as f:
        json.dump(report_payload, f, default=str, indent=2)
    print(f"Zapisano raport: {report_file}")


if __name__ == "__main__":
    main()
