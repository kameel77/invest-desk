"""Transaction cost model for backtests and paper simulations."""
from __future__ import annotations

from . import config


def net_return_pct(entry_px: float, exit_px: float) -> float:
    """Zwrot netto uwzględniający prowizję i poślizg po obu stronach transakcji.

    Wzór multiplikatywny:
      entry_eff = entry_px * (1 + SLIPPAGE_PCT) * (1 + COMMISSION_PCT)
      exit_eff  = exit_px  * (1 - SLIPPAGE_PCT) * (1 - COMMISSION_PCT)
      return    = ((exit_eff / entry_eff) - 1.0) * 100.0

    Minimalna prowizja (COMMISSION_MIN) jest pomijana w symulacjach wieloletnich.
    """
    comm = float(config.COMMISSION_PCT)
    slip = float(config.SLIPPAGE_PCT)
    entry_eff = float(entry_px) * (1.0 + slip) * (1.0 + comm)
    exit_eff = float(exit_px) * (1.0 - slip) * (1.0 - comm)
    return float(((exit_eff / entry_eff) - 1.0) * 100.0)
