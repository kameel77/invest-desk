"""Transaction cost model for backtests and paper simulations.

Implements both percentage-based multiplicative formulas and exact cash/per-share
cost profiles across brokers and exchanges:
- GPW_RETAIL: standard Polish brokerage (0.35% commission, min 3.00 PLN, 0.10% slippage, 0% FX).
- IBKR_TIERED_USD: Interactive Brokers Tiered for US stocks/ETFs ($0.0035/share, min $0.35,
  max 1.0% notional, +$0.001/share regulatory/exchange fees, 0.05% slippage, 0% FX on USD account).
- XTB_PLN: XTB zero-commission tier (0% commission up to 100k EUR/month, 0.05% slippage,
  0.50% FX conversion markup each way = 1.0% round-trip for PLN accounts).
- CONSERVATIVE_US: stress-test profile (0.10% commission, min $1.00, 0.10% slippage, 0.50% FX each way).

Note: SEC transaction fee (approx 0.00278% on sales) and FINRA TAF ($0.000166/share on sales)
are negligible (< 0.003%) relative to minimum ticks and slippage, and are omitted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config


@dataclass(frozen=True)
class BrokerProfile:
    name: str
    commission_pct: float = 0.0
    commission_min: float = 0.0
    commission_per_share: float = 0.0
    commission_max_pct: float | None = None
    regulatory_per_share: float = 0.0
    slippage_pct: float = 0.001
    fx_pct: float = 0.0  # Applied on both entry and exit (each way)


PROFILES: dict[str, BrokerProfile] = {
    "GPW_RETAIL": BrokerProfile(
        name="GPW_RETAIL",
        commission_pct=0.0035,
        commission_min=3.00,
        slippage_pct=0.0010,
        fx_pct=0.0,
    ),
    "IBKR_TIERED_USD": BrokerProfile(
        name="IBKR_TIERED_USD",
        commission_pct=0.0,
        commission_min=0.35,
        commission_per_share=0.0035,
        commission_max_pct=0.010,
        regulatory_per_share=0.0010,
        slippage_pct=0.0005,
        fx_pct=0.0,
    ),
    "XTB_PLN": BrokerProfile(
        name="XTB_PLN",
        commission_pct=0.0,
        commission_min=0.0,
        slippage_pct=0.0005,
        fx_pct=0.0050,  # 0.5% each way = ~1.0% round trip
    ),
    "XTB_USD": BrokerProfile(
        name="XTB_USD",
        commission_pct=0.0,
        commission_min=0.0,
        slippage_pct=0.0005,
        fx_pct=0.0,  # Subkonto USD - bez marży FX na transakcjach
    ),
    "CONSERVATIVE_US": BrokerProfile(
        name="CONSERVATIVE_US",
        commission_pct=0.0010,
        commission_min=1.00,
        slippage_pct=0.0010,
        fx_pct=0.0050,
    ),
}


def get_profile(name_or_symbol: str = "") -> BrokerProfile:
    """Resolve a BrokerProfile from a profile name or ticker symbol."""
    if name_or_symbol in PROFILES:
        return PROFILES[name_or_symbol]
    if name_or_symbol.upper().endswith(".WA"):
        return PROFILES.get(config.DEFAULT_BROKER_PROFILE_GPW, PROFILES["GPW_RETAIL"])
    return PROFILES.get(config.DEFAULT_BROKER_PROFILE_US, PROFILES["IBKR_TIERED_USD"])


def calculate_trade_costs(
    side: str,
    price: float,
    shares: float,
    profile: BrokerProfile | str | None = None,
) -> dict[str, float]:
    """Calculate exact cash amounts for slippage, commission, and FX markup."""
    p = profile if isinstance(profile, BrokerProfile) else get_profile(profile or "")
    side_u = side.upper()
    notional = float(price) * float(shares)

    # Slippage: shifts price against the trader
    slip_amount = notional * p.slippage_pct
    executed_px = price * (1.0 + p.slippage_pct) if side_u == "BUY" else price * (1.0 - p.slippage_pct)

    # Commission
    comm = 0.0
    if p.commission_per_share > 0 or p.regulatory_per_share > 0:
        base_comm = shares * p.commission_per_share
        if p.commission_min > 0:
            base_comm = max(base_comm, p.commission_min)
        if p.commission_max_pct is not None and p.commission_max_pct > 0:
            base_comm = min(base_comm, notional * p.commission_max_pct)
        comm = base_comm + (shares * p.regulatory_per_share)
    elif p.commission_pct > 0:
        comm = max(notional * p.commission_pct, p.commission_min)

    # FX cost (multiplicative markup on notional for currency conversion)
    fx_cost = notional * p.fx_pct

    # Total cash outflow/inflow
    if side_u == "BUY":
        effective_px = (notional + slip_amount + comm + fx_cost) / shares if shares > 0 else executed_px
        total_cash = notional + slip_amount + comm + fx_cost
    else:
        effective_px = (notional - slip_amount - comm - fx_cost) / shares if shares > 0 else executed_px
        total_cash = notional - slip_amount - comm - fx_cost

    return {
        "side": side_u,
        "raw_price": float(price),
        "executed_price": float(executed_px),
        "effective_price": float(effective_px),
        "shares": float(shares),
        "notional": float(notional),
        "commission": float(comm),
        "slippage": float(slip_amount),
        "fx_cost": float(fx_cost),
        "total_cash": float(total_cash),
    }


def net_return_pct(
    entry_px: float,
    exit_px: float,
    commission_pct: float | None = None,
    slippage_pct: float | None = None,
    fx_pct: float | None = None,
    shares: float | None = None,
    notional: float | None = None,
    symbol: str = "",
    profile: BrokerProfile | str | None = None,
) -> float:
    """Zwrot netto uwzględniający prowizję, poślizg i ewentualny FX po obu stronach transakcji.

    W ścieżce profilowej (gdy przekazano profile lub gdy podano notional/shares z symbolem)
    wymagane jest jawne podanie wartości transakcji (notional w USD) lub liczby akcji (shares).
    W przeciwnym razie rzucany jest ValueError (nie zakłada się stałych 100 akcji).

    Gdy nie podano profilu ani notional/shares, funkcja zachowuje standardowe procentowe zachowanie
    oparte na config.COMMISSION_PCT i config.SLIPPAGE_PCT, unikając pułapki zerowej prowizji
    z profilu IBKR dla symboli US.
    """
    if profile is not None or notional is not None or (shares is not None and shares > 0):
        if shares is not None and shares > 0:
            n_shares = float(shares)
        elif notional is not None and notional > 0:
            n_shares = float(notional) / float(entry_px)
        else:
            raise ValueError("Profile-based net_return_pct requires 'notional' or 'shares'")

        prof = profile if isinstance(profile, BrokerProfile) else get_profile(profile or symbol)
        buy_res = calculate_trade_costs("BUY", entry_px, n_shares, prof)
        sell_res = calculate_trade_costs("SELL", exit_px, n_shares, prof)
        if buy_res["total_cash"] <= 0:
            return 0.0
        return float(((sell_res["total_cash"] / buy_res["total_cash"]) - 1.0) * 100.0)

    # Legacy/percentage-based multiplicative formula
    comm = float(commission_pct if commission_pct is not None else config.COMMISSION_PCT)
    slip = float(slippage_pct if slippage_pct is not None else config.SLIPPAGE_PCT)
    fx = float(fx_pct if fx_pct is not None else 0.0)

    entry_eff = float(entry_px) * (1.0 + slip) * (1.0 + comm) * (1.0 + fx)
    exit_eff = float(exit_px) * (1.0 - slip) * (1.0 - comm) * (1.0 - fx)
    return float(((exit_eff / entry_eff) - 1.0) * 100.0)

