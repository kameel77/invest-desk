"""CLI tool to execute multi-asset portfolio simulations.

Usage:
    python3 tools/run_portfolio_sim.py --slots 5 --profile IBKR_TIERED_USD
    python3 tools/run_portfolio_sim.py --slots 5 --profile XTB_PLN
    python3 tools/run_portfolio_sim.py --slots 8 --cash 25000
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import backtest as bt
from app import config, db
from app.portfolio_sim import PortfolioSimulator


def _parse_date_to_ts(d_str: str) -> int:
    dt = datetime.datetime.strptime(d_str.strip(), "%Y-%m-%d")
    return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())


def _calc_spy_benchmark(start_ts: int, end_ts: int) -> dict[str, float]:
    df = bt._cached_frame("SPY")
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-asset Portfolio Simulation for US Reversion.")
    parser.add_argument("--slots", type=int, default=5, help="Number of portfolio slots (default: 5)")
    parser.add_argument("--cash", type=float, default=1350.0, help="Initial capital in USD (default: 1350)")
    parser.add_argument("--fractional", action=argparse.BooleanOptionalAction, default=True,
                        help="Allow fractional shares (default: True)")
    parser.add_argument("--profile", default=config.DEFAULT_BROKER_PROFILE_US,
                        help=f"Broker profile (default: {config.DEFAULT_BROKER_PROFILE_US})")
    parser.add_argument("--regime-filter", default=None, choices=["spy200", "spy150", "spy250"],
                        help="Market regime filter (e.g. spy200)")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument("--symbols", nargs="*", help="Override symbols (default: US watchlist)")
    parser.add_argument("--all", action="store_true", help="Include all watchlist symbols (both GPW and US)")
    args = parser.parse_args()

    db.init_db()
    watchlist = db.get_watchlist()
    if args.symbols:
        universe = [s.strip().upper() for s in args.symbols]
    elif args.all:
        universe = watchlist
    else:
        universe = [s for s in watchlist if not s.endswith(".WA")]

    start_ts = _parse_date_to_ts(args.start) if args.start else None
    end_ts = _parse_date_to_ts(args.end) if args.end else None

    print("\n" + "=" * 80)
    print("=== SYMULACJA PORTFELOWA: US MEAN REVERSION ===")
    print("=" * 80)
    print(f"Uniwersum: {len(universe)} instrumentów US: {', '.join(universe[:10])}...")
    print(f"Kapitał początkowy: ${args.cash:,.2f} USD")
    print(f"Liczba slotów: {args.slots} (alokacja {100.0 / args.slots:.1f}% kapitału na pozycję)")
    print(f"Profil brokera: {args.profile}")
    print(f"Ułamkowe akcje: {'TAK' if args.fractional else 'NIE'}")
    print(f"Filtr reżimu:   {args.regime_filter or 'BRAK'}")
    print("-" * 80)

    sim = PortfolioSimulator(
        symbols=universe,
        initial_cash=args.cash,
        max_slots=args.slots,
        broker_profile=args.profile,
        fractional=args.fractional,
        regime_filter=args.regime_filter,
    )

    t0 = time.time()
    res = sim.run(start_ts=start_ts, end_ts=end_ts)
    elapsed = time.time() - t0

    if "error" in res:
        print(f"Błąd symulacji: {res['error']}")
        return

    # Benchmark SPY
    first_ts = res["curve"][0]["ts"]
    last_ts = res["curve"][-1]["ts"]
    spy_bm = _calc_spy_benchmark(first_ts, last_ts)

    start_dt = datetime.datetime.fromtimestamp(first_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
    end_dt = datetime.datetime.fromtimestamp(last_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")

    print("\n" + "=" * 80)
    print("=== WYNIKI SYMULACJI PORTFELA ===")
    print("=" * 80)
    print(f"Okres: {start_dt} do {end_dt} ({res['days']} dni sesyjnych)")
    print(f"Kapitał końcowy:        ${res['final_equity']:,.2f} USD")
    print(f"Łączny zwrot:           {res['total_return_pct']:+.2f}% (SPY B&H: {spy_bm['return_pct']:+.2f}%)")
    print(f"CAGR:                   {res['cagr']:+.2f}% / rok (SPY B&H: {spy_bm['cagr']:+.2f}%)")
    print(f"Max Drawdown:           {res['max_drawdown_pct']:.2f}% (SPY B&H: {spy_bm['mdd_pct']:.2f}%)")
    print(f"Max czas obsunięcia:    {res['max_drawdown_duration_bars']} dni sesyjnych")
    print(f"Sharpe Ratio (rf=0%):   {res['sharpe_rf0']:.3f}")
    print(f"Sharpe Ratio (rf=4%):   {res['sharpe_rf4']:.3f}")
    print(f"Sortino Ratio:          {res['sortino']:.3f}")
    print(f"Calmar Ratio:           {res['calmar']:.2f}")
    print("-" * 80)
    print(f"Liczba transakcji:      {res['total_trades']}")
    print(f"Win Rate:               {res['win_rate']:.1f}%")
    print(f"Profit Factor:          {res['profit_factor']:.2f}")
    print(f"Średni zysk/transakcja: {res['expectancy_per_trade']:+.3f}% (mediana: {res['median_trade_ret']:+.3f}%)")
    print("-" * 80)
    print(f"Średnie użycie slotów:  {res['avg_slots_used']:.2f} / {args.slots} ({res['avg_slots_used']/args.slots*100:.1f}%)")
    print(f"Dni z obciążeniem ≥80%: {res['pct_days_slots_above_80']:.1f}% dni")
    rej = res["rejected_signals"]
    print(f"Odrzucone sygnały:      łącznie {rej['total']} "
          f"(brak slotu: {rej['slot_full']}, filtr reżimu: {rej.get('regime_off', 0)}, "
          f"brak gotówki: {rej['cash_exhausted']}, prowizja: {rej['cash_exhausted_comm']}, "
          f"rozmiar < min: {rej['size_below_min']}, brak open: {rej['no_open_price']})")
    print("=" * 80)

    # Save report
    res["benchmark_spy"] = spy_bm
    res["config"] = {
        "slots": args.slots,
        "cash": args.cash,
        "fractional": args.fractional,
        "profile": args.profile,
        "regime_filter": args.regime_filter,
        "universe_size": len(universe),
        "start": start_dt,
        "end": end_dt,
    }

    reports_dir = config.DATA_DIR.parent / "reports" if hasattr(config, "DATA_DIR") else Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    regime_suffix = f"_{args.regime_filter}" if args.regime_filter else ""
    report_file = reports_dir / f"portfolio_us_sim_{time.strftime('%Y%m%d_%H%M%S')}_{args.profile}_{args.slots}slots{regime_suffix}.json"
    with open(report_file, "w") as f:
        json.dump(res, f, default=str, indent=2)
    print(f"Zapisano pełny raport portfelowy: {report_file} (czas obliczeń: {elapsed:.2f}s)\n")


if __name__ == "__main__":
    main()
