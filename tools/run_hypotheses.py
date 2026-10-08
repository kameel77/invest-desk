"""Run every hypothesis family through the same anchored walk-forward.

The point is not to find a winner — the trend system already lost out-of-sample
at -2.02%, and a family that only looks good in-sample is worthless. Each
family gets tuned on the train window and scored on bars it never saw, exactly
like the original strategy did.

Run:  .venv/bin/python -m tools.run_hypotheses
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Any, Callable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import backtest as bt
from app import config, db, hypotheses as H

DAY = 86400


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
        return [
            {"lookback": 5, "z_entry": 1.5, "hold": 3},
            {"lookback": 10, "z_entry": 1.5, "hold": 5},
            {"lookback": 10, "z_entry": 2.0, "hold": 5},
            {"lookback": 20, "z_entry": 2.0, "hold": 10},
        ]
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward analysis of strategy families.")
    parser.add_argument("--folds", type=int, default=8, help="Number of walk-forward folds (default: 8)")
    parser.add_argument("--train-years", type=float, default=2.0, help="Years in train window (default: 2.0)")
    parser.add_argument("--test-months", type=float, default=5.0, help="Months in test window (default: 5.0)")
    parser.add_argument("--families", nargs="*", default=["ma", "driver", "reversion", "momentum_short"],
                        help="Families to test (default: all)")
    args = parser.parse_args()

    db.init_db()
    started = time.time()
    symbols = db.get_watchlist()
    drivers = H.driver_series()
    print(f"Uniwersum: {len(symbols)} · sterowniki makro: {len(drivers)} "
          f"({', '.join(drivers)})")
    movers = H.liquid_movers(symbols)
    print(f"Liquid + istotny ruch (ADV≥progi, |20d ROC|≥3%): {len(movers)}")
    for s, adv, mv in movers[:8]:
        print(f"   {s:9} ADV {adv:>14,.0f}  20d {mv:+.1f}%")

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
    }

    reports_dir = config.DATA_DIR.parent / "reports" if hasattr(config, "DATA_DIR") else Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / f"hypotheses_{time.strftime('%Y%m%d_%H%M')}.json"
    with open(report_file, "w") as f:
        json.dump(report_payload, f, default=str, indent=2)
    print(f"Zapisano raport: {report_file}")


if __name__ == "__main__":
    main()
