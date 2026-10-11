"""CLI runner and report generator for M1 ETF Trend Following Strategy.

Runs 20-year backtest on the ETF basket (SPY, TLT, GLD, EEM, DBC) with monthly
rebalancing, inverse-volatility sizing, differential turnover, and broker cost profiles.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import costs, etf_sim


def main() -> None:
    parser = argparse.ArgumentParser(description="Run M1 ETF Trend Following Backtest")
    parser.add_argument("--start", type=str, default="2006-10-10", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, default="2026-10-09", help="End date YYYY-MM-DD")
    parser.add_argument("--cash", type=float, default=10000.0, help="Initial cash (USD)")
    parser.add_argument("--profile", type=str, default="XTB_USD", help="Broker profile from app/costs.py (e.g. XTB_USD, IBKR_TIERED_USD, CONSERVATIVE_US)")
    parser.add_argument("--fee", type=float, default=None, help="Explicit fee & slippage rate per turnover (overrides --profile if provided)")
    parser.add_argument("--cap", type=float, default=0.30, help="Max weight per asset (default 30%)")
    parser.add_argument("--save", action="store_true", default=True, help="Save JSON report to reports/")
    args = parser.parse_args()

    # Determine fee rate from profile or explicit argument
    if args.fee is not None:
        fee_rate = args.fee
    else:
        prof = costs.get_profile(args.profile)
        # Turnover fee rate combines commission + slippage + fx markup
        fee_rate = prof.commission_pct + prof.slippage_pct + prof.fx_pct

    start_d = datetime.date.fromisoformat(args.start) if args.start else None
    end_d = datetime.date.fromisoformat(args.end) if args.end else None

    print("=" * 70)
    print(" M1 TIME SERIES MOMENTUM — KOSZYK ETF (20 LAT)")
    print(f" Uniwersum: {', '.join(etf_sim.DEFAULT_M1_UNIVERSE)}")
    print(f" Okres: {args.start} do {args.end} | Kapitał początkowy: ${args.cash:,.2f}")
    print(f" Profil brokera: {args.profile} | Koszt obrotu: {fee_rate * 100:.2f}% | Cap na walor: {args.cap * 100:.0f}%")
    print("=" * 70)

    frames = etf_sim.load_etf_frames(etf_sim.DEFAULT_M1_UNIVERSE)
    missing = [s for s in etf_sim.DEFAULT_M1_UNIVERSE if s not in frames]
    if missing:
        print(f"Brakujące instrumenty w bazie: {missing}")
        return

    res = etf_sim.simulate_m1(
        frames=frames,
        symbols=etf_sim.DEFAULT_M1_UNIVERSE,
        initial_cash=args.cash,
        fee_rate=fee_rate,
        max_asset_weight=args.cap,
        start_date=start_d,
        end_date=end_d,
    )

    print("\n[1] GŁÓWNE WSKAŹNIKI STRATEGII:")
    print(f"  • Kapitał końcowy:        ${res['final_equity']:,.2f} (Zwrot: +{res['total_return_pct']:.1f}%)")
    print(f"  • CAGR (roczna stopa):    {res['cagr_pct']:.2f}%")
    print(f"  • Max Drawdown (MDD):     {res['max_drawdown_pct']:.2f}%")
    print(f"  • Sharpe Ratio (rf=0%):   {res['sharpe_rf0']:.3f}")
    print(f"  • Sortino Ratio:          {res['sortino']:.3f}")
    print(f"  • Liczba rebalansów:      {res['rebalances_count']}")

    spy = res.get("spy_benchmark", {})
    if spy:
        print("\n[2] PORÓWNANIE Z BENCHMARKIEM SPY BUY & HOLD:")
        print(f"  • SPY CAGR:               {spy.get('cagr_pct'):.2f}%")
        print(f"  • SPY Max Drawdown:       {spy.get('max_drawdown_pct'):.2f}%")
        print(f"  • SPY Sharpe:             {spy.get('sharpe'):.3f}")
        print(f"  • Redukcja ryzyka (MDD):  Z {spy.get('max_drawdown_pct'):.1f}% na {res['max_drawdown_pct']:.1f}% (-{abs(spy.get('max_drawdown_pct', 0) - res['max_drawdown_pct']):.1f} pp)")

    print("\n[3] ROCZNE STOPY ZWROTU M1 (2006–2026):")
    print("  Rok    |  M1 Zwrot  | Status Kryzysu")
    print("  -------+------------+--------------------------------------")
    for yr, ret in sorted(res["annual_returns"].items()):
        note = ""
        if yr == 2008:
            note = "<- Wielki Kryzys Finansowy (SPY -38%)"
        elif yr == 2011:
            note = "<- Kryzys zadłużenia strefy euro"
        elif yr == 2020:
            note = "<- Krach Covidowy"
        elif yr == 2022:
            note = "<- Bessa inflacyjna (SPY -18%, 60/40 -16%)"
        sign = "+" if ret > 0 else ""
        print(f"  {yr}   |   {sign}{ret:6.2f}%  | {note}")

    if args.save:
        reports_dir = Path("reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = reports_dir / f"m1_etf_trend_{ts_str}.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)
        print(f"\n[OK] Raport zapisany do: {report_path}")


if __name__ == "__main__":
    main()
