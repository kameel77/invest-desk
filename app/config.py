"""Configuration for the trading desk.

Everything is overridable through environment variables so the app can be
deployed without editing code.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("TD_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "trading.db"

# --- Server ---------------------------------------------------------------
HOST = os.getenv("TD_HOST", "127.0.0.1")
PORT = int(os.getenv("TD_PORT", "8770"))

# --- Brokerage simulation -------------------------------------------------
STARTING_CASH = float(os.getenv("TD_STARTING_CASH", "10000"))
# GPW standard retail commission is 0.35% of notional, with a per-trade floor
# charged by most Polish brokers. Both are configurable.
COMMISSION_PCT = float(os.getenv("TD_COMMISSION_PCT", "0.0035"))
COMMISSION_MIN = float(os.getenv("TD_COMMISSION_MIN", "1.00"))
SLIPPAGE_PCT = float(os.getenv("TD_SLIPPAGE_PCT", "0.001"))
# Minimum tradable notional per signal — small-ticket discipline.
MIN_TICKET = float(os.getenv("TD_MIN_TICKET", "50"))

# Broker profiles per market
DEFAULT_BROKER_PROFILE_US = os.getenv("TD_BROKER_PROFILE_US", "IBKR_TIERED_USD")
DEFAULT_BROKER_PROFILE_GPW = os.getenv("TD_BROKER_PROFILE_GPW", "GPW_RETAIL")

# Portfolio slot constraints for US paper trading / execution
US_MAX_SLOTS = int(os.getenv("TD_US_MAX_SLOTS", "5"))
US_SLOT_PCT = float(os.getenv("TD_US_SLOT_PCT", "0.20"))

# --- Strategy & Hypothesis flags ------------------------------------------
# Trend-momentum has an unconfirmed OOS edge (-2.02% OOS). By default, new BUY
# entries for trend are disabled (0) to preserve cash for validated hypotheses.
TREND_ENABLED = os.getenv("TD_TREND_ENABLED", "0") == "1"

# Mean reversion hold period (bars) — single source of truth across modules.
REVERSION_HOLD_DAYS = int(os.getenv("TD_REVERSION_HOLD_DAYS", "10"))

# Mean reversion on GPW showed consistent negative OOS expectancy (base variant
# -0.62%; all 12 fixed-sweep variants between -0.24% and -0.81%, -1.07% with 1.5x
# costs) due to transaction costs (0.9% round-trip).
# By default, paper trade execution for reversion is disabled on GPW (0).
REVERSION_GPW_ENABLED = os.getenv("TD_REVERSION_GPW_ENABLED", "0") == "1"

# --- Market data ----------------------------------------------------------
# Optional Alpaca key unlocks true real-time US quotes (IEX, free tier).
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
ALPACA_REALTIME = bool(ALPACA_API_KEY and ALPACA_SECRET)

USER_AGENT = os.getenv("TD_USER_AGENT", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")

# Cache TTLs (seconds) - keeps us inside free-tier rate limits.
QUOTE_TTL = float(os.getenv("TD_QUOTE_TTL", "30"))
CANDLE_TTL = float(os.getenv("TD_CANDLE_TTL", "900"))

# A quote older than this is not a quote. Yahoo returns a real-looking
# regularMarketPrice for halted/delisted listings whose regularMarketTime is
# months old, and the change vs. its stale prev_close reads as a huge move
# that never happened. 4 days covers weekends and exchange holidays.
STALE_QUOTE_SECONDS = float(os.getenv("TD_STALE_QUOTE_SECONDS", str(4 * 86400)))

SCAN_INTERVAL = int(os.getenv("TD_SCAN_INTERVAL", "300"))  # seconds

# Default universe. Every GPW symbol below was verified live against the Yahoo
# chart API — the suffix is NOT derivable from the company name (KGHM -> KGH.WA,
# mBank -> MBK.WA, Budimex -> BDX.WA), and Yahoo's Warsaw coverage is only
# partial, so this list is deliberately short rather than aspirational.
# Use `python -m tools.resolve_gpw "<nazwa firmy>"` to find a missing one.
DEFAULT_WATCHLIST = [
    # --- GPW / WSE (live-verified) ---
    "CDR.WA",  # CD Projekt
    "KGH.WA",  # KGHM
    "PKN.WA",  # Orlen
    "PZU.WA",  # PZU
    "MBK.WA",  # mBank
    "PEO.WA",  # Pekao
    "ALR.WA",  # Alior Bank
    "MIL.WA",  # Bank Millennium
    "ING.WA",  # ING Bank Slaski
    "BOS.WA",  # Bank Ochrony Silesia
    "LPP.WA",  # LPP
    "CPR.WA",  # CPD
    "TPE.WA",  # Tauron
    "ENI.WA",  # ENEA
    "GPW.WA",  # GPW benchmark
    "TRN.WA",  # Getin Noble Securities
    "XTB.WA",  # XTB
    "11B.WA",  # 11 bit studios
    "ABS.WA",  # ABS Development
    "BDX.WA",  # Budimex
    "DNP.WA",  # Dino Polska
    "ZAB.WA",  # Zabka
    "CPS.WA",  # Polsat
    "EAT.WA",  # ATENOR Polska
    "ZAP.WA",  # Zapro
    "ATC.WA",  # ATC Auto
    "MCI.WA",  # MCI
    "APN.WA",  # Agora
    # HPE.WA (HIPERCO) and VER.WA (Vercom) are deliberately absent: Yahoo still
    # returns a last price for them, but its timestamp is 2024, so they render
    # as a fictional +78%/+120% day. Run `python -m tools.check_stale` to audit.
    "GTC.WA",  # Getin Top Car
    "TEN.WA",  # TENET Group
    # --- US large caps / liquid ---
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "V",
    "XOM", "UNH", "LLY", "AVGO", "COST", "NFLX",
    # --- ETFs / proxies ---
    "SPY", "QQQ", "IWM", "EFA", "EEM", "VGK", "EWZ",
]
